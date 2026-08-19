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

    async def _connect_and_auth(self):  # type: ignore[return]
        import websockets

        uri = f"ws://{self._host}:{self._port}/api/websocket"
        ws = await websockets.connect(uri)
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

    async def get_device_registry(self) -> list[dict]:
        """Authenticate and fetch config/device_registry/list via HA WebSocket API."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"Device registry request failed: {msg}")
            return msg.get("result", [])
        finally:
            await ws.close()

    async def get_persistent_notifications(self) -> list[dict]:
        """Fetch current persistent notifications via HA WebSocket API."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "persistent_notification/get"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"Persistent notification request failed: {msg}")
            return msg.get("result", [])
        finally:
            await ws.close()

    async def get_repair_issues(self) -> list[dict]:
        """Fetch current repair issues via HA WebSocket API."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "repairs/list_issues"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"Repairs list_issues request failed: {msg}")
            return msg.get("result", {}).get("issues", [])
        finally:
            await ws.close()

    async def get_config_entries(self) -> list[dict]:
        """Fetch loaded config entries via HA WebSocket config/config_entries/all."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "config/config_entries/all"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"Config entries request failed: {msg}")
            entries = msg.get("result", [])
            return [e for e in entries if e.get("state") == "loaded"]
        finally:
            await ws.close()

    async def get_entity_registry(self) -> list[dict]:
        """Fetch all entities via HA WebSocket config/entity_registry/list."""
        ws = await self._connect_and_auth()
        try:
            await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"Entity registry request failed: {msg}")
            return msg.get("result", [])
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
    ) -> None:
        self._devices: list[dict] = devices or []
        self._notifications: list[dict] = notifications or []
        self._repair_issues: list[dict] = repair_issues or []
        self._config_entries: list[dict] = config_entries or []
        self._entity_registry: list[dict] = entity_registry or []
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

    async def get_entity_registry(self) -> list[dict]:
        self.calls.append("get_entity_registry")
        return list(self._entity_registry)
