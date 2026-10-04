"""HA WebSocket client for device registry queries and FakeHAWebSocketClient test double."""

from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger("ha_ws_client")


class LovelaceConfigNotFound(Exception):
    """Raised when a named Lovelace dashboard has no stored config (file-mode or empty)."""


class HAWebSocketClient:  # pragma: no cover
    """Short-lived WebSocket client for HA device registry queries.

    Opens a connection, authenticates, fetches the requested data, then closes.
    Not suitable for long-lived subscriptions.
    """

    def __init__(self, host: str, port: int, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token

    async def _connect_and_auth(self):  # type: ignore[return]
        import websockets

        uri = f"ws://{self._host}:{self._port}/api/websocket"
        ws = await websockets.connect(uri, open_timeout=10)
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            await ws.close()
            raise RuntimeError(f"Expected auth_required, got: {msg.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") == "auth_invalid":
            await ws.close()
            raise RuntimeError("HA WebSocket authentication failed")
        if msg.get("type") != "auth_ok":
            await ws.close()
            raise RuntimeError(f"Unexpected auth response: {msg.get('type')}")
        return ws

    async def _call(self, type_: str, **payload: Any) -> Any:
        """Connect, auth, send one command, check success, close. Returns result."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": type_, **payload}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"{type_} request failed: {msg}")
            return msg.get("result", [])
        finally:
            await ws.close()

    async def get_device_registry(self) -> list[dict]:
        """Authenticate and fetch config/device_registry/list via HA WebSocket API."""
        return await self._call("config/device_registry/list")

    async def get_persistent_notifications(self) -> list[dict]:
        """Fetch current persistent notifications via HA WebSocket API."""
        return await self._call("persistent_notification/get")

    async def dismiss_notification(self, notification_id: str) -> None:
        """Dismiss a persistent notification via HA WebSocket call_service."""
        await self._call(
            "call_service",
            domain="persistent_notification",
            service="dismiss",
            service_data={"notification_id": notification_id},
        )

    async def get_repair_issues(self) -> list[dict]:
        """Fetch current repair issues via HA WebSocket API."""
        result = await self._call("repairs/list_issues")
        if not isinstance(result, dict):
            _log.error(
                "required_field_missing field=result source=ha_ws_repairs actual_type=%s",
                type(result).__name__,
            )
            return []
        return result.get("issues", [])

    async def get_config_entries(self) -> list[dict]:
        """Fetch loaded config entries via HA WebSocket API.

        Tries commands in order of recency:
          config_entries/get          — HA 2026.x+
          config/config_entries/all   — HA 2022–2025
          config_entries/list         — HA pre-2022
        """
        ws = await self._connect_and_auth()
        try:
            for msg_id, cmd in (
                (1, "config_entries/get"),
                (2, "config/config_entries/all"),
                (3, "config_entries/list"),
            ):
                await ws.send(json.dumps({"id": msg_id, "type": cmd}))
                msg = json.loads(await ws.recv())
                if msg.get("success"):
                    entries = msg.get("result", [])
                    return [e for e in entries if e.get("state") == "loaded"]
                if msg.get("error", {}).get("code") != "unknown_command":
                    raise RuntimeError(f"Config entries request failed: {msg}")
            raise RuntimeError("Config entries: no supported WebSocket command found")
        finally:
            await ws.close()

    async def get_all_config_entries(self) -> list[dict]:
        """Fetch all config entries including not-loaded ones via HA WebSocket API."""
        ws = await self._connect_and_auth()
        try:
            for msg_id, cmd in (
                (1, "config_entries/get"),
                (2, "config/config_entries/all"),
                (3, "config_entries/list"),
            ):
                await ws.send(json.dumps({"id": msg_id, "type": cmd}))
                msg = json.loads(await ws.recv())
                if msg.get("success"):
                    return msg.get("result", [])
                if msg.get("error", {}).get("code") != "unknown_command":
                    raise RuntimeError(f"Config entries request failed: {msg}")
            raise RuntimeError("Config entries: no supported WebSocket command found")
        finally:
            await ws.close()

    async def get_ha_components(self) -> list[str]:
        """Fetch the list of loaded HA components via REST /api/config."""
        import httpx

        url = f"http://{self._host}:{self._port}/api/config"
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data: dict = resp.json()
        return data.get("components", [])

    async def get_spook_entity_issues(self) -> list[dict]:
        """Fetch Spook entity issues via the spook/entities/issues/list WS command.

        Returns an empty list when Spook is not installed (unknown_command error).
        Raises on other failures.
        """
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "spook/entities/issues/list"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                code = msg.get("error", {}).get("code", "")
                if code == "unknown_command":
                    return []
                raise RuntimeError(f"spook/entities/issues/list request failed: {msg}")
            return msg.get("result", [])
        finally:
            await ws.close()

    async def get_entity_registry(self) -> list[dict]:
        """Fetch all entities via HA WebSocket config/entity_registry/list."""
        return await self._call("config/entity_registry/list")

    async def get_states(self) -> list[dict]:
        """Return all current HA entity states via get_states WS command."""
        return await self._call("get_states")

    async def get_system_log(self) -> list[dict]:
        """Fetch HA system log entries via system_log/list WS command."""
        result = await self._call("system_log/list")
        return result if isinstance(result, list) else []

    async def get_area_registry(self) -> list[dict]:
        """Fetch HA area registry via config/area_registry/list."""
        result = await self._call("config/area_registry/list")
        return result if isinstance(result, list) else []

    async def get_floor_registry(self) -> list[dict]:
        """Fetch HA floor registry via config/floor_registry/list."""
        result = await self._call("config/floor_registry/list")
        return result if isinstance(result, list) else []

    async def get_label_registry(self) -> list[dict]:
        """Fetch HA label registry via config/label_registry/list."""
        result = await self._call("config/label_registry/list")
        return result if isinstance(result, list) else []

    async def list_traces(self, domain: str, item_id: str | None = None) -> list[dict]:
        """List automation/script trace runs via trace/list WS command."""
        payload: dict = {"domain": domain}
        if item_id is not None:
            payload["item_id"] = item_id
        result = await self._call("trace/list", **payload)
        return result if isinstance(result, list) else []

    async def get_trace(self, domain: str, item_id: str, run_id: str) -> dict:
        """Fetch a single automation/script trace run via trace/get WS command."""
        result = await self._call(
            "trace/get", domain=domain, item_id=item_id, run_id=run_id
        )
        return result if isinstance(result, dict) else {}

    async def get_lovelace_dashboards(self) -> list[dict]:
        """List all named dashboards via lovelace/dashboards/list."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "lovelace/dashboards/list"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                return []
            return msg.get("result", [])
        finally:
            await ws.close()

    async def get_lovelace_config(self, url_path: str | None = None) -> dict:
        """Fetch a Lovelace dashboard config. url_path=None fetches the default dashboard."""
        ws = await self._connect_and_auth()
        try:
            payload: dict = {"id": 1, "type": "lovelace/config"}
            if url_path is not None:
                payload["url_path"] = url_path
            await ws.send(json.dumps(payload))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                if msg.get("error", {}).get("code") == "config_not_found":
                    if url_path is not None:
                        raise LovelaceConfigNotFound(
                            f"Dashboard '{url_path}' has no stored config (file-mode or empty)"
                        )
                    # Default dashboard in auto/storage mode — nothing to scan.
                    return {}
                raise RuntimeError(f"lovelace/config request failed: {msg}")
            return msg.get("result", {})
        finally:
            await ws.close()


class FakeHAWebSocketClient:
    """Test double for HAWebSocketClientProtocol."""

    def __init__(
        self,
        devices: list[dict] | None = None,
        notifications: list[dict] | None = None,
        repair_issues: list[dict] | None = None,
        config_entries: list[dict] | None = None,
        entity_registry: list[dict] | None = None,
        lovelace_dashboards: list[dict] | None = None,
        lovelace_configs: dict | None = None,
        lovelace_config_not_found: set[str] | None = None,
        states: list[dict] | None = None,
        ha_components: list[str] | None = None,
        spook_entity_issues: list[dict] | None = None,
        system_log: list[dict] | None = None,
        area_registry: list[dict] | None = None,
        floor_registry: list[dict] | None = None,
        label_registry: list[dict] | None = None,
        traces: list[dict] | None = None,
        trace_detail: dict | None = None,
    ) -> None:
        self._devices: list[dict] = devices or []
        self._notifications: list[dict] = notifications or []
        self._repair_issues: list[dict] = repair_issues or []
        self._config_entries: list[dict] = config_entries or []
        self._entity_registry: list[dict] = entity_registry or []
        self._lovelace_dashboards: list[dict] = lovelace_dashboards or []
        # Keys are url_path strings or None for the default dashboard.
        self._lovelace_configs: dict = lovelace_configs or {}
        # url_path values for which get_lovelace_config should raise LovelaceConfigNotFound.
        self._lovelace_config_not_found: set[str] = lovelace_config_not_found or set()
        self._states: list[dict] = states or []
        self._ha_components: list[str] = ha_components or []
        self._spook_entity_issues: list[dict] = spook_entity_issues or []
        self._system_log: list[dict] = system_log or []
        self._area_registry: list[dict] = area_registry or []
        self._floor_registry: list[dict] = floor_registry or []
        self._label_registry: list[dict] = label_registry or []
        # traces: list of run summaries returned by list_traces
        self._traces: list[dict] = traces or []
        # trace_detail: full trace dict returned by get_trace (same for all run_ids)
        self._trace_detail: dict = trace_detail or {}
        self.calls: list[str] = []

    async def get_device_registry(self) -> list[dict]:
        self.calls.append("get_device_registry")
        return list(self._devices)

    async def get_persistent_notifications(self) -> list[dict]:
        self.calls.append("get_persistent_notifications")
        return list(self._notifications)

    async def get_repair_issues(self) -> list[dict]:
        self.calls.append("get_repair_issues")
        return list(self._repair_issues)

    async def get_config_entries(self) -> list[dict]:
        self.calls.append("get_config_entries")
        return [e for e in self._config_entries if e.get("state") == "loaded"]

    async def get_all_config_entries(self) -> list[dict]:
        self.calls.append("get_all_config_entries")
        return list(self._config_entries)

    async def get_ha_components(self) -> list[str]:
        self.calls.append("get_ha_components")
        return list(self._ha_components)

    async def get_spook_entity_issues(self) -> list[dict]:
        self.calls.append("get_spook_entity_issues")
        return list(self._spook_entity_issues)

    async def get_entity_registry(self) -> list[dict]:
        self.calls.append("get_entity_registry")
        return list(self._entity_registry)

    async def get_states(self) -> list[dict]:
        self.calls.append("get_states")
        return list(self._states)

    async def dismiss_notification(self, notification_id: str) -> None:
        self.calls.append(f"dismiss_notification:{notification_id}")

    async def get_lovelace_dashboards(self) -> list[dict]:
        self.calls.append("get_lovelace_dashboards")
        return list(self._lovelace_dashboards)

    async def get_lovelace_config(self, url_path: str | None = None) -> dict:
        self.calls.append(f"get_lovelace_config:{url_path}")
        if url_path is not None and url_path in self._lovelace_config_not_found:
            raise LovelaceConfigNotFound(
                f"Dashboard '{url_path}' has no stored config (file-mode or empty)"
            )
        if url_path not in self._lovelace_configs:
            raise RuntimeError(f"No lovelace config for url_path={url_path!r}")
        return dict(self._lovelace_configs[url_path])

    async def get_system_log(self) -> list[dict]:
        self.calls.append("get_system_log")
        return list(self._system_log)

    async def get_area_registry(self) -> list[dict]:
        self.calls.append("get_area_registry")
        return list(self._area_registry)

    async def get_floor_registry(self) -> list[dict]:
        self.calls.append("get_floor_registry")
        return list(self._floor_registry)

    async def get_label_registry(self) -> list[dict]:
        self.calls.append("get_label_registry")
        return list(self._label_registry)

    async def list_traces(self, domain: str, item_id: str | None = None) -> list[dict]:
        self.calls.append(f"list_traces:{domain}:{item_id}")
        return list(self._traces)

    async def get_trace(self, domain: str, item_id: str, run_id: str) -> dict:
        self.calls.append(f"get_trace:{domain}:{item_id}:{run_id}")
        return dict(self._trace_detail)
