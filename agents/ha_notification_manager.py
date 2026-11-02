#!/usr/bin/env python3
"""HA persistent notification triage: schema, rule-based classification, history tracking, and enrichment."""

import asyncio
import datetime
import ipaddress
import re
import socket
import sqlite3
import time
from typing import Literal, Optional

from pydantic import BaseModel

from typing import TYPE_CHECKING

import config as _config
from config import (
    DB_PATH,
)
from interfaces import (
    HARestClientProtocol,
    HAWebSocketClientProtocol,
    LLMClientProtocol,
    NetAlertXClientProtocol,
    SSHClientProtocol,
)
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from utils.hitl.notify import NotifierProtocol
    from utils.agent.autonomy import AutonomyGate

log = get_logger("ha_notification_manager")

_IP_CANDIDATE_PATTERN = re.compile(r"from \(?([0-9A-Fa-f:.]+[0-9A-Fa-f])")
_IOS_HA_UA = "io.robbie.HomeAssistant"

# Private LAN ranges that need no local-interface context: RFC-1918, IPv4
# link-local, IPv6 link-local and IPv6 unique-local (ULA).
_PRIVATE_LAN_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fe80::/10",
        "fc00::/7",
    )
)
_INTERFACE_ADDR_PATTERN = re.compile(r"\binet6?\s+([0-9A-Fa-f:.]+)")
_MAC_PATTERN = re.compile(r"([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", re.I)

NetworkScope = Literal["pueo_host", "lan", "external", "unknown"]


