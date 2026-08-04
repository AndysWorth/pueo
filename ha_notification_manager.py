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

from config import (
    CONFIG_REMOTE_PATH,
    DB_PATH,
    HA_NOTIFICATION_ENRICH_AUTH_FAILURES,
    MAX_PROMPT_TOKENS,
    OLLAMA_MODEL,
)
from interfaces import (
    HARestClientProtocol,
    HAWebSocketClientProtocol,
    LLMClientProtocol,
    NetAlertXClientProtocol,
    SSHClientProtocol,
)
from utils.context import truncate_to_budget
from utils.logging import get_logger

if TYPE_CHECKING:
    from utils.notify import NotifierProtocol

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


class _NotificationLLMOutput(BaseModel):
    human_explanation: str
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
        except Exception:  # nosec B110
            pass

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
        except Exception:  # nosec B110
            pass

    arp_info = await _get_arp_info(ip)
    context.update(arp_info)

    gateway_ip = await _get_default_gateway()
    if gateway_ip:
        dhcp_hostname = await _try_router_dhcp_hostname(gateway_ip, ip)
        if dhcp_hostname:
            context["dhcp_hostname"] = dhcp_hostname
            context["is_known_device"] = True

    return context


async def analyze_notification(
    notification_id: str,
    title: Optional[str],
    message: str,
    enriched_context: dict,
    config_content: str,
    llm_client: Optional[LLMClientProtocol] = None,
    category: str = "other",
    severity: str = "MEDIUM",
) -> NotificationAnalysis:
    """LLM plain-English analysis of a notification. Returns a full NotificationAnalysis."""
    from utils.ollama_client import OllamaClient

    client: LLMClientProtocol = llm_client or OllamaClient()  # pragma: no cover

    context_parts: list[str] = []
    if enriched_context:
        lines: list[str] = []
        if enriched_context.get("source_ip"):
            lines.append(f"Source IP: {enriched_context['source_ip']}")
        if enriched_context.get("mac_address"):
            mac = enriched_context["mac_address"]
            rand = enriched_context.get("mac_is_randomized")
            vendor = enriched_context.get("mac_vendor")
            if rand:
                lines.append(
                    f"MAC: {mac} (locally administered / randomized — cannot identify vendor)"
                )
            else:
                lines.append(f"MAC: {mac}" + (f" ({vendor})" if vendor else ""))
        for key in ("dhcp_hostname", "hostname", "netalertx_name", "ha_device_name"):
            val = enriched_context.get(key)
            if val:
                lines.append(f"{key.replace('_', ' ').title()}: {val}")
        is_known = enriched_context.get("is_known_device", False)
        lines.append(f"Known device: {'yes' if is_known else 'no'}")
        context_parts.append("Device context:\n" + "\n".join(lines))
    if config_content:
        budget = MAX_PROMPT_TOKENS - 400
        context_parts.append(
            f"Current configuration.yaml:\n{truncate_to_budget(config_content, budget)}"
        )

    context_str = (
        "\n\n".join(context_parts) if context_parts else "No additional context."
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a Home Assistant assistant explaining system notifications "
                "in plain English. Provide a clear human-readable explanation of what "
                "happened and what the user should do about it."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Notification ID: {notification_id}\n"
                f"Category: {category} | Severity: {severity}\n"
                f"Title: {title or '(none)'}\n"
                f"Message: {message}\n\n"
                f"{context_str}\n\n"
                "Explain what this notification means and what the user should do. "
                "Set requires_hitl to true if the user needs to take immediate action."
            ),
        },
    ]

    response = await client.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        options={"temperature": 0.0},
        format=_NotificationLLMOutput.model_json_schema(),
    )
    raw = response["message"]["content"]
    llm_out = _NotificationLLMOutput.model_validate_json(raw)

    return NotificationAnalysis(
        notification_id=notification_id,
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        original_title=title,
        original_message=message,
        human_explanation=llm_out.human_explanation,
        enriched_context=enriched_context,
        recommended_action=llm_out.recommended_action,
        requires_hitl=llm_out.requires_hitl,
    )


