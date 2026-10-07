"""Tests for utils/ha/ha_event_subscriber.py.

Covers: message parsing, state-change filter, ring-buffer bound, reconnect
backoff, FakeHAEventSubscriber, and HAEventSubscriberProtocol structural check.
All tests use a scripted mock websocket — no real HA connection.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sub_id_map() -> dict[int, str]:
    return {
        1: "state_changed",
        2: "repairs_issue_registry_updated",
        3: "automation_triggered",
        4: "persistent_notification_event",
    }


def _event_msg(sub_id: int, data: dict, event_type_override: str | None = None) -> dict:
    """Build a raw HA event message for a given subscription ID."""
    inner: dict = {"data": data}
    if event_type_override:
        inner["event_type"] = event_type_override
    return {"id": sub_id, "type": "event", "event": inner}


# ---------------------------------------------------------------------------
# _is_interesting_state_change
# ---------------------------------------------------------------------------


class TestIsInterestingStateChange:
    def _fn(self, data: dict) -> bool:
        from utils.ha.ha_event_subscriber import _is_interesting_state_change

        return _is_interesting_state_change(data)

    def test_update_domain_is_interesting(self):
        assert self._fn({"entity_id": "update.core", "new_state": {"state": "on"}})

    def test_unavailable_state_is_interesting(self):
        assert self._fn(
            {"entity_id": "sensor.temp", "new_state": {"state": "unavailable"}}
        )

    def test_unknown_state_is_interesting(self):
        assert self._fn(
            {"entity_id": "light.bedroom", "new_state": {"state": "unknown"}}
        )

    def test_normal_sensor_change_not_interesting(self):
        assert not self._fn(
            {"entity_id": "sensor.temperature", "new_state": {"state": "22.5"}}
        )

    def test_light_on_not_interesting(self):
        assert not self._fn({"entity_id": "light.lounge", "new_state": {"state": "on"}})

    def test_missing_new_state_not_interesting(self):
        assert not self._fn({"entity_id": "sensor.foo"})

    def test_new_state_none_not_interesting(self):
        # HA sends new_state=null when entity is removed
        assert not self._fn({"entity_id": "sensor.foo", "new_state": None})

    def test_no_entity_id_not_interesting(self):
        assert not self._fn({"new_state": {"state": "on"}})

    def test_ai_agent_ha_automation_is_interesting(self):
        assert self._fn(
            {
                "entity_id": "automation.ai_agent_auto_lights",
                "new_state": {"state": "on"},
            }
        )

    def test_ai_agent_ha_automation_off_is_interesting(self):
        assert self._fn(
            {
                "entity_id": "automation.ai_agent_auto_notify",
                "new_state": {"state": "off"},
            }
        )

    def test_non_ai_agent_automation_not_interesting(self):
        assert not self._fn(
            {"entity_id": "automation.my_lights", "new_state": {"state": "on"}}
        )


# ---------------------------------------------------------------------------
# _AI_AGENT_HA_AUTOMATION_PREFIX constant
# ---------------------------------------------------------------------------


class TestAiAgentHaPrefix:
    def test_prefix_value(self):
        from utils.ha.ha_event_subscriber import _AI_AGENT_HA_AUTOMATION_PREFIX

        assert _AI_AGENT_HA_AUTOMATION_PREFIX == "automation.ai_agent_auto_"


# ---------------------------------------------------------------------------
# lovelace_updated in _SUB_EVENTS
# ---------------------------------------------------------------------------


class TestSubEvents:
    def test_lovelace_updated_subscribed(self):
        from utils.ha.ha_event_subscriber import _SUB_EVENTS

        assert "lovelace_updated" in _SUB_EVENTS

    def test_automation_triggered_subscribed(self):
        from utils.ha.ha_event_subscriber import _SUB_EVENTS

        assert "automation_triggered" in _SUB_EVENTS


# ---------------------------------------------------------------------------
# FakeHAEventSubscriber
# ---------------------------------------------------------------------------


class TestFakeHAEventSubscriber:
    def _make(self, events=None, connected=True):
        from utils.ha.ha_event_subscriber import FakeHAEventSubscriber

        return FakeHAEventSubscriber(events=events, connected=connected)

    def test_get_events_empty(self):
        fake = self._make()
        assert fake.get_events() == []

    def test_is_connected_true(self):
        assert self._make(connected=True).is_connected() is True

    def test_is_connected_false(self):
        assert self._make(connected=False).is_connected() is False

    def test_filter_by_event_type(self):
        events = [
            {"event_type": "state_changed", "entity_id": "update.core"},
            {"event_type": "automation_triggered", "entity_id": "automation.x"},
        ]
        fake = self._make(events=events)
        result = fake.get_events(event_type="state_changed")
        assert len(result) == 1
        assert result[0]["entity_id"] == "update.core"

    def test_filter_by_entity_id(self):
        events = [
            {"event_type": "state_changed", "entity_id": "update.core"},
            {"event_type": "state_changed", "entity_id": "update.supervisor"},
        ]
        fake = self._make(events=events)
        result = fake.get_events(entity_id="update.core")
        assert len(result) == 1

    def test_limit_applied(self):
        events = [
            {"event_type": "state_changed", "entity_id": f"e.{i}"} for i in range(10)
        ]
        fake = self._make(events=events)
        result = fake.get_events(limit=3)
        assert len(result) == 3

    def test_no_filter_returns_all(self):
        events = [{"event_type": "x"}, {"event_type": "y"}]
        fake = self._make(events=events)
        assert len(fake.get_events()) == 2


# ---------------------------------------------------------------------------
# HAEventSubscriber._handle_event
# ---------------------------------------------------------------------------


class TestHandleEvent:
    def _make_subscriber(self):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        return HAEventSubscriber("host", 8123, "token", buffer_size=10)

    def test_state_changed_update_entity_buffered(self):
        sub = self._make_subscriber()
        msg = _event_msg(
            1,
            {"entity_id": "update.core", "new_state": {"state": "on"}},
            event_type_override="state_changed",
        )
        sub._handle_event(msg, _make_sub_id_map())
        assert len(sub._buffer) == 1
        assert sub._buffer[0]["event_type"] == "state_changed"
        assert sub._buffer[0]["entity_id"] == "update.core"

    def test_state_changed_normal_sensor_filtered_out(self):
        sub = self._make_subscriber()
        msg = _event_msg(
            1,
            {"entity_id": "sensor.temp", "new_state": {"state": "22.5"}},
            event_type_override="state_changed",
        )
        sub._handle_event(msg, _make_sub_id_map())
        assert len(sub._buffer) == 0

    def test_repairs_registry_buffered(self):
        sub = self._make_subscriber()
        msg = _event_msg(2, {"issue_id": "abc-123"})
        sub._handle_event(msg, _make_sub_id_map())
        assert len(sub._buffer) == 1
        assert sub._buffer[0]["event_type"] == "repairs_issue_registry_updated"

    def test_automation_triggered_buffered(self):
        sub = self._make_subscriber()
        msg = _event_msg(3, {"entity_id": "automation.morning", "name": "Morning"})
        sub._handle_event(msg, _make_sub_id_map())
        assert sub._buffer[0]["event_type"] == "automation_triggered"
        assert sub._buffer[0]["entity_id"] == "automation.morning"

    def test_persistent_notification_buffered(self):
        sub = self._make_subscriber()
        msg = _event_msg(4, {"notification_id": "n_1", "type": "added"})
        sub._handle_event(msg, _make_sub_id_map())
        assert sub._buffer[0]["event_type"] == "persistent_notification_event"
        assert sub._buffer[0]["entity_id"] == "n_1"

    def test_timestamp_recorded(self):
        sub = self._make_subscriber()
        before = time.time()
        msg = _event_msg(2, {})
        sub._handle_event(msg, _make_sub_id_map())
        after = time.time()
        ts = sub._buffer[0]["time"]
        assert before <= ts <= after

    def test_unknown_sub_id_buffered_with_unknown_type(self):
        sub = self._make_subscriber()
        msg = {"id": 99, "type": "event", "event": {"data": {}}}
        sub._handle_event(msg, _make_sub_id_map())
        # unknown sub_id → still buffered (not filtered); state_changed filter only
        assert sub._buffer[0]["event_type"] == "unknown"


# ---------------------------------------------------------------------------
# Ring buffer bound
# ---------------------------------------------------------------------------


class TestRingBufferBound:
    def test_buffer_does_not_exceed_maxlen(self):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        sub = HAEventSubscriber("h", 8123, "t", buffer_size=5)
        sub_map = {2: "repairs_issue_registry_updated"}
        for i in range(10):
            sub._handle_event(
                {"id": 2, "type": "event", "event": {"data": {"i": i}}}, sub_map
            )
        assert len(sub._buffer) == 5

    def test_oldest_events_dropped(self):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        sub = HAEventSubscriber("h", 8123, "t", buffer_size=3)
        sub_map = {3: "automation_triggered"}
        for i in range(5):
            sub._handle_event(
                {
                    "id": 3,
                    "type": "event",
                    "event": {"data": {"entity_id": f"automation.x{i}"}},
                },
                sub_map,
            )
        entity_ids = [e.get("entity_id") for e in sub._buffer]
        # oldest two dropped; keep x2, x3, x4
        assert entity_ids == ["automation.x2", "automation.x3", "automation.x4"]


# ---------------------------------------------------------------------------
# get_events filtering on HAEventSubscriber
# ---------------------------------------------------------------------------


class TestGetEvents:
    def _make(self, buffer_size=100):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        sub = HAEventSubscriber("h", 8123, "t", buffer_size=buffer_size)
        sub._connected = True
        return sub

    def _add_event(self, sub, event_type: str, entity_id: str | None = None):
        entry = {"event_type": event_type, "time": time.time(), "data": {}}
        if entity_id:
            entry["entity_id"] = entity_id
        sub._buffer.append(entry)

    def test_returns_newest_first(self):
        sub = self._make()
        self._add_event(sub, "automation_triggered", "automation.a1")
        self._add_event(sub, "automation_triggered", "automation.a2")
        result = sub.get_events(event_type="automation_triggered")
        # newest first
        assert result[0]["entity_id"] == "automation.a2"
        assert result[1]["entity_id"] == "automation.a1"

    def test_filter_event_type(self):
        sub = self._make()
        self._add_event(sub, "state_changed", "update.core")
        self._add_event(sub, "automation_triggered", "automation.x")
        result = sub.get_events(event_type="state_changed")
        assert len(result) == 1
        assert result[0]["entity_id"] == "update.core"

    def test_filter_entity_id(self):
        sub = self._make()
        self._add_event(sub, "state_changed", "update.core")
        self._add_event(sub, "state_changed", "update.supervisor")
        result = sub.get_events(entity_id="update.core")
        assert len(result) == 1

    def test_limit_applied(self):
        sub = self._make()
        for i in range(10):
            self._add_event(sub, "repairs_issue_registry_updated")
        assert len(sub.get_events(limit=3)) == 3

    def test_empty_buffer_returns_empty(self):
        sub = self._make()
        assert sub.get_events() == []


# ---------------------------------------------------------------------------
# Reconnect / backoff (scripted mock WS)
# ---------------------------------------------------------------------------


class _MockWsCtx:
    """Async context manager wrapping a scripted websocket."""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *_):
        pass


class _ScriptedWs:
    """Minimal fake websocket for scripted auth + subscribe sequences."""

    def __init__(self, messages: list[dict]) -> None:
        self._out = [json.dumps(m) for m in messages]
        self._pos = 0
        self.sent: list[dict] = []

    async def recv(self) -> str:
        msg = self._out[self._pos]
        self._pos += 1
        return msg

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pos >= len(self._out):
            raise StopAsyncIteration
        msg = self._out[self._pos]
        self._pos += 1
        return msg


def _auth_ok_sequence() -> list[dict]:
    """Auth + successful subscribe_result messages for all _SUB_EVENTS + notification."""
    return [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"type": "result", "id": 1, "success": True},
        {"type": "result", "id": 2, "success": True},
        {"type": "result", "id": 3, "success": True},
        {"type": "result", "id": 4, "success": True},
        {"type": "result", "id": 5, "success": True},
    ]


class TestSubscriberConnectFlow:
    def _make_subscriber(self, buffer_size=50):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        return HAEventSubscriber("host", 8123, "tok", buffer_size=buffer_size)

    def test_connected_flag_set_after_successful_auth(self):
        """After successful auth + subscribe, is_connected() returns True."""
        sub = self._make_subscriber()
        ws = _ScriptedWs(_auth_ok_sequence())

        async def _run():
            with patch("websockets.connect", return_value=_MockWsCtx(ws)):
                await sub._connect_and_subscribe()

        asyncio.run(_run())
        assert sub.is_connected() is True

    def test_event_buffered_after_subscribe(self):
        """An event message arriving after subscribe is stored in the buffer."""
        sub = self._make_subscriber()
        messages = _auth_ok_sequence() + [
            {
                "id": 3,
                "type": "event",
                "event": {
                    "event_type": "automation_triggered",
                    "data": {"entity_id": "automation.morning"},
                },
            }
        ]
        ws = _ScriptedWs(messages)

        async def _run():
            with patch("websockets.connect", return_value=_MockWsCtx(ws)):
                await sub._connect_and_subscribe()

        asyncio.run(_run())
        assert len(sub._buffer) == 1
        assert sub._buffer[0]["event_type"] == "automation_triggered"

    def test_auth_invalid_raises(self):
        sub = self._make_subscriber()
        messages = [{"type": "auth_required"}, {"type": "auth_invalid"}]
        ws = _ScriptedWs(messages)

        async def _run():
            with patch("websockets.connect", return_value=_MockWsCtx(ws)):
                await sub._connect_and_subscribe()

        with pytest.raises(RuntimeError, match="authentication failed"):
            asyncio.run(_run())

    def test_unexpected_auth_type_raises(self):
        sub = self._make_subscriber()
        messages = [{"type": "auth_required"}, {"type": "mystery"}]
        ws = _ScriptedWs(messages)

        async def _run():
            with patch("websockets.connect", return_value=_MockWsCtx(ws)):
                await sub._connect_and_subscribe()

        with pytest.raises(RuntimeError, match="Unexpected auth response"):
            asyncio.run(_run())


class TestReconnectBackoff:
    def test_run_forever_retries_after_error(self):
        """run_forever retries once on a connection error (short-circuits with CancelledError)."""
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        sub = HAEventSubscriber("host", 8123, "tok")
        call_count = 0

        async def _fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("connection refused")
            # Second call cancels the loop
            raise asyncio.CancelledError

        sub._connect_and_subscribe = _fake_connect  # type: ignore[method-assign]

        async def _run():
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await sub.run_forever()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())

        assert call_count == 2

    def test_cancelled_error_propagates(self):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        sub = HAEventSubscriber("host", 8123, "tok")

        async def _raise_cancelled():
            raise asyncio.CancelledError

        sub._connect_and_subscribe = _raise_cancelled  # type: ignore[method-assign]

        async def _run():
            await sub.run_forever()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())


# ---------------------------------------------------------------------------
# Protocol structural check
# ---------------------------------------------------------------------------


class TestHAEventSubscriberProtocol:
    def test_fake_satisfies_protocol(self):
        """FakeHAEventSubscriber satisfies HAEventSubscriberProtocol structurally."""
        from utils.ha.ha_event_subscriber import FakeHAEventSubscriber
        from interfaces import HAEventSubscriberProtocol
        from typing import runtime_checkable, Protocol

        # Verify required methods are present
        fake = FakeHAEventSubscriber()
        assert callable(fake.get_events)
        assert callable(fake.is_connected)

    def test_real_subscriber_has_required_methods(self):
        from utils.ha.ha_event_subscriber import HAEventSubscriber

        sub = HAEventSubscriber("h", 8123, "t")
        assert callable(sub.get_events)
        assert callable(sub.is_connected)
        assert callable(sub.run_forever)