def _parse_ip(
    value: str,
) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse an IPv4/IPv6 string (zone suffix ``%en0`` dropped); None if invalid.

    IPv4-mapped IPv6 addresses (``::ffff:10.0.0.5``) are unwrapped to IPv4.
    """
    try:
        addr = ipaddress.ip_address(value.strip().split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private_lan(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        addr in net for net in _PRIVATE_LAN_NETWORKS if addr.version == net.version
    )


def classify_network_scope(ip: str, local_addrs: set) -> NetworkScope:
    """Classify a source IP relative to the Pueo host — pure, deterministic.

    ``pueo_host``: one of the host's own addresses (or loopback).
    ``lan``: a private LAN range, or a global IPv6 address in the same /64 as one
    of the host's own global IPv6 addresses (SLAAC clients on the home network
    have public-looking addresses, so "not RFC-1918" does not mean external).
    ``external``: anything else.  ``unknown``: not a parseable IP.
    """
    addr = _parse_ip(ip)
    if addr is None:
        return "unknown"
    local = {a for a in (_parse_ip(str(x)) for x in local_addrs) if a is not None}
    if addr in local or addr.is_loopback:
        return "pueo_host"
    if _is_private_lan(addr):
        return "lan"
    if isinstance(addr, ipaddress.IPv6Address):
        for own in local:
            if (
                isinstance(own, ipaddress.IPv6Address)
                and not own.is_loopback
                and not own.is_link_local
                and addr in ipaddress.IPv6Network(f"{own}/64", strict=False)
            ):
                return "lan"
    return "external"


def _is_ios_ha_companion(message: str) -> bool:
    """Return True when the message is from the iOS HA companion app on a local IP."""
    if _IOS_HA_UA not in message:
        return False
    ip = extract_ip_from_message(message)
    addr = _parse_ip(ip) if ip else None
    return bool(addr is not None and _is_private_lan(addr))


class NotificationAnalysis(BaseModel):
    notification_id: str
    category: Literal["security", "update", "config_error", "integration", "other"]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    original_title: Optional[str]
    original_message: str
    human_explanation: str
    enriched_context: dict
    recommended_action: str
    requires_hitl: bool


_CLASSIFICATION_MAP: dict[str, tuple[str, str]] = {
    "http-login": ("security", "HIGH"),
    "ip-ban": ("security", "HIGH"),
    "invalid_config": ("config_error", "HIGH"),
}


def classify_notification(notification_id: str, message: str = "") -> tuple[str, str]:
    """Return (category, severity) for a notification_id using rule-based lookup.

    For http-login, lowers severity to MEDIUM when the message identifies the iOS
    HA companion app (io.robbie.HomeAssistant) on a local RFC-1918 IP.
    """
    category, severity = _CLASSIFICATION_MAP.get(notification_id, ("other", "MEDIUM"))
    if (
        notification_id == "http-login"
        and severity == "HIGH"
        and _is_ios_ha_companion(message)
    ):
        severity = "MEDIUM"
    return category, severity


def notification_knowledge_query(notification_id: str, title: Optional[str]) -> str:
    """Targeted pre-inject query: notification id words (no numeric suffixes) + title."""
    id_words = [
        w for w in re.split(r"[^A-Za-z0-9]+", notification_id) if w and w.isalpha()
    ]
    return " ".join(["HA notification", *id_words, title or ""]).strip()


def extract_ip_from_message(message: str) -> Optional[str]:
    """Extract the first IPv4/IPv6 address preceded by 'from ' in a notification message."""
    for m in _IP_CANDIDATE_PATTERN.finditer(message):
        candidate = m.group(1)
        if _parse_ip(candidate) is not None:
            return candidate
    return None


async def _get_default_gateway() -> Optional[str]:
    """Return the default gateway IP from the local routing table, or None on failure."""
    for cmd in (
        ["route", "-n", "get", "default"],
        ["netstat", "-rn"],
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            for line in stdout.decode().splitlines():
                if "gateway" in line.lower() or "default" in line.lower():
                    parts = line.split()
                    for part in parts:
                        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", part):
                            return part
        except Exception:  # nosec B110
            pass
    return None


def _parse_interface_addresses(text: str) -> set:
    """Extract IPv4 and IPv6 addresses from ``ifconfig`` or ``ip -o addr`` output.

    Zone suffixes (``%en0``) and prefix lengths (``/64``) are dropped.  Deprecated
    and temporary IPv6 privacy addresses are kept — a client may still be using
    one when HA logs its request (#1061).
    """
    addrs: set = set()
    for m in _INTERFACE_ADDR_PATTERN.finditer(text):
        addr = _parse_ip(m.group(1))
        if addr is not None:
            addrs.add(str(addr))
    return addrs


def _get_local_interface_addresses() -> set:
    """Return all IPv4/IPv6 addresses on local interfaces (sync, for to_thread).

    Tries ``ifconfig`` (macOS) and then ``ip -o addr`` (Linux / Docker host
    network); the union of whatever succeeds is returned.
    """
    import subprocess  # nosec B404

    addrs: set = set()
    for cmd in (["ifconfig"], ["ip", "-o", "addr"]):
        try:
            out = subprocess.check_output(  # nosec B603 B607
                cmd, text=True, stderr=subprocess.DEVNULL, timeout=5
            )
        except Exception:  # nosec B112
            continue
        addrs |= _parse_interface_addresses(out)
    return addrs


def _normalize_mac(mac: str) -> str:
    """Lower-case and zero-pad a MAC (``e4:5f:1:a2:b1:91`` → ``e4:5f:01:a2:b1:91``)."""
    return ":".join(part.zfill(2) for part in mac.lower().split(":"))


def _parse_neighbor_mac(text: str, ip: str) -> Optional[str]:
    """Return the MAC for ``ip`` from ``ndp -an`` or ``ip -6 neigh`` output, or None.

    Each line starts with the address (``ndp`` adds ``%en0`` to link-local ones);
    lines without a MAC (``(incomplete)``, ``FAILED``) are skipped.
    """
    target = _parse_ip(ip)
    if target is None:
        return None
    for line in text.splitlines():
        parts = line.split()
        if not parts or _parse_ip(parts[0]) != target:
            continue
        m = _MAC_PATTERN.search(" ".join(parts[1:]))
        if m:
            return _normalize_mac(m.group(1))
    return None


async def _run_capture(*cmd: str) -> str:
    """Run a local command and return its stdout ('' on any failure)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace")
    except Exception:
        return ""


async def _lookup_mac(ip: str) -> Optional[str]:
    """Resolve ``ip`` to a MAC via the host's ARP (IPv4) or NDP (IPv6) cache."""
    addr = _parse_ip(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        for cmd in (("ndp", "-an"), ("ip", "-6", "neigh", "show", str(addr))):
            mac = _parse_neighbor_mac(await _run_capture(*cmd), str(addr))
            if mac:
                return mac
        return None
    if addr is None:
        return None
    m = _MAC_PATTERN.search(await _run_capture("arp", "-n", str(addr)))
    return _normalize_mac(m.group(1)) if m else None


async def _get_arp_info(ip: str) -> dict:
    """Return MAC address and vendor info from the local ARP / NDP neighbor cache."""
    result: dict = {
        "mac_address": None,
        "mac_is_randomized": None,
        "mac_vendor": None,
    }
    try:
        mac = await _lookup_mac(ip)
        if not mac:
            return result
        result["mac_address"] = mac
        first_byte = int(mac.split(":")[0], 16)
        result["mac_is_randomized"] = bool(first_byte & 0x02)
        if not result["mac_is_randomized"]:
            try:
                from manuf import manuf as manuf_mod  # type: ignore[import]

                p = manuf_mod.MacParser()
                result["mac_vendor"] = p.get_manuf(mac)
            except Exception:  # nosec B110
                pass
    except Exception:  # nosec B110
        pass
    return result


async def _try_router_dhcp_hostname(gateway_ip: str, target_ip: str) -> Optional[str]:
    """Try SSH to the router for dnsmasq lease files. Returns hostname or None on failure."""
    lease_cmd = "cat /tmp/dhcp.leases /var/lib/misc/dnsmasq.leases 2>/dev/null"
    for user in ("root", "admin"):
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "ssh",
                    "-o",
                    "ConnectTimeout=2",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "BatchMode=yes",
                    f"{user}@{gateway_ip}",
                    lease_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                timeout=3.0,
            )
            stdout, _ = await proc.communicate()
            for line in stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[2] == target_ip and parts[3] != "*":
                    return parts[3]
        except Exception:  # nosec B110
            pass
    return None


def _match_netalertx_device(
    devices: list[dict],
    source: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address],
    mac: Optional[str],
) -> tuple[Optional[dict], Optional[str]]:
    """Return (device, "ip"|"mac") for the NetAlertX device matching the source.

    IP match (``devLastIP`` / ``devPrimaryIPv4`` / ``devPrimaryIPv6``) wins over a
    MAC match, which is how IPv6 sources are found (#1061).
    """
    if source is not None:
        for device in devices:
            for key in ("devLastIP", "devPrimaryIPv4", "devPrimaryIPv6"):
                value = device.get(key)
                if value and _parse_ip(str(value)) == source:
                    return device, "ip"
    if mac:
        for device in devices:
            dev_mac = device.get("devMac")
            if dev_mac and _normalize_mac(str(dev_mac)) == mac:
                return device, "mac"
    return None, None


