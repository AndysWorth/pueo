"""Long-lived HA WebSocket event subscriber with ring buffer.

Maintains a persistent, auto-reconnecting WebSocket connection to HA and
buffers filtered events in a collections.deque ring buffer.  Query the buffer
with get_events() — this is synchronous and safe to call from any async context.

Subscribed event types:
  state_changed        — filtered to update.* entities and unavailable/unknown
  repairs_issue_registry_updated
  automation_triggered
  persistent_notification/subscribe  — stored as "persistent_notification_event"
"""

from __future__ import annotations

import asyncio
import collections
import json
import time
from typing import Any

from utils.core.logging import get_logger

_log = get_logger("ha_event_subscriber")

_INTERESTING_DOMAINS = frozenset({"update"})
_INTERESTING_STATES = frozenset({"unavailable", "unknown"})

_DEFAULT_BUFFER_SIZE = 500

# Subscription ID → event type label stored in the ring buffer
_SUB_EVENTS = [
    "state_changed",
    "repairs_issue_registry_updated",
    "automation_triggered",
]
_NOTIF_SUB_TYPE = "persistent_notification/subscribe"
_NOTIF_EVENT_LABEL = "persistent_notification_event"


def _is_interesting_state_change(data: dict) -> bool:
    entity_id: str = data.get("entity_id", "")
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    new_state_dict = data.get("new_state") or {}
    new_state: str = (
        new_state_dict.get("state", "") if isinstance(new_state_dict, dict) else ""
    )
    return domain in _INTERESTING_DOMAINS or new_state in _INTERESTING_STATES


class HAEventSubscriber:  # pragma: no cover
    """Persistent WS connection that buffers HA events in a ring buffer.

    Call start() once to launch the reconnect loop as an asyncio task.
    Query get_events() at any time to read from the buffer.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._buffer: collections.deque[dict] = collections.deque(maxlen=buffer_size)
        self._connected = False

    def get_events(
        self,
        event_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return buffered events, newest first, filtered by type and/or entity_id."""
        events: list[dict] = list(self._buffer)
        if event_type:
            events = [e for e in events if e.get("event_type") == event_type]
        if entity_id:
            events = [e for e in events if e.get("entity_id") == entity_id]
        # buffer is oldest-first; reverse to newest-first then slice
        return list(reversed(events))[:limit]

    def is_connected(self) -> bool:
        return self._connected

    async def run_forever(self) -> None:
        """Reconnect loop with exponential backoff.  Intended as a supervisor task."""
        backoff = 2.0
        while True:
            try:
                await self._connect_and_subscribe()
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                _log.warning(
                    "ha_event_subscriber_error",
                    error=str(exc),
                    retry_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

    async def _connect_and_subscribe(self) -> None:
        """Connect, authenticate, subscribe, and process events until disconnection."""
        import websockets

        uri = f"ws://{self._host}:{self._port}/api/websocket"
        async with websockets.connect(uri, open_timeout=10) as ws:
            await self._auth(ws)

            # Subscribe to regular event types (IDs 1, 2, 3)
            sub_id_map: dict[int, str] = {}
            for i, event_type in enumerate(_SUB_EVENTS, start=1):
                await ws.send(
                    json.dumps(
                        {"id": i, "type": "subscribe_events", "event_type": event_type}
                    )
                )
                resp = json.loads(await ws.recv())
                if not resp.get("success"):
                    _log.warning(
                        "ha_event_subscribe_failed", event_type=event_type, resp=resp
                    )
                else:
                    sub_id_map[i] = event_type

            # Subscribe to persistent_notification/subscribe (ID 4)
            notif_id = len(_SUB_EVENTS) + 1
            await ws.send(json.dumps({"id": notif_id, "type": _NOTIF_SUB_TYPE}))
            resp = json.loads(await ws.recv())
            if not resp.get("success"):
                _log.warning(
                    "ha_event_subscribe_failed",
                    event_type=_NOTIF_SUB_TYPE,
                    resp=resp,
                )
            else:
                sub_id_map[notif_id] = _NOTIF_EVENT_LABEL

            self._connected = True
            _log.info(
                "ha_event_subscriber_connected",
                host=self._host,
                subs=list(sub_id_map.values()),
            )

            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "event":
                    self._handle_event(msg, sub_id_map)

    async def _auth(self, ws: Any) -> None:
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {msg.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") == "auth_invalid":
            raise RuntimeError("HA WebSocket authentication failed")
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"Unexpected auth response: {msg.get('type')}")

    def _handle_event(self, msg: dict, sub_id_map: dict[int, str]) -> None:
        """Filter and buffer one event message from HA."""
        sub_id: int = msg.get("id", 0)
        event_type = sub_id_map.get(sub_id, "unknown")
        event = msg.get("event", {})
        data: dict = event.get("data", {}) if isinstance(event, dict) else {}

        # state_changed: only buffer interesting changes
        if event_type == "state_changed" and not _is_interesting_state_change(data):
            return

        entry: dict[str, Any] = {
            "event_type": event_type,
            "time": time.time(),
            "data": data,
        }
        entity_id: str = data.get("entity_id") or data.get("notification_id") or ""
        if entity_id:
            entry["entity_id"] = entity_id

        self._buffer.append(entry)


class FakeHAEventSubscriber:
    """Test double for HAEventSubscriber.  Pre-load events and control connected state."""

    def __init__(
        self,
        events: list[dict] | None = None,
        connected: bool = True,
    ) -> None:
        self._events: list[dict] = events or []
        self._connected = connected

    async def run_forever(self) -> None:
        """Sleep indefinitely — cancelled quickly in tests."""
        await asyncio.sleep(86400)

    def get_events(
        self,
        event_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        events = list(self._events)
        if event_type:
            events = [e for e in events if e.get("event_type") == event_type]
        if entity_id:
            events = [e for e in events if e.get("entity_id") == entity_id]
        return events[:limit]

    def is_connected(self) -> bool:
        return self._connected