async def enrich_and_analyze_notification(
    notification_id: str,
    title: Optional[str],
    message: str,
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    ws_client: Optional[HAWebSocketClientProtocol] = None,
) -> NotificationAnalysis:
    """Orchestrate enrichment and LLM analysis for a notification.

    For http_login: extracts source IP and enriches with reverse DNS,
    NetAlertX device name, and HA device registry. Unknown-source logins
    are escalated to CRITICAL.

    For invalid_config: fetches configuration.yaml content so the LLM can
    cite the specific broken section.
    """
    category, severity = classify_notification(notification_id)
    enriched_context: dict = {}
    config_content = ""

    if (
        notification_id in ("http-login", "ip-ban")
        and HA_NOTIFICATION_ENRICH_AUTH_FAILURES
    ):
        ip = extract_ip_from_message(message)
        if ip:
            enriched_context = await enrich_http_login(ip, netalertx_client, ws_client)
            if not enriched_context.get("is_known_device", True):
                severity = "CRITICAL"
    elif notification_id == "invalid_config" and ssh_client is not None:
        try:
            config_content = await ssh_client.read_file(CONFIG_REMOTE_PATH)
        except Exception:  # nosec B110
            pass

    return await analyze_notification(
        notification_id,
        title,
        message,
        enriched_context,
        config_content,
        llm_client,
        category,
        severity,
    )


def record_notification_seen(
    notification_id: str,
    category: str,
    severity: str,
    db_path: str = DB_PATH,
    ha_created_at: Optional[float] = None,
) -> bool:
    """Insert notification into history if new; update last_seen_at if existing.

    If ha_created_at is newer than the stored value, resets hitl_sent_at so the
    new occurrence generates a fresh HITL card.

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
    """Record that a HITL card was dispatched for this notification."""
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
    """Record dismissal after the user clicks Dismiss in the HITL card."""
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


# ── HITL card formatting ──────────────────────────────────────────────────────


def _format_notification_subject(analysis: "NotificationAnalysis") -> str:
    """Build the HITL card subject line for a notification."""
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
    """Build the HITL card body text for a notification."""
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
    """Poll persistent_notification.* entities and send HITL cards for new ones.

    Returns the count of new HITL cards sent.
    """
    from config import HA_API_PORT, HA_API_TOKEN, HA_HOST, NOTIFY_WATCH_DIR
    from utils.ha_rest_client import HARestClient
    from utils.ha_ws_client import HAWebSocketClient
    from utils.notify import FileNotifier

    rest: HARestClientProtocol = ha_rest_client or HARestClient(  # pragma: no cover
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _ws: HAWebSocketClientProtocol = ws_client or HAWebSocketClient(  # pragma: no cover
        HA_HOST, HA_API_PORT, HA_API_TOKEN
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
                pass

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

        try:
            analysis = await enrich_and_analyze_notification(
                ha_nid,
                title,
                message,
                ssh_client,
                llm_client,
                netalertx_client,
                ws_client,
            )
        except Exception as exc:
            log.error(
                "notification_analysis_failed",
                ha_notification_id=ha_nid,
                error=str(exc),
            )
            continue

        card_id = f"notif_{ha_nid}"
        subject = _format_notification_subject(analysis)
        body = _format_notification_body(analysis)

        payload: dict = {
            "notification_id": card_id,
            "ha_notification_id": ha_nid,
            "is_notification_card": True,
            "category": analysis.category,
            "severity": analysis.severity,
            "human_explanation": analysis.human_explanation,
            "recommended_action": analysis.recommended_action,
            "enriched_context": analysis.enriched_context,
            "original_message": analysis.original_message,
            "original_title": analysis.original_title,
            "ha_created_at": ha_created_at,
        }

        await card_notifier.send(subject, body, payload)
        mark_notification_hitl_sent(ha_nid, db_path)
        new_count += 1
        log.info(
            "notification_hitl_sent",
            ha_notification_id=ha_nid,
            severity=analysis.severity,
        )
        print(f"[{analysis.severity}] {subject}")

    if new_count == 0:
        print("No new notifications to triage.")
    else:
        print(f"\n{new_count} notification HITL card(s) sent.")

    return new_count