def _registry_device_matches(
    device: dict,
    source: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address],
    mac: Optional[str],
) -> bool:
    """True when an HA registry device has an ``ip`` or ``mac`` connection for the source."""
    for conn in device.get("connections", []):
        if len(conn) != 2:
            continue
        kind, value = conn[0], str(conn[1])
        if kind == "ip" and source is not None and _parse_ip(value) == source:
            return True
        if kind == "mac" and mac and _normalize_mac(value) == mac:
            return True
    return False


async def enrich_http_login(
    ip: str,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    ws_client: Optional[HAWebSocketClientProtocol] = None,
) -> dict:
    """Enrich an http_login notification with device context for the given source IP.

    Sources: local interfaces (``network_scope``), reverse DNS, the host's ARP/NDP
    neighbor cache (MAC), the NetAlertX device list and the HA device registry.
    All are best-effort; failure in any source is skipped.  ``inventory_available`` is
    True only when the NetAlertX device list was actually read, so a null
    ``netalertx_name`` is not mistaken for "not in inventory" (#945).
    ``netalertx_status`` is one of ok / not_configured / no_token / error.

    NetAlertX and HA are matched by IP first and then by MAC.  NetAlertX scans
    IPv4 only, so an IPv6 source is identified through its neighbor-cache MAC
    (#1061).  ``network_scope`` (pueo_host / lan / external / unknown) is computed
    in code so a home-network IPv6 address is never judged "external".
    """
    context: dict = {
        "source_ip": ip,
        "network_scope": "unknown",
        "hostname": None,
        "inventory_available": False,
        "netalertx_status": "not_configured",
        "netalertx_name": None,
        "netalertx_match": None,
        "ha_device_name": None,
        "is_known_device": False,
        "is_pueo_host": False,
        "mac_address": None,
        "mac_is_randomized": None,
        "mac_vendor": None,
        "dhcp_hostname": None,
    }
    source = _parse_ip(ip)

    try:
        local_addrs = await asyncio.to_thread(_get_local_interface_addresses)
    except Exception:  # nosec B110
        local_addrs = set()
    context["network_scope"] = classify_network_scope(ip, local_addrs)
    if context["network_scope"] == "pueo_host":
        context["is_pueo_host"] = True
        context["is_known_device"] = True

    try:
        hostname, _, _ = await asyncio.to_thread(socket.gethostbyaddr, ip)
        context["hostname"] = hostname
        context["is_known_device"] = True
    except Exception:  # nosec B110
        pass

    arp_info = await _get_arp_info(ip)
    context.update(arp_info)
    mac = arp_info.get("mac_address")

    if netalertx_client is not None and not getattr(
        netalertx_client, "has_token", True
    ):
        context["netalertx_status"] = "no_token"
    elif netalertx_client is not None:
        try:
            devices = await netalertx_client.get_devices()
            context["inventory_available"] = True
            context["netalertx_status"] = "ok"
            match, method = _match_netalertx_device(devices, source, mac)
            if match is not None:
                context["netalertx_name"] = match.get("devName")
                context["netalertx_match"] = method
                context["is_known_device"] = True
        except Exception as exc:  # nosec B110
            context["netalertx_status"] = "error"
            log.warning("netalertx_enrichment_failed", ip=ip, error=str(exc))

    if ws_client is not None:
        try:
            registry = await ws_client.get_device_registry()
            for device in registry:
                if _registry_device_matches(device, source, mac):
                    context["ha_device_name"] = device.get(
                        "name_by_user"
                    ) or device.get("name")
                    context["is_known_device"] = True
                    break
        except Exception as exc:  # nosec B110
            log.warning("ha_device_registry_enrichment_failed", ip=ip, error=str(exc))

    gateway_ip = await _get_default_gateway()
    if gateway_ip and isinstance(source, ipaddress.IPv4Address):
        dhcp_hostname = await _try_router_dhcp_hostname(gateway_ip, ip)
        if dhcp_hostname:
            context["dhcp_hostname"] = dhcp_hostname
            context["is_known_device"] = True

    return context


