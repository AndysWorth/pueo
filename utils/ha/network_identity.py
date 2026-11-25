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
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

log = get_logger("network_identity")

_PORT_RE = re.compile(r":\d+$")


def _strip_port(value: str) -> str:
    return _PORT_RE.sub("", value)


@dataclass
class DeviceTwin:
    """Another HA device that shares the same physical hardware."""

    device_id: str
    device_name: str
    integration: str  # domain of its config entry
    config_entry_id: str
    entity_states: list[dict]  # [{entity_id, state, last_changed}]


def _normalise_serial(value: str) -> str:
    """Lowercase and remove colons so MACs and serials compare uniformly."""
    return value.lower().replace(":", "")


def find_device_twins(
    device: dict,
    device_registry: list[dict],
    entity_states: Optional[list[dict]] = None,
    config_entries: Optional[list[dict]] = None,
) -> list[DeviceTwin]:
    """Return other devices that share the same physical hardware as *device*.

    Matching criteria (either is sufficient):
    - Normalised ``serial_number`` equality (strips colons, lowercases).
    - A MAC connection on one device equals the ``serial_number`` of the other
      (or vice-versa) after normalisation — the LIFX + Matter twin pattern.

    Name alone is never sufficient to count as a twin.  A device that shares the
    same config entry is excluded (it is a sibling, not a twin).
    """
    source_id: str = device.get("id", "")
    source_entries: set[str] = set(device.get("config_entries", []))
    source_serial: str = _normalise_serial(device.get("serial_number") or "")
    source_macs: set[str] = {
        _normalise_serial(v)
        for t, v in device.get("connections", [])
        if t == "mac" and v
    }

    # Build lookup for config entry domain
    entry_domain: dict[str, str] = {}
    if config_entries:
        for entry in config_entries:
            eid = entry.get("entry_id") or entry.get("id", "")
            dom = entry.get("domain", "")
            if eid:
                entry_domain[eid] = dom

    # Build lookup for entity states
    state_by_entity: dict[str, dict] = {}
    if entity_states:
        for s in entity_states:
            eid = s.get("entity_id", "")
            if eid:
                state_by_entity[eid] = s

    twins: list[DeviceTwin] = []
    for other in device_registry:
        other_id: str = other.get("id", "")
        if not other_id or other_id == source_id:
            continue
        other_entries: set[str] = set(other.get("config_entries", []))
        # Same config entry → sibling, not twin
        if source_entries & other_entries:
            continue

        other_serial: str = _normalise_serial(other.get("serial_number") or "")
        other_macs: set[str] = {
            _normalise_serial(v)
            for t, v in other.get("connections", [])
            if t == "mac" and v
        }

        matched = False
        # Serial ↔ serial
        if source_serial and other_serial and source_serial == other_serial:
            matched = True
        # MAC ↔ serial cross-match (d0:73:d5:84:e4:05 ↔ D073D584E405)
        if not matched and source_serial and source_serial in other_macs:
            matched = True
        if not matched and other_serial and other_serial in source_macs:
            matched = True

        if not matched:
            continue

        # Determine integration via first config entry
        integration = ""
        config_entry_id = ""
        for oe in sorted(other_entries):
            dom = entry_domain.get(oe, "")
            if dom:
                integration = dom
                config_entry_id = oe
                break
        if not integration and other_entries:
            config_entry_id = next(iter(sorted(other_entries)))

        # Gather representative entity states for this twin (from entity_states)
        twin_entity_states: list[dict] = []
        if entity_states:
            for s in entity_states:
                eid = s.get("entity_id", "")
                if not eid:
                    continue
                # We have no entity-registry here, so filter by state dict fields only;
                # callers that have entity registry pass it pre-filtered via entity_states.
                twin_entity_states.append(
                    {
                        "entity_id": eid,
                        "state": s.get("state", ""),
                        "last_changed": s.get("last_changed", ""),
                    }
                )

        other_name: str = other.get("name_by_user") or other.get("name") or other_id
        twins.append(
            DeviceTwin(
                device_id=other_id,
                device_name=other_name,
                integration=integration,
                config_entry_id=config_entry_id,
                entity_states=twin_entity_states,
            )
        )

    return twins


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
