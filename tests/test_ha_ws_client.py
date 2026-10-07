"""Tests for utils/ha/ha_ws_client.py — graceful handling of known HA error codes."""

from __future__ import annotations

import asyncio
import json

import pytest

from utils.ha.ha_ws_client import HAWebSocketClient


# ---------------------------------------------------------------------------
# Helpers: mock WebSocket-level transport
# ---------------------------------------------------------------------------


class _MockWs:
    """Minimal fake websocket that drives a pre-scripted message sequence."""

    def __init__(self, messages: list[dict]) -> None:
        self._out = [json.dumps(m) for m in messages]
        self._pos = 0
        self._sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        msg = self._out[self._pos]
        self._pos += 1
        return msg

    async def send(self, data: str) -> None:
        self._sent.append(data)

    async def close(self) -> None:
        self.closed = True


def _make_client() -> HAWebSocketClient:  # pragma: no cover
    return HAWebSocketClient(host="ha.local", port=8123, token="tok")


def _patch_connect(monkeypatch, mock_ws: _MockWs):
    """Patch _connect_and_auth so it returns the mock websocket."""

    async def _fake_connect_and_auth(self):  # type: ignore[override]
        return mock_ws

    monkeypatch.setattr(HAWebSocketClient, "_connect_and_auth", _fake_connect_and_auth)


# ---------------------------------------------------------------------------
# get_lovelace_config — config_not_found returns {} without raising
# ---------------------------------------------------------------------------


class TestGetLovelaceConfigNotFound:
    def test_config_not_found_returns_empty_dict(self, monkeypatch):
        """HA in auto-mode returns config_not_found; must return {} not raise."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "config_not_found",
                        "message": "No config found.",
                    },
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_lovelace_config(None))
        assert result == {}
        assert ws.closed

    def test_config_not_found_for_named_dashboard(self, monkeypatch):
        """config_not_found on a named url_path raises LovelaceConfigNotFound."""
        from utils.ha.ha_ws_client import LovelaceConfigNotFound

        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "config_not_found",
                        "message": "No config found.",
                    },
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        with pytest.raises(LovelaceConfigNotFound, match="my-dashboard"):
            asyncio.run(client.get_lovelace_config("my-dashboard"))

    def test_other_errors_still_raise(self, monkeypatch):
        """Non-config_not_found failures must still raise RuntimeError."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unauthorized", "message": "Unauthorized."},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        with pytest.raises(RuntimeError, match="lovelace/config request failed"):
            asyncio.run(client.get_lovelace_config(None))

    def test_success_returns_result(self, monkeypatch):
        """Happy-path: success=True returns the result dict."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": {"views": [{"title": "Home"}]},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_lovelace_config(None))
        assert result == {"views": [{"title": "Home"}]}


# ---------------------------------------------------------------------------
# get_config_entries — tries config_entries/get, then two legacy fallbacks
# ---------------------------------------------------------------------------

_UNKNOWN_CMD = {
    "type": "result",
    "success": False,
    "error": {"code": "unknown_command", "message": "Unknown command."},
}


class TestGetConfigEntriesCommandFallback:
    def test_modern_command_succeeds(self, monkeypatch):
        """config_entries/get (HA 2026.x) returns entries on first try."""
        entries = [{"entry_id": "abc", "domain": "zha", "state": "loaded"}]
        ws = _MockWs(
            [
                {**_UNKNOWN_CMD, "id": 1},
                {"id": 1, "type": "result", "success": True, "result": entries},
            ]
        )
        # Override: first msg succeeds immediately
        ws = _MockWs([{"id": 1, "type": "result", "success": True, "result": entries}])
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_config_entries())
        assert result == entries
        cmds = [json.loads(s) for s in ws._sent]
        assert cmds[0]["type"] == "config_entries/get"
        assert len(cmds) == 1

    def test_falls_back_through_all_three_commands(self, monkeypatch):
        """All three commands tried in order; succeeds on the third."""
        entries = [{"entry_id": "x", "domain": "mqtt", "state": "loaded"}]
        ws = _MockWs(
            [
                {**_UNKNOWN_CMD, "id": 1},
                {**_UNKNOWN_CMD, "id": 2},
                {"id": 3, "type": "result", "success": True, "result": entries},
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_config_entries())
        assert result == entries
        cmds = [json.loads(s) for s in ws._sent]
        assert [c["type"] for c in cmds] == [
            "config_entries/get",
            "config/config_entries/all",
            "config_entries/list",
        ]

    def test_all_commands_unknown_raises(self, monkeypatch):
        """If all three commands return unknown_command, RuntimeError is raised."""
        ws = _MockWs(
            [
                {**_UNKNOWN_CMD, "id": 1},
                {**_UNKNOWN_CMD, "id": 2},
                {**_UNKNOWN_CMD, "id": 3},
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        with pytest.raises(RuntimeError, match="no supported WebSocket command found"):
            asyncio.run(client.get_config_entries())

    def test_non_unknown_command_error_raises_immediately(self, monkeypatch):
        """Errors other than unknown_command raise without trying the next command."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unauthorized", "message": "Unauthorized."},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        with pytest.raises(RuntimeError, match="Config entries request failed"):
            asyncio.run(client.get_config_entries())
        assert len(ws._sent) == 1

    def test_success_filters_to_loaded_entries(self, monkeypatch):
        """Only entries with state=loaded are returned."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": [
                        {"entry_id": "a", "state": "loaded"},
                        {"entry_id": "b", "state": "not_loaded"},
                    ],
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_config_entries())
        assert len(result) == 1
        assert result[0]["entry_id"] == "a"

    def test_second_command_succeeds(self, monkeypatch):
        """config/config_entries/all (HA 2022–2025) works on second try."""
        entries = [{"entry_id": "y", "state": "loaded"}]
        ws = _MockWs(
            [
                {**_UNKNOWN_CMD, "id": 1},
                {"id": 2, "type": "result", "success": True, "result": entries},
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_config_entries())
        assert result == entries
        cmds = [json.loads(s) for s in ws._sent]
        assert cmds[1]["type"] == "config/config_entries/all"


# ---------------------------------------------------------------------------
# _call helper
# ---------------------------------------------------------------------------


class TestCallHelper:
    def test_success_returns_result(self, monkeypatch):
        """_call returns result on success."""
        ws = _MockWs(
            [{"id": 1, "type": "result", "success": True, "result": [{"a": 1}]}]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client._call("config/device_registry/list"))
        assert result == [{"a": 1}]
        assert ws.closed

    def test_failure_raises(self, monkeypatch):
        """_call raises RuntimeError when success=False."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unauthorized"},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        with pytest.raises(RuntimeError, match="request failed"):
            asyncio.run(client._call("config/device_registry/list"))
        assert ws.closed

    def test_payload_forwarded(self, monkeypatch):
        """Extra kwargs are serialised into the outgoing message."""
        ws = _MockWs([{"id": 1, "type": "result", "success": True, "result": []}])
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        asyncio.run(client._call("call_service", domain="light", service="turn_on"))
        sent = json.loads(ws._sent[0])
        assert sent["domain"] == "light"
        assert sent["service"] == "turn_on"