async def _run_notification_investigation(
    notification_id: str,
    title: Optional[str],
    message: str,
    ha_created_at: Optional[float],
    db_path: str,
    notifier: "NotifierProtocol",
    llm_client: Optional[LLMClientProtocol] = None,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    ws_client: Optional[HAWebSocketClientProtocol] = None,
    knowledge_store: Optional[object] = None,
    ha_rest_client: Optional["HARestClientProtocol"] = None,
    ssh_client: Optional["SSHClientProtocol"] = None,
    gate: Optional["AutonomyGate"] = None,
) -> None:
    """Run an AgentLoop to investigate an HA persistent notification and create a HITL card.

    Runs through ``AutonomyGate(AUTONOMY_LEVEL)`` so REST write tools route
    through the real gate and produce HITL cards at lower autonomy levels.
    SSH reads use ``ReadOnlySSHClient`` (ADR 043); every SSH write raises.
    """
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_notification_investigation_registry
    from utils.agent.autonomy import AutonomyGate
    from utils.ha.read_only_ssh import ReadOnlySSHClient
    from utils.llm.llm_factory import make_llm_client
    from utils.core.prompts import load_agent_prompt

    category, severity = classify_notification(notification_id, message)
    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = gate or AutonomyGate(_config.AUTONOMY_LEVEL)
    if ssh_client is None:
        from utils.ha.ssh_client import AsyncSSHClient

        ssh_client = AsyncSSHClient()

    pending_notif: dict = {
        "ha_nid": notification_id,
        "title": title,
        "message": message,
        "category": category,
        "severity": severity,
        "ha_created_at": ha_created_at,
        "db_path": db_path,
    }

    executor = ToolExecutor(
        ha_ssh_client=ReadOnlySSHClient(ssh_client),
        gate=gate,
        notifier=notifier,
        netalertx_api_client=netalertx_client,  # type: ignore[arg-type]
        ha_ws_client=ws_client,
        ha_rest_client=ha_rest_client,
        knowledge_store=knowledge_store,  # type: ignore[arg-type]
        db_path=db_path,
        pending_notification=pending_notif,
    )
    try:
        from utils.ha.ha_environment import load_environment_profile as _lep
        import asyncio as _asyncio

        _prof = await _asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_notification_investigation_registry()
    system_prompt = load_agent_prompt(
        "notification", "finish_notification_investigation"
    )

    initial_context = (
        f"HA persistent notification:\n"
        f"Notification ID: {notification_id}\n"
        f"Category: {category}\n"
        f"Title: {title or '(none)'}\n"
        f"Message: {message}\n"
    )

    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
        publish_activity_done,
    )

    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,
        tool_registry=registry,
        system_prompt=system_prompt,
        terminal_tool_name="finish_notification_investigation",
        trigger="notification_poll",
        activity_type="notification",
        db_path=db_path,
        knowledge_store=knowledge_store,  # type: ignore[arg-type]
        timeline_callback=make_activity_timeline_callback(
            "notification", trigger=f"HA notification: {notification_id}"
        ),
        capture_llm=True,
    )
    from utils.knowledge.runbook_signature import sig_notification

    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=notification_knowledge_query(notification_id, title),
            signature=sig_notification(notification_id),
            subject_key=notification_id,
        )
        publish_activity_done("notification", result.outcome)
        if result.outcome != "success":
            import sqlite3 as _sqlite3
            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3.connect(db_path) as _conn:
                mark_investigation_backoff(
                    _conn, f"notification_backoff:{notification_id}"
                )
            log.warning(
                "notification_stuck_backoff",
                outcome=result.outcome,
                notification_id=notification_id,
            )
    except Exception as exc:
        log.error(
            "notification_investigation_failed",
            ha_notification_id=notification_id,
            error=str(exc),
        )
    finally:
        decrement_active_agent()


