#!/usr/bin/env python3
"""HA persistent notification triage: schema, rule-based classification, history tracking, and enrichment."""

import asyncio
import datetime
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

log = get_logger("ha_notification_manager")

_IP_PATTERN = re.compile(r"from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")


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


def classify_notification(notification_id: str) -> tuple[str, str]:
    """Return (category, severity) for a notification_id using rule-based lookup."""
    return _CLASSIFICATION_MAP.get(notification_id, ("other", "MEDIUM"))


def extract_ip_from_message(message: str) -> Optional[str]:
    """Extract the first IPv4 address preceded by 'from ' in a notification message."""
    m = _IP_PATTERN.search(message)
    return m.group(1) if m else None


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


async def _get_arp_info(ip: str) -> dict:
    """Return MAC address and vendor info from the local ARP table."""
    result: dict = {
        "mac_address": None,
        "mac_is_randomized": None,
        "mac_vendor": None,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            "arp",
            "-n",
            ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        m = re.search(r"([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", stdout.decode(), re.I)
        if not m:
            return result
        mac = m.group(1).lower()
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


async def enrich_http_login(
    ip: str,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    ws_client: Optional[HAWebSocketClientProtocol] = None,
) -> dict:
    """Enrich an http_login notification with device context for the given source IP.

    Tries three sources in order: reverse DNS, NetAlertX device list, HA device registry.
    All are best-effort; failure in any source is silently skipped.
    """
    context: dict = {
        "source_ip": ip,
        "hostname": None,
        "netalertx_name": None,
        "ha_device_name": None,
        "is_known_device": False,
        "mac_address": None,
        "mac_is_randomized": None,
        "mac_vendor": None,
        "dhcp_hostname": None,
    }

    try:
        hostname, _, _ = await asyncio.to_thread(socket.gethostbyaddr, ip)
        context["hostname"] = hostname
        context["is_known_device"] = True
    except Exception:  # nosec B110
        pass

    if netalertx_client is not None:
        try:
            devices = await netalertx_client.get_devices()
            for device in devices:
                if device.get("devLastIP") == ip:
                    context["netalertx_name"] = device.get("devName")
                    context["is_known_device"] = True
                    break
        except Exception as exc:  # nosec B110
            log.warning("netalertx_enrichment_failed", ip=ip, error=str(exc))

    if ws_client is not None:
        try:
            registry = await ws_client.get_device_registry()
            for device in registry:
                if ["ip", ip] in device.get("connections", []):
                    context["ha_device_name"] = device.get(
                        "name_by_user"
                    ) or device.get("name")
                    context["is_known_device"] = True
                    break
        except Exception as exc:  # nosec B110
            log.warning("ha_device_registry_enrichment_failed", ip=ip, error=str(exc))

    arp_info = await _get_arp_info(ip)
    context.update(arp_info)

    gateway_ip = await _get_default_gateway()
    if gateway_ip:
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
) -> None:
    """Run an AgentLoop to investigate an HA persistent notification and create a HITL card."""
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_notification_investigation_registry
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.llm.llm_factory import make_llm_client
    from utils.core.prompts import load_prompt

    category, severity = classify_notification(notification_id)
    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = FakeAutonomyGate(auto_execute_result=True)

    class _NullSSH:
        async def read_file(self, path: str) -> str:
            return ""

        async def write_file(self, path: str, content: str) -> None:
            pass

        async def download_file(self, remote_path: str, local_path: str) -> None:
            pass

        async def run(self, command: str, check: bool = False) -> tuple:
            return (0, "", "")

        async def stream_lines(self, command: str):  # type: ignore[return]
            pass

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
        ha_ssh_client=_NullSSH(),  # type: ignore[arg-type]
        gate=gate,  # type: ignore[arg-type]
        notifier=notifier,
        netalertx_api_client=netalertx_client,  # type: ignore[arg-type]
        ha_ws_client=ws_client,
        knowledge_store=knowledge_store,  # type: ignore[arg-type]
        db_path=db_path,
        pending_notification=pending_notif,
    )

    registry = build_notification_investigation_registry()
    system_prompt = load_prompt("agent_loop_notification").format(
        terminal_tool="finish_notification_investigation"
    )

    initial_context = (
        f"HA persistent notification:\n"
        f"Notification ID: {notification_id}\n"
        f"Category: {category} | Severity: {severity}\n"
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
    )
    increment_active_agent()
    try:
        result = await loop.run(initial_context=initial_context)
        publish_activity_done("notification", result.outcome)
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
