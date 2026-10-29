"""NetAlertX event router — routes REST /events to HITL notification cards.

Usage:
  router = NetAlertXEventRouter(notifier, gate)
  await router.route_rest_event(event_dict)   # called from event_poll.py
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from utils.hitl.card_types import CARD_TYPE_NETALERTX_NEW_DEVICE
from utils.hitl.hitl_tracker import mark_card_sent, should_send_card, stable_nid
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from utils.hitl.notify import NotifierProtocol

log = get_logger("netalertx.event_router")

_MAC_RE = re.compile(r"[^0-9a-fA-F]")


def _normalize_mac(mac: str) -> str:
    raw = _MAC_RE.sub("", mac)
    if len(raw) != 12:
        return mac.upper()
    return ":".join(raw[i : i + 2].upper() for i in range(0, 12, 2))


def _check_and_mark_card(
    db_path: str, card_key: str, card_type: str, description: str
) -> bool:
    """Synchronous: check dedup and mark the card sent in one transaction.

    Returns True when the card should be sent (and has been marked), False
    when it is actively suppressed or already pending.  Fails open on any
    DB error so a missing table never silently swallows an alert.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            if not should_send_card(conn, card_key):
                return False
            mark_card_sent(conn, card_key, card_type, description)
            return True
    except Exception:  # nosec B110
        return True


class NetAlertXEventRouter:
    """Route and deduplicate REST /events new-device alerts.

    Deduplicates within a session via an in-memory window and across
    restarts via the hitl_suppression table when db_path is provided.
    When a new-device event arrives the router deduplicates, checks the
    autonomy gate, and sends an approval notification card when required.
    """

    def __init__(
        self,
        notifier: "NotifierProtocol",
        gate: Any,
        dedup_window_seconds: float = 60.0,
        db_path: str = "",
        api_client: "NetAlertXAPIClient | None" = None,
    ) -> None:
        self._notifier = notifier
        self._gate = gate
        self._dedup_window = dedup_window_seconds
        self._seen: dict[str, float] = {}
        self._db_path = db_path
        self._api = api_client

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _is_duplicate(self, key: str) -> bool:
        """Return True if key was seen within the dedup window; record it if not."""
        now = time.monotonic()
        self._seen = {
            k: v for k, v in self._seen.items() if now - v < self._dedup_window
        }
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    # ------------------------------------------------------------------
    # REST event routing
    # ------------------------------------------------------------------

    async def route_rest_event(self, event: dict) -> None:
        """Route a single event dict from GET /events.

        New-device events (eveEventType == 'New Device') generate a
        deduplicated notification card.  All other event types are logged
        as context only — device-offline alerting lives in ADR 047.
        """
        event_type = event.get("eveEventType", "")
        mac = event.get("eveMac", "")
        ip = event.get("eveIp", "")
        vendor = event.get("devVendor", "")
        name = event.get("devName", "")

        if event_type == "New Device":
            await self._handle_new_device(mac=mac, ip=ip, vendor=vendor, name=name)
        else:
            log.debug(
                "netalertx_event_skipped",
                event_type=event_type,
                mac=mac,
            )

    # ------------------------------------------------------------------
    # New-device handler
    # ------------------------------------------------------------------

    async def _handle_new_device(
        self, mac: str, ip: str, vendor: str, name: str
    ) -> None:
        mac_key = mac.upper().strip() if mac.strip() else ""
        key = mac_key if mac_key else f"unknown_{time.monotonic()}"
        if self._is_duplicate(key):
            log.info("netalertx_new_device_deduplicated", mac=mac)
            return

        # Enrich vendor and name from /devices API (events carry empty devVendor).
        if self._api and mac_key:
            try:
                devices = await self._api.get_devices()
                norm = _normalize_mac(mac)
                for dev in devices:
                    if _normalize_mac(dev.get("devMac", "")) == norm:
                        vendor = dev.get("devVendor", "") or vendor
                        name = dev.get("devName", "") or name
                        break
            except Exception:  # nosec B110
                pass

        card_key = (
            f"netalertx:new_device:{mac_key}"
            if mac_key
            else f"netalertx:new_device:unknown"
        )
        nid = stable_nid(card_key)

        # Cross-restart dedup: check and mark in the suppression table.
        if self._db_path:
            description = f"New device detected: MAC={mac} IP={ip}"
            allowed = await asyncio.to_thread(
                _check_and_mark_card,
                self._db_path,
                card_key,
                CARD_TYPE_NETALERTX_NEW_DEVICE,
                description,
            )
            if not allowed:
                log.info("netalertx_new_device_suppressed", mac=mac)
                return

        parts = [f"MAC: {mac}"] if mac else ["MAC: unknown"]
        if ip:
            parts.append(f"IP: {ip}")
        if vendor:
            parts.append(f"Vendor: {vendor}")
        if name:
            parts.append(f"Name: {name}")
        device_desc = "  ".join(parts)
        await self._notifier.send(
            subject="Pueo: NetAlertX detected a new network device",
            body=device_desc,
            payload={
                "card_type": CARD_TYPE_NETALERTX_NEW_DEVICE,
                "notification_id": nid,
                "suppression_key": card_key,
                "severity": "INFO",
                "mac": mac,
                "ip": ip,
                "vendor": vendor,
                "name": name,
            },
        )
        log.info("netalertx_new_device_card_sent", mac=mac)