# ---------------------------------------------------------------------------
# get_system_log
# ---------------------------------------------------------------------------


class TestGetSystemLog:
    def test_returns_list_on_success(self, monkeypatch):
        """get_system_log returns the result list from system_log/list."""
        entries = [
            {
                "logger": "homeassistant.loader",
                "level": "ERROR",
                "message": ["Error loading component"],
                "timestamp": 1000.0,
                "first_occurred": 900.0,
                "count": 2,
            }
        ]
        ws = _MockWs([{"id": 1, "type": "result", "success": True, "result": entries}])
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_system_log())
        assert result == entries
        assert ws.closed

    def test_empty_list_on_non_list_result(self, monkeypatch):
        """get_system_log returns [] when HA returns a non-list result."""
        ws = _MockWs([{"id": 1, "type": "result", "success": True, "result": None}])
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.get_system_log())
        assert result == []

    def test_sends_correct_command(self, monkeypatch):
        """get_system_log sends type=system_log/list."""
        ws = _MockWs([{"id": 1, "type": "result", "success": True, "result": []}])
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        asyncio.run(client.get_system_log())
        sent = json.loads(ws._sent[0])
        assert sent["type"] == "system_log/list"


# ---------------------------------------------------------------------------
# list_orphaned_database_entities — uses call_service with return_response
# ---------------------------------------------------------------------------