def record_notification_seen(
    notification_id: str,
    category: str,
    severity: str,
    db_path: str = DB_PATH,
    ha_created_at: Optional[float] = None,
) -> bool:
    """Insert notification into history if new; update last_seen_at if existing.

    If ha_created_at is newer than the stored value, resets hitl_sent_at so the
    new occurrence generates a fresh approval card.

    Returns True if newly seen (first time), False if already in history.
    """
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute(
            "SELECT ha_created_at, dismissed_at FROM notification_history"
            " WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if existing:
            stored_ha_created_at, stored_dismissed_at = existing
            is_newer_timestamp = (
                ha_created_at is not None
                and stored_ha_created_at is not None
                and ha_created_at > stored_ha_created_at
            )
            # Only treat as a new occurrence when ha_created_at is strictly newer.
            # Using >= would reset dismissed_at during the window between Pueo
            # calling the HA dismiss service and HA removing the notification from
            # its active list (a common race that caused cards to reappear).
            is_new_occurrence = is_newer_timestamp
            if is_new_occurrence:
                conn.execute(
                    """UPDATE notification_history
                       SET first_seen_at = ?, last_seen_at = ?, ha_created_at = ?,
                           hitl_sent_at = NULL, dismissed_at = NULL, dismissed_by = NULL
                       WHERE notification_id = ?""",
                    (now, now, ha_created_at, notification_id),
                )
            else:
                conn.execute(
                    "UPDATE notification_history SET last_seen_at = ? WHERE notification_id = ?",
                    (now, notification_id),
                )
            return False
        conn.execute(
            """
            INSERT INTO notification_history
                (notification_id, first_seen_at, last_seen_at, category, severity,
                 ha_created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (notification_id, now, now, category, severity, ha_created_at),
        )
    return True


def mark_notification_hitl_sent(
    notification_id: str,
    db_path: str = DB_PATH,
) -> None:
    """Record that an approval card was dispatched for this notification."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE notification_history SET hitl_sent_at = ? WHERE notification_id = ?",
            (time.time(), notification_id),
        )


def mark_notification_dismissed(
    notification_id: str,
    dismissed_by: str = "user",
    db_path: str = DB_PATH,
) -> None:
    """Record dismissal after the user clicks Dismiss in the approval card."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE notification_history SET dismissed_at = ?, dismissed_by = ? WHERE notification_id = ?",
            (time.time(), dismissed_by, notification_id),
        )


def get_notification_history(
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return all rows from notification_history ordered by first_seen_at descending."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM notification_history ORDER BY first_seen_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_pending_notifications(
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return notifications not yet dismissed."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM notification_history WHERE dismissed_at IS NULL ORDER BY first_seen_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


# ── Approval card formatting ──────────────────────────────────────────────────


def _format_notification_subject(analysis: "NotificationAnalysis") -> str:
    """Build the approval card subject line for a notification."""
    if analysis.notification_id in ("http-login", "ip-ban"):
        ec = analysis.enriched_context
        if not ec.get("is_known_device", True):
            return "⚠ Unknown source IP — Failed login attempt"
        device = (
            ec.get("netalertx_name") or ec.get("ha_device_name") or ec.get("hostname")
        )
        if device:
            return f"Login attempt from {device}"
        return "Failed login attempt from known device"
    if analysis.notification_id == "invalid_config":
        return "Home Assistant Configuration Error"
    if analysis.original_title:
        return analysis.original_title
    return f"HA Notification: {analysis.notification_id}"


def _format_notification_body(analysis: "NotificationAnalysis") -> str:
    """Build the approval card body text for a notification."""
    lines = [analysis.human_explanation]
    if analysis.recommended_action:
        lines.append(f"\nRecommended action: {analysis.recommended_action}")
    return "\n".join(lines)


# ── One-shot entry point (--mode notifications) ───────────────────────────────


async def run_notifications(
    ha_rest_client: Optional[HARestClientProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    ws_client: Optional[HAWebSocketClientProtocol] = None,
    notifier: Optional["NotifierProtocol"] = None,
    db_path: str = DB_PATH,
) -> int:
    """Poll persistent_notification.* entities and send approval cards for new ones.

    Returns the count of new approval cards sent.
    """
    from config import (
        HA_API_PORT,
        HA_API_TOKEN,
        HA_HOST,
        NETALERTX_API_PORT,
        NETALERTX_API_TOKEN,
        NETALERTX_HOST,
        NOTIFY_WATCH_DIR,
    )
    from netalertx.api_client import NetAlertXAPIClient
    from utils.ha.ha_rest_client import HARestClient
    from utils.ha.ha_ws_client import HAWebSocketClient
    from utils.hitl.notify import FileNotifier

    rest: HARestClientProtocol = ha_rest_client or HARestClient(  # pragma: no cover
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _ws: HAWebSocketClientProtocol = ws_client or HAWebSocketClient(  # pragma: no cover
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _nax: NetAlertXClientProtocol = (
        netalertx_client
        or NetAlertXAPIClient(  # pragma: no cover
            f"http://{NETALERTX_HOST}:{NETALERTX_API_PORT}", NETALERTX_API_TOKEN
        )
    )
    card_notifier: "NotifierProtocol" = notifier or FileNotifier(  # pragma: no cover
        watch_dir=NOTIFY_WATCH_DIR
    )

    try:
        notifications = await _ws.get_persistent_notifications()
    except Exception as exc:
        log.error("notification_poll_failed", error=str(exc))
        print(f"Error polling notifications: {exc}")
        return 0

    new_count = 0
    for notif in notifications:
        ha_nid: str = notif.get("notification_id", "")
        if not ha_nid:
            log.warning("notification_id_missing", notif_keys=list(notif.keys()))
            continue
        title: Optional[str] = notif.get("title")
        message: str = notif.get("message", "")
        ha_created_at: Optional[float] = None
        created_at_str: Optional[str] = notif.get("created_at")
        if created_at_str:
            try:
                ha_created_at = datetime.datetime.fromisoformat(
                    created_at_str
                ).timestamp()
            except Exception:  # nosec B110
                log.warning("notification_created_at_parse_failed", raw=created_at_str)

        category, severity = classify_notification(ha_nid)
        record_notification_seen(ha_nid, category, severity, db_path, ha_created_at)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at FROM notification_history WHERE notification_id = ?",
                (ha_nid,),
            ).fetchone()
        if row and row[0] is not None:
            log.debug("notification_card_already_sent", ha_notification_id=ha_nid)
            continue

        with sqlite3.connect(db_path) as _bconn:
            from utils.hitl.hitl_tracker import should_send_card as _ssc

            if not _ssc(_bconn, f"notification_backoff:{ha_nid}"):
                log.debug(
                    "notification_stuck_backoff_active", ha_notification_id=ha_nid
                )
                continue

        # Capture loop-local variables for the async closure.
        _ha_nid = ha_nid
        _title = title
        _message = message
        _ha_created_at = ha_created_at
        _llm = llm_client
        _nax_ref = _nax
        _ws_ref = _ws
        _notifier_ref = card_notifier
        _db = db_path

        async def _analyze_and_send(
            ha_nid: str = _ha_nid,
            title: "Optional[str]" = _title,
            message: str = _message,
            ha_created_at: "Optional[float]" = _ha_created_at,
        ) -> None:
            await _run_notification_investigation(
                notification_id=ha_nid,
                title=title,
                message=message,
                ha_created_at=ha_created_at,
                db_path=_db,
                notifier=_notifier_ref,
                llm_client=_llm,
                netalertx_client=_nax_ref,
                ws_client=_ws_ref,
            )

        from utils.agent.work_queue import (
            PRIORITY_HIGH,
            WorkItem,
            get_work_queue_or_none,
        )

        _wq = get_work_queue_or_none()
        if _wq is not None:
            accepted = await _wq.submit(
                WorkItem(
                    priority=PRIORITY_HIGH,
                    activity_type="notification",
                    description=f"Notification triage: {ha_nid}",
                    dedup_key=f"notification:{ha_nid}",
                    suppress_while_running=frozenset(),
                    coro_factory=_analyze_and_send,
                )
            )
            if accepted:
                new_count += 1
        else:
            await _analyze_and_send()
            new_count += 1

    if new_count == 0:
        print("No new notifications to triage.")
    else:
        print(f"\n{new_count} notification approval card(s) sent.")

    return new_count
