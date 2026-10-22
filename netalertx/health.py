"""NetAlertX health monitor — periodic REST API polling (item 16).

Polls get_devices() every max_scan_age_minutes minutes. Produces HealthReport
with scan freshness, device counts, and anomaly list.

Calls HaNameSync.sync_device(mac) when a device has devIsNew or blank devName.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel

from config import NETALERTX_MAX_SCAN_AGE_MINUTES
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.ha_name_sync import HaNameSync

log = get_logger("netalertx.health")


class HealthReport(BaseModel):
    last_scan_age_minutes: int
    device_counts: dict[str, int]
    anomalies: list[str]
    netalertx_version: str


def _normalize_mac(mac: str) -> str:
    import re

    raw = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(raw) != 12:
        return mac.upper()
    return ":".join(raw[i : i + 2].upper() for i in range(0, 12, 2))


def _compute_scan_age(
    devices: list[dict],
    now: datetime | None = None,
) -> int:
    """Return minutes since the most recently seen device (0 if no timestamps)."""
    _now = now or datetime.now(timezone.utc)
    newest: datetime | None = None

    for d in devices:
        ts_str = d.get("devLastConnection", "")
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            if newest is None or ts > newest:
                newest = ts
        except ValueError:
            pass

    if newest is None:
        return 0
    return max(0, int((_now - newest).total_seconds() / 60))


class NetAlertXHealthMonitor:
    """Periodic REST health poller."""

    def __init__(
        self,
        api_client: "NetAlertXAPIClient",
        ha_name_sync: "HaNameSync | None" = None,
        max_scan_age_minutes: int = NETALERTX_MAX_SCAN_AGE_MINUTES,
    ) -> None:
        self._api = api_client
        self._ha_name_sync = ha_name_sync
        self._max_scan_age_minutes = max_scan_age_minutes

    async def poll_once(self) -> HealthReport:
        """Run one poll cycle and return a HealthReport."""
        devices = await self._api.get_devices()

        scan_age = _compute_scan_age(devices)

        total = len(devices)
        online = sum(
            1
            for d in devices
            if str(d.get("devStatus", "")).lower() == "online" or d.get("devIsNew")
        )

        anomalies: list[str] = []
        if scan_age > self._max_scan_age_minutes:
            anomalies.append(
                f"Last scan is {scan_age} minutes old "
                f"(threshold: {self._max_scan_age_minutes})"
            )

        # Sync new or unnamed devices
        if self._ha_name_sync is not None:
            for d in devices:
                mac = _normalize_mac(d.get("devMac", ""))
                if mac and (d.get("devIsNew") or not d.get("devName")):
                    await self._ha_name_sync.sync_device(mac)

        try:
            about = await self._api.get_about()
            version: str = about.get("version", "unknown")
        except Exception as exc:
            log.warning("health_get_about_failed", error=str(exc))
            version = "unknown"

        report = HealthReport(
            last_scan_age_minutes=scan_age,
            device_counts={"total": total, "online": online},
            anomalies=anomalies,
            netalertx_version=version,
        )
        log.info(
            "health_report_produced",
            scan_age=scan_age,
            total=total,
            anomalies=len(anomalies),
        )
        return report

    async def run(self) -> None:
        """Continuous poll loop."""
        while True:
            await self.poll_once()
            await asyncio.sleep(self._max_scan_age_minutes * 60)
