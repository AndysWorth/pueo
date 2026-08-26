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
        """config_not_found on a named url_path also returns {} silently."""
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
        result = asyncio.run(client.get_lovelace_config("my-dashboard"))
        assert result == {}

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
