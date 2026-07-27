"""HA WebSocket client for device registry queries and FakeHAWebSocketClient test double."""

from __future__ import annotations

import json


class HAWebSocketClient:  # pragma: no cover
    """Short-lived WebSocket client for HA device registry queries.

    Opens a connection, authenticates, fetches the requested data, then closes.
    Not suitable for long-lived subscriptions.
    """

    def __init__(self, host: str, port: int, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token

    async def get_device_registry(self) -> list[dict]:
        """Authenticate and fetch config/device_registry/list via HA WebSocket API."""
        import websockets

        uri = f"ws://{self._host}:{self._port}/api/websocket"
        async with websockets.connect(uri) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got: {msg.get('type')}")

            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))

            msg = json.loads(await ws.recv())
            if msg.get("type") == "auth_invalid":
                raise RuntimeError("HA WebSocket authentication failed")
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"Unexpected auth response: {msg.get('type')}")

            await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))

            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"Device registry request failed: {msg}")

            return msg.get("result", [])


class FakeHAWebSocketClient:
    """Test double for HAWebSocketClientProtocol."""

    def __init__(self, devices: list[dict] | None = None) -> None:
        self._devices: list[dict] = devices or []
        self.calls: list[str] = []

    async def get_device_registry(self) -> list[dict]:
        self.calls.append("get_device_registry")
        return list(self._devices)
