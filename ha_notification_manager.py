#!/usr/bin/env python3
"""HA persistent notification triage: schema, rule-based classification, history tracking, and enrichment."""

import asyncio
import re
import socket
import sqlite3
import time
from typing import Literal, Optional

from pydantic import BaseModel

from config import (
    CONFIG_REMOTE_PATH,
    DB_PATH,
    HA_NOTIFICATION_ENRICH_AUTH_FAILURES,
    MAX_PROMPT_TOKENS,
    OLLAMA_MODEL,
)
from interfaces import (
    HAWebSocketClientProtocol,
    LLMClientProtocol,
    NetAlertXClientProtocol,
    SSHClientProtocol,
)
from utils.context import truncate_to_budget
from utils.logging import get_logger

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
    "http_login": ("security", "HIGH"),
    "invalid_config": ("config_error", "HIGH"),
}


def classify_notification(notification_id: str) -> tuple[str, str]:
    """Return (category, severity) for a notification_id using rule-based lookup."""
    return _CLASSIFICATION_MAP.get(notification_id, ("other", "MEDIUM"))


def extract_ip_from_message(message: str) -> Optional[str]:
    """Extract the first IPv4 address preceded by 'from ' in a notification message."""
    m = _IP_PATTERN.search(message)
    return m.group(1) if m else None


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
        context_parts.append(f"Enriched context: {enriched_context}")
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

    if notification_id == "http_login" and HA_NOTIFICATION_ENRICH_AUTH_FAILURES:
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
) -> bool:
    """Insert notification into history if new; update last_seen_at if existing.

    Returns True if newly seen (first time), False if already in history.
    """
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute(
            "SELECT notification_id FROM notification_history WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE notification_history SET last_seen_at = ? WHERE notification_id = ?",
                (now, notification_id),
            )
            return False
        conn.execute(
            """
            INSERT INTO notification_history
                (notification_id, first_seen_at, last_seen_at, category, severity)
            VALUES (?, ?, ?, ?, ?)
            """,
            (notification_id, now, now, category, severity),
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
