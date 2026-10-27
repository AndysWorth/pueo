"""Network identity resolution: HA device → NetAlertX device.

Mapping order (best to weakest evidence):
  1. Manual link from user_prefs (netalertx:link:<device_id>)
  2. Every MAC address in the HA device registry
  3. IP connections in the HA device registry
  4. Config-entry host/address/ip_address (port stripped)
"""

from __future__ import annotations

import json
import re
from typing import Optional, TYPE_CHECKING

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

log = get_logger("network_identity")

_PORT_RE = re.compile(r":\d+$")


def _strip_port(value: str) -> str:
    return _PORT_RE.sub("", value)


def candidates_for_device(
    device: dict,
    host_by_entry: dict[str, str],
    manual_links: dict[str, str],
) -> list[tuple[str, str]]:
    """Return (kind, value) candidates in reliability order.

    kind is one of: manual | mac | ip | config_entry
    """
    device_id = device.get("id", "")
    result: list[tuple[str, str]] = []

    if device_id in manual_links:
        result.append(("manual", manual_links[device_id].lower()))

    for conn_type, conn_val in device.get("connections", []):
        if conn_type == "mac" and conn_val:
            result.append(("mac", conn_val.lower()))
        elif conn_type == "ip" and conn_val:
            result.append(("ip", _strip_port(conn_val)))

    for entry_id in device.get("config_entries", []):
        host = host_by_entry.get(entry_id)
        if host:
            result.append(("config_entry", _strip_port(host)))

    return result


def match_netalertx(
    candidates: list[tuple[str, str]],
    nax_devices: list[dict],
) -> tuple[Optional[dict], str]:
    """Return (nax_device, method) by matching candidates against NetAlertX inventory.

    method is one of: manual | mac | ip | config_entry | unmapped | not_in_netalertx
    """
    if not candidates:
        return None, "unmapped"

    mac_to_nax: dict[str, dict] = {}
    ip_to_nax: dict[str, dict] = {}
    for dev in nax_devices:
        mac = (dev.get("devMac") or "").lower()
        if mac:
            mac_to_nax[mac] = dev
        ip = dev.get("devLastIP") or ""
        if ip:
            ip_to_nax[ip] = dev

    for kind, value in candidates:
        if kind in ("manual", "mac"):
            nax = mac_to_nax.get(value)
            if nax is not None:
                return nax, kind
        else:  # ip or config_entry
            nax = ip_to_nax.get(value)
            if nax is not None:
                return nax, kind

    return None, "not_in_netalertx"


async def load_config_entry_hosts(ssh: "SSHClientProtocol") -> dict[str, str]:
    """Read core.config_entries from HA and return entry_id → host/IP.

    Only extracts host/address/ip_address values from each entry's data dict.
    Never logs other entry fields. Returns {} on any error.
    """
    _HOST_KEYS = ("host", "address", "ip_address")
    try:
        raw = await ssh.read_file("/config/.storage/core.config_entries")
        storage = json.loads(raw)
        entries = storage.get("data", {}).get("entries", [])
        result: dict[str, str] = {}
        for entry in entries:
            entry_id = entry.get("entry_id") or entry.get("id", "")
            if not entry_id:
                continue
            data = entry.get("data", {})
            for key in _HOST_KEYS:
                val = data.get(key)
                if val and isinstance(val, str):
                    result[entry_id] = val
                    break
        return result
    except Exception:  # nosec B110
        log.debug("load_config_entry_hosts_failed")
        return {}


def load_manual_links(db_path: str) -> dict[str, str]:
    """Read netalertx:link:<device_id> rows from user_prefs.

    Returns device_id → MAC (lowercase). Returns {} on any error.
    """
    import sqlite3

    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_prefs WHERE key LIKE 'netalertx:link:%'"
            ).fetchall()
        return {
            row[0].removeprefix("netalertx:link:"): row[1].lower()
            for row in rows
            if row[1]
        }
    except Exception:  # nosec B110
        return {}