class TestListOrphanedDatabaseEntities:
    def test_happy_path_returns_response(self, monkeypatch):
        """service_not_found returns {} without raising."""
        response = {"count": 2, "entities": ["sensor.old_a", "sensor.old_b"]}
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": {
                        "context": {"id": "abc"},
                        "response": response,
                    },
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.list_orphaned_database_entities())
        assert result == response

    def test_service_not_found_returns_empty(self, monkeypatch):
        """service_not_found returns {} without raising."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "service_not_found",
                        "message": "No such service.",
                    },
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.list_orphaned_database_entities())
        assert result == {}

    def test_unknown_error_returns_empty(self, monkeypatch):
        """unknown_error also returns {} gracefully."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_error", "message": "err"},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        result = asyncio.run(client.list_orphaned_database_entities())
        assert result == {}

    def test_other_errors_raise(self, monkeypatch):
        """Unexpected errors still raise RuntimeError."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unauthorized", "message": "Unauthorized."},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        with pytest.raises(
            RuntimeError, match="list_orphaned_database_entities failed"
        ):
            asyncio.run(client.list_orphaned_database_entities())

    def test_sends_call_service_with_return_response(self, monkeypatch):
        """The outgoing WS message uses call_service with return_response=True."""
        ws = _MockWs(
            [
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": {"response": {}},
                }
            ]
        )
        _patch_connect(monkeypatch, ws)
        client = _make_client()
        asyncio.run(client.list_orphaned_database_entities())
        sent = json.loads(ws._sent[0])
        assert sent["type"] == "call_service"
        assert sent["domain"] == "homeassistant"
        assert sent["service"] == "list_orphaned_database_entities"
        assert sent.get("return_response") is True


class TestFakeHAWebSocketClientOrphanedEntities:
    """FakeHAWebSocketClient correctly handles the orphaned_entities parameter."""

    def test_returns_orphaned_entities(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        data = {"count": 3, "entities": ["a", "b", "c"]}
        fake = FakeHAWebSocketClient(orphaned_entities=data)
        result = asyncio.run(fake.list_orphaned_database_entities())
        assert result == data
        assert "list_orphaned_database_entities" in fake.calls

    def test_default_empty_dict(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient()
        result = asyncio.run(fake.list_orphaned_database_entities())
        assert result == {}


# ---------------------------------------------------------------------------
# _connect_and_auth — socket is closed on any auth failure (#769 FD leak)
# ---------------------------------------------------------------------------


class _RaisingWs(_MockWs):
    def __init__(self, exc: BaseException) -> None:
        super().__init__([])
        self._exc = exc

    async def recv(self) -> str:
        raise self._exc


def _patch_websockets_connect(monkeypatch, ws: _MockWs) -> None:
    import websockets

    async def _connect(uri, **kwargs):
        return ws

    monkeypatch.setattr(websockets, "connect", _connect)


class TestConnectAndAuthClosesOnError:
    def test_timeout_during_auth_closes_socket(self, monkeypatch):
        ws = _RaisingWs(asyncio.TimeoutError())
        _patch_websockets_connect(monkeypatch, ws)
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_make_client()._connect_and_auth())
        assert ws.closed is True

    def test_connection_closed_during_auth_closes_socket(self, monkeypatch):
        from websockets.exceptions import ConnectionClosedError

        ws = _RaisingWs(ConnectionClosedError(None, None))
        _patch_websockets_connect(monkeypatch, ws)
        with pytest.raises(ConnectionClosedError):
            asyncio.run(_make_client()._connect_and_auth())
        assert ws.closed is True

    def test_auth_invalid_closes_socket(self, monkeypatch):
        ws = _MockWs([{"type": "auth_required"}, {"type": "auth_invalid"}])
        _patch_websockets_connect(monkeypatch, ws)
        with pytest.raises(RuntimeError, match="authentication failed"):
            asyncio.run(_make_client()._connect_and_auth())
        assert ws.closed is True

    def test_auth_ok_leaves_socket_open(self, monkeypatch):
        ws = _MockWs([{"type": "auth_required"}, {"type": "auth_ok"}])
        _patch_websockets_connect(monkeypatch, ws)
        result = asyncio.run(_make_client()._connect_and_auth())
        assert result is ws
        assert ws.closed is False


# ---------------------------------------------------------------------------
# TestFakeHAWebSocketClientStatistics
# ---------------------------------------------------------------------------


class TestFakeHAWebSocketClientStatistics:
    """FakeHAWebSocketClient correctly handles get_statistics."""

    def test_returns_empty_dict_by_default(self):
        import asyncio
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient()
        result = asyncio.run(
            fake.get_statistics(
                statistic_ids=["sensor.energy"],
                start_time="2026-01-01 00:00:00",
                end_time=None,
                period="hour",
                types=["mean"],
            )
        )
        assert result == {}

    def test_returns_preset_data(self):
        import asyncio
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        data = {
            "sensor.energy": [
                {"start": 1700000000000, "mean": 1.5},
                {"start": 1700003600000, "mean": 2.3},
            ]
        }
        fake = FakeHAWebSocketClient()
        fake.set_statistics(data)
        result = asyncio.run(
            fake.get_statistics(
                statistic_ids=["sensor.energy"],
                start_time="2026-01-01 00:00:00",
                end_time="2026-01-02 00:00:00",
                period="hour",
                types=["mean"],
            )
        )
        assert result == data

    def test_records_call(self):
        import asyncio
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient()
        asyncio.run(
            fake.get_statistics(
                statistic_ids=["sensor.energy", "sensor.water"],
                start_time="2026-01-01 00:00:00",
                end_time=None,
                period="day",
                types=["sum"],
            )
        )
        assert any("get_statistics" in c for c in fake.calls)
        assert any("day" in c for c in fake.calls)
