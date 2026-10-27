"""NetAlertX post-install reconciliation: subnet drift fix and name sync.

Runs once from main.py when a valid token is confirmed and the install is
in a done state (FULLY_OPERATIONAL or OPERATIONAL_NO_API).

Idempotent: subnet fix is a no-op when SCAN_SUBNETS is already clean;
name sync locks already-correct entries without re-writing them.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from netalertx.api_client import NetAlertXAPIClient
    from utils.agent.autonomy import AutonomyGate
    from utils.hitl.notify import NotifierProtocol

log = get_logger("netalertx.reconcile")


async def reconcile_existing_install(
    api: "NetAlertXAPIClient",
    ssh: "SSHClientProtocol",
    notifier: "NotifierProtocol",
    gate: "AutonomyGate",
    db_path: str,
    slug: str = "",
) -> None:
    """Fix subnet drift and run name sync after token is confirmed.

    Never raises — all errors are logged and swallowed so a reconcile
    failure never breaks startup.
    """
    try:
        await _fix_scan_subnets(ssh, slug)
    except Exception as exc:  # nosec B110
        log.warning("reconcile_subnet_fix_failed", error=str(exc))

    try:
        ip_names = await _build_ip_names(ssh)
    except Exception:  # nosec B110
        ip_names = {}

    try:
        await _run_name_sync(api, ssh, gate, notifier, ip_names)
    except Exception as exc:  # nosec B110
        log.warning("reconcile_name_sync_failed", error=str(exc))


# ── subnet reconciliation ────────────────────────────────────────────────────


async def _fix_scan_subnets(ssh: "SSHClientProtocol", slug: str) -> None:
    """Drop 172.16.0.0/12 overlap entries from SCAN_SUBNETS in app.conf.

    Reads app.conf, validates, and writes back if any bad entries exist.
    Does NOT restart NetAlertX; the fix takes effect on the next restart.
    """
    if not slug:
        log.debug("reconcile_subnet_skip_no_slug")
        return

    from netalertx.config_validator import validate_scan_subnets
    from netalertx.installer import _merge_app_conf
    from utils.ha.app_configs import resolve_app_config_root

    app_config_root = await resolve_app_config_root(ssh)
    conf_path = f"{app_config_root or '/app_configs'}/{slug}/config/app.conf"

    try:
        conf_text = await ssh.read_file(conf_path)
    except Exception as exc:  # nosec B110
        log.debug("reconcile_subnet_conf_unreadable", path=conf_path, error=str(exc))
        return

    issues = validate_scan_subnets(conf_text)
    if not issues:
        log.debug("reconcile_scan_subnets_clean")
        return

    new_val = _build_clean_scan_subnets(conf_text)
    if not new_val:
        log.warning("reconcile_scan_subnets_empty_after_clean", conf_path=conf_path)
        return

    merged, diff = _merge_app_conf(conf_text, {"SCAN_SUBNETS": new_val})
    await ssh.write_file(conf_path, merged)
    log.info("reconcile_scan_subnets_fixed", diff=diff, path=conf_path)


def _build_clean_scan_subnets(conf_text: str) -> str:
    """Return a SCAN_SUBNETS value string with 172.16.0.0/12 overlaps removed.

    Parses the Python-list-of-strings format that NetAlertX uses, e.g.:
      SCAN_SUBNETS=['10.0.0.0/24 eth0', '172.30.32.0/23 eth0']
    Returns '' if the key is not found.
    """
    import ipaddress

    _DOCKER_RANGE = ipaddress.ip_network("172.16.0.0/12")

    for line in conf_text.splitlines():
        if not line.strip().startswith("SCAN_SUBNETS"):
            continue
        _, _, value = line.partition("=")
        value = value.strip()
        entries = re.findall(r"'([^']+)'", value)
        clean: list[str] = []
        for entry in entries:
            cidr_m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})", entry)
            if cidr_m:
                try:
                    net = ipaddress.ip_network(cidr_m.group(1), strict=False)
                    if net.overlaps(_DOCKER_RANGE):
                        log.info("reconcile_subnet_entry_removed", entry=entry)
                        continue
                except ValueError:
                    pass
            clean.append(entry)
        return "[" + ", ".join(f"'{e}'" for e in clean) + "]"
    return ""


# ── IP-name map from config entries + device registry ───────────────────────


async def _build_ip_names(ssh: "SSHClientProtocol") -> dict[str, str]:
    """Return {ip: friendly_name} by cross-referencing config entry IPs with HA device names.

    Uses load_config_entry_hosts (entry_id → host) and the device registry
    (device → config_entries + name) to produce a weak-evidence mapping
    suitable for naming devices that have no MAC in HA.
    """
    from utils.ha.network_identity import load_config_entry_hosts

    host_by_entry = await load_config_entry_hosts(ssh)
    if not host_by_entry:
        return {}

    ip_names: dict[str, str] = {}
    try:
        raw = await ssh.read_file("/config/.storage/core.device_registry")
        data = json.loads(raw)
        for device in data.get("data", {}).get("devices", []):
            fname = device.get("name_by_user") or device.get("name", "")
            if not fname:
                continue
            for entry_id in device.get("config_entries", []):
                host = host_by_entry.get(entry_id, "")
                if host and host not in ip_names:
                    ip_names[host] = fname
    except Exception:  # nosec B110
        log.debug("reconcile_ip_names_registry_failed")

    return ip_names


# ── name sync ────────────────────────────────────────────────────────────────


async def _run_name_sync(
    api: "NetAlertXAPIClient",
    ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    ip_names: dict[str, str],
) -> None:
    """Run HaNameSync.sync_names with notify_unnamed=False and IP-based name hints."""
    import config as _cfg
    from netalertx.ha_name_sync import HaNameSync

    syncer = HaNameSync(
        ssh_client=ssh,
        api_client=api,
        gate=gate,
        notifier=notifier,
        ha_host=_cfg.HA_HOST,
        ha_api_token=_cfg.HA_API_TOKEN,
    )
    report = await syncer.sync_names(notify_unnamed=False, extra_ip_names=ip_names)
    log.info(
        "reconcile_name_sync_complete",
        written=len(report.written),
        locked=len(report.locked),
        conflicted=len(report.conflicted),
        unnamed=len(report.unnamed),
    )
