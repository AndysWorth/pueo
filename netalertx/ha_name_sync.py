"""NetAlertX ↔ HA device name synchronisation — Cases 1–4 (items 13 and 14).

Reads friendly names from three HA sources (priority: Source 1 wins):
  1. /config/.storage/core.device_registry  (user-set or auto names)
  2. /config/known_devices.yaml             (deprecated but common in many installs)
  3. GET /api/states device_tracker.*        (lowest priority)

sync_names() applies all four cases:
  Case 1 — blank/auto-generated devName + HA name known → write devName + lock
  Case 2 — devName already matches HA name (case-insensitive) → lock only
  Case 3 — devName non-empty and differs from HA name → single HITL; write+lock on approval
  Case 4 — no HA name found:
    Step A — existing plausible devName → lock and keep
    Step B — reverse DNS lookup → write hostname + lock if usable
    Step C — still unnamed → single LOW-risk HITL listing unnamed devices

sync_device(mac) runs a single MAC through Cases 1–4; called by the health monitor
when a device with devIsNew or blank devName is detected.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import httpx
import yaml
from pydantic import BaseModel

from config import (
    HA_API_TOKEN,
    HA_HOST,
    NETALERTX_AUTO_GENERATED_NAME_PATTERNS,
)
from utils.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from netalertx.api_client import NetAlertXAPIClient
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.ha_name_sync")


def _normalize_mac(mac: str) -> str:
    """Normalize MAC to uppercase colon-delimited (AA:BB:CC:DD:EE:FF)."""
    raw = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(raw) != 12:
        return mac.upper()
    return ":".join(raw[i : i + 2].upper() for i in range(0, 12, 2))


def _matches_auto_pattern(name: str, patterns: list[str]) -> bool:
    return not name or any(re.search(p, name) for p in patterns)


class ConflictEntry(BaseModel):
    mac: str
    ha_name: str
    netalertx_name: str


class UnnamedEntry(BaseModel):
    mac: str
    vendor: str
    last_ip: str


class SyncReport(BaseModel):
    written: list[str] = []
    locked: list[str] = []
    conflicted: list[ConflictEntry] = []
    unnamed: list[UnnamedEntry] = []
    reverse_dns: list[str] = []


class HaNameSync:
    def __init__(
        self,
        ssh_client: "SSHClientProtocol",
        api_client: "NetAlertXAPIClient",
        gate: "AutonomyGate",
        notifier: "NotifierProtocol",
        ha_host: str = HA_HOST,
        ha_api_token: str = HA_API_TOKEN,
        auto_patterns: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._ssh = ssh_client
        self._api = api_client
        self._gate = gate
        self._notifier = notifier
        self._ha_host = ha_host
        self._ha_token = ha_api_token
        self._patterns: list[str] = (
            auto_patterns
            if auto_patterns is not None
            else NETALERTX_AUTO_GENERATED_NAME_PATTERNS
        )
        self._http = http_client

    async def _ha_get(self, path: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._ha_token}"}
        url = f"http://{self._ha_host}:8123{path}"
        if self._http is not None:
            return await self._http.get(url, headers=headers)
        async with httpx.AsyncClient(timeout=30) as c:
            return await c.get(url, headers=headers)

    async def read_ha_names(self) -> dict[str, str]:
        """Return a {normalized-MAC: friendly-name} map merged from all three sources."""
        names: dict[str, str] = {}

        # Source 3 — lowest priority: device_tracker.* states via HA REST API
        try:
            resp = await self._ha_get("/api/states")
            resp.raise_for_status()
            for state in resp.json():
                if not state.get("entity_id", "").startswith("device_tracker."):
                    continue
                attrs = state.get("attributes", {})
                mac = attrs.get("mac_address", "")
                fname = attrs.get("friendly_name", "")
                if mac and fname:
                    names[_normalize_mac(mac)] = fname
        except Exception as exc:
            log.warning("ha_names_source3_failed", error=str(exc))
        log.info("ha_names_source3", count=len(names))

        # Source 2 — known_devices.yaml (deprecated per HA roadmap but present in many installs)
        src2: dict[str, str] = {}
        try:
            content = await self._ssh.read_file("/config/known_devices.yaml")
            parsed = yaml.safe_load(content) or {}
            for _key, device in parsed.items():
                if not isinstance(device, dict):
                    continue
                mac = device.get("mac", "")
                fname = device.get("name", "")
                if mac and fname:
                    src2[_normalize_mac(mac)] = fname
            names.update(src2)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("ha_names_source2_failed", error=str(exc))
        log.info("ha_names_source2", count=len(src2))

        # Source 1 — highest priority: core.device_registry (user-assigned names)
        src1: dict[str, str] = {}
        try:
            content = await self._ssh.read_file("/config/.storage/core.device_registry")
            data = json.loads(content)
            for device in data.get("data", {}).get("devices", []):
                fname = device.get("name_by_user") or device.get("name", "")
                if not fname:
                    continue
                for conn_type, conn_val in device.get("connections", []):
                    if conn_type == "mac":
                        src1[_normalize_mac(conn_val)] = fname
            names.update(src1)
        except Exception as exc:
            log.warning("ha_names_source1_failed", error=str(exc))
        log.info("ha_names_source1", count=len(src1))

        return names

    async def _reverse_dns(self, ip: str) -> str:
        """Run 'host <ip>' over SSH and return a usable hostname, or ''."""
        if not ip:
            return ""
        try:
            _, stdout, _ = await self._ssh.run(f"host {ip}")
            for line in stdout.splitlines():
                if "domain name pointer" in line:
                    hostname = line.split("domain name pointer")[-1].strip().rstrip(".")
                    if (
                        hostname
                        and not hostname.endswith(".in-addr.arpa")
                        and not _matches_auto_pattern(hostname, self._patterns)
                    ):
                        return hostname
        except Exception as exc:
            log.warning("reverse_dns_failed", ip=ip, error=str(exc))
        return ""

    async def _process_one(
        self, mac: str, dev: dict, ha_names: dict[str, str], report: SyncReport
    ) -> None:
        """Apply Cases 1, 2, 4A, 4B for one device; collect Cases 3 and 4C into report."""
        dev_name: str = dev.get("devName", "") or ""
        ha_name = ha_names.get(mac, "")

        if ha_name:
            if _matches_auto_pattern(dev_name, self._patterns):
                # Case 1: blank/auto-generated name + HA name known → write + lock
                await self._api.update_device_column(mac, "devName", ha_name)
                await self._api.lock_device_field(mac, "devName", lock=True)
                report.written.append(mac)
                log.info("name_written", mac=mac, ha_name=ha_name)
            elif dev_name.lower() == ha_name.lower():
                # Case 2: already matches → lock idempotently
                await self._api.lock_device_field(mac, "devName", lock=True)
                report.locked.append(mac)
                log.info("name_already_correct", mac=mac)
            else:
                # Case 3: conflict → collect for batch HITL after the loop
                report.conflicted.append(
                    ConflictEntry(mac=mac, ha_name=ha_name, netalertx_name=dev_name)
                )
        else:
            # Case 4: no HA name — try Steps A → B → C
            if dev_name and not _matches_auto_pattern(dev_name, self._patterns):
                # Step A: plausible existing name → lock and keep it
                await self._api.lock_device_field(mac, "devName", lock=True)
                report.locked.append(mac)
                log.info("hostname_plugin_name_kept", mac=mac, dev_name=dev_name)
            else:
                # Step B: reverse DNS
                rdns_name = await self._reverse_dns(dev.get("devLastIP", ""))
                if rdns_name:
                    await self._api.update_device_column(mac, "devName", rdns_name)
                    await self._api.lock_device_field(mac, "devName", lock=True)
                    report.written.append(mac)
                    report.reverse_dns.append(mac)
                    log.info("reverse_dns_name_written", mac=mac, rdns_name=rdns_name)
                else:
                    # Step C: truly unnamed → collect for HITL
                    report.unnamed.append(
                        UnnamedEntry(
                            mac=mac,
                            vendor=dev.get("devVendor", ""),
                            last_ip=dev.get("devLastIP", ""),
                        )
                    )

    async def _resolve_conflicts(self, report: SyncReport) -> None:
        """Issue a single MEDIUM-risk HITL for all collected conflicts; write+lock on approval."""
        from utils.autonomy import RiskLevel

        conflict_table = "\n".join(
            f"  {e.mac} | {e.ha_name} | {e.netalertx_name}" for e in report.conflicted
        )
        body = (
            f"Name conflicts ({len(report.conflicted)}):\n"
            f"  MAC | HA name | NetAlertX name\n"
            f"{conflict_table}\n\n"
            "Approve to overwrite NetAlertX names with HA names."
        )
        approved = await self._gate.require_approval(
            subject="Pueo: NetAlertX name conflicts",
            body=body,
            payload={"notification_id": "ha_name_sync_conflicts"},
            notifier=self._notifier,
            risk=RiskLevel.MEDIUM,
        )
        if approved:
            for entry in report.conflicted:
                await self._api.update_device_column(
                    entry.mac, "devName", entry.ha_name
                )
                await self._api.lock_device_field(entry.mac, "devName", lock=True)
                report.written.append(entry.mac)
                log.info("conflict_resolved", mac=entry.mac, ha_name=entry.ha_name)
        else:
            for entry in report.conflicted:
                log.info("conflict_skipped", mac=entry.mac)

    async def _notify_unnamed(self, report: SyncReport) -> None:
        """Issue a single LOW-risk HITL listing all still-unnamed devices."""
        from utils.autonomy import RiskLevel

        unnamed_table = "\n".join(
            f"  {e.mac} | {e.vendor} | {e.last_ip}" for e in report.unnamed
        )
        body = (
            f"Unnamed devices ({len(report.unnamed)}):\n"
            f"  MAC | Vendor | Last IP\n"
            f"{unnamed_table}\n\n"
            "Name them in NetAlertX or add entries to /config/known_devices.yaml."
        )
        await self._gate.require_approval(
            subject="Pueo: Unnamed NetAlertX devices",
            body=body,
            payload={"notification_id": "ha_name_sync_unnamed"},
            notifier=self._notifier,
            risk=RiskLevel.LOW,
        )

    async def sync_names(self) -> SyncReport:
        """Sync HA names into NetAlertX — all four Cases."""
        from utils.autonomy import RiskLevel

        ha_names = await self.read_ha_names()

        if not ha_names:
            await self._gate.require_approval(
                subject="Pueo: HA has no MAC→name mappings",
                body=(
                    "HA device registry has no MAC→name mappings. "
                    "Confirm to proceed with unnamed NetAlertX devices, "
                    "or set up a network tracker integration in HA first."
                ),
                payload={"notification_id": "ha_name_sync_zero_macs"},
                notifier=self._notifier,
                risk=RiskLevel.LOW,
            )

        devices = await self._api.get_devices()
        report = SyncReport()

        for dev in devices:
            mac = _normalize_mac(dev.get("devMAC", ""))
            await self._process_one(mac, dev, ha_names, report)

        if report.conflicted:
            await self._resolve_conflicts(report)

        if report.unnamed:
            await self._notify_unnamed(report)

        log.info(
            "sync_names_complete",
            written=len(report.written),
            locked=len(report.locked),
            conflicted=len(report.conflicted),
            unnamed=len(report.unnamed),
        )
        return report

    async def sync_device(self, mac: str) -> None:
        """Targeted sync for a single MAC — called by health monitor on devIsNew or blank devName."""
        ha_names = await self.read_ha_names()
        devices = await self._api.get_devices()
        dev = next(
            (d for d in devices if _normalize_mac(d.get("devMAC", "")) == mac),
            None,
        )
        if dev is None:
            log.warning("sync_device_not_found", mac=mac)
            return

        report = SyncReport()
        await self._process_one(mac, dev, ha_names, report)

        if report.conflicted:
            await self._resolve_conflicts(report)

        if report.unnamed:
            await self._notify_unnamed(report)

        log.info(
            "sync_device_complete",
            mac=mac,
            written=len(report.written),
            locked=len(report.locked),
        )
