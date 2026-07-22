"""Async HTTP client for the NetAlertX REST API (v26.7.1+).

All calls use Bearer token auth.  Pass an ``httpx.AsyncClient`` constructed
with a mock transport in tests; omit it in production (a fresh client is
created per call group).

Endpoint reference (v26.7.1):
  GET  /devices               — all devices
  GET  /events                — all events
  GET  /metrics               — Prometheus plain-text metrics
  POST /graphql               — GraphQL (used here for all-settings query)
  GET  /settings/<key>        — single setting value
  POST /nettools/trigger-scan — queue a plugin scan (default: ARPSCAN)
  GET  /health                — system health (returned by get_about)
"""

from __future__ import annotations

import json

import httpx

_GRAPHQL_ALL_SETTINGS = "{ settings { settings { setKey setValue } count } }"


class NetAlertXAPIClient:
    """Thin async wrapper around the NetAlertX REST API."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = api_token
        self._client = http_client  # None → fresh client per request group

    # ── internal helpers ────────────────────────────────────────────────────────

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str) -> httpx.Response:
        if self._client is not None:
            return await self._client.get(f"{self._base}{path}", headers=self._auth())
        async with httpx.AsyncClient(timeout=30) as c:
            return await c.get(f"{self._base}{path}", headers=self._auth())

    async def _post(self, path: str, **kwargs) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                f"{self._base}{path}", headers=self._auth(), **kwargs
            )
        async with httpx.AsyncClient(timeout=30) as c:
            return await c.post(f"{self._base}{path}", headers=self._auth(), **kwargs)

    # ── public API ──────────────────────────────────────────────────────────────

    async def get_devices(self) -> list[dict]:
        """Return all devices from GET /devices."""
        resp = await self._get("/devices")
        resp.raise_for_status()
        return resp.json().get("devices", [])

    async def get_events(self) -> list[dict]:
        """Return all events from GET /events."""
        resp = await self._get("/events")
        resp.raise_for_status()
        return resp.json().get("events", [])

    async def get_metrics(self) -> dict[str, float]:
        """Return aggregate Prometheus metrics from GET /metrics as a dict.

        Only scalar (non-labeled) metrics are included; per-device labeled
        entries are skipped.  Keys match the metric names without the
        ``netalertx_`` prefix (e.g. ``connected_devices``).
        """
        resp = await self._get("/metrics")
        resp.raise_for_status()
        return _parse_prometheus_text(resp.text)

    async def get_settings(self) -> dict[str, str]:
        """Return all settings as {setKey: setValue} via POST /graphql."""
        payload = {"query": _GRAPHQL_ALL_SETTINGS}
        resp = await self._post("/graphql", json=payload)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("settings", {}).get("settings", [])
        return {item["setKey"]: item.get("setValue", "") for item in items}

    async def trigger_scan(self, scan_type: str = "ARPSCAN") -> None:
        """Queue a network scan via POST /nettools/trigger-scan."""
        resp = await self._post("/nettools/trigger-scan", json={"type": scan_type})
        resp.raise_for_status()

    async def get_about(self) -> dict:
        """Return system health info from GET /health."""
        resp = await self._get("/health")
        resp.raise_for_status()
        return resp.json()

    async def update_device_column(
        self, mac: str, column_name: str, column_value: str
    ) -> None:
        """Write one device column via POST /device/<mac>/update-column."""
        resp = await self._post(
            f"/device/{mac}/update-column",
            json={"columnName": column_name, "columnValue": column_value},
        )
        resp.raise_for_status()

    async def lock_device_field(
        self, mac: str, field_name: str, lock: bool = True
    ) -> None:
        """Lock or unlock a device field via POST /device/<mac>/field/lock."""
        if not mac:
            raise ValueError("lock_device_field requires a non-empty MAC address")
        resp = await self._post(
            f"/device/{mac}/field/lock",
            json={"fieldName": field_name, "lock": lock},
        )
        resp.raise_for_status()


# ── helpers ─────────────────────────────────────────────────────────────────────


def _parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse scalar Prometheus metric lines into a plain dict.

    Lines without labels (e.g. ``netalertx_connected_devices 31``) are
    included; labeled lines are skipped.
    """
    result: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            continue
        parts = line.split()
        if len(parts) == 2:
            name = parts[0].removeprefix("netalertx_")
            try:
                result[name] = float(parts[1])
            except ValueError:
                pass
    return result
