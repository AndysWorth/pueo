"""NetAlertX event router — routes REST /events to HITL notification cards.

Usage:
  router = NetAlertXEventRouter(notifier, gate)
  await router.route_rest_event(event_dict)   # called from event_poll.py
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from utils.agent.autonomy import RiskLevel
from utils.hitl.card_types import CARD_TYPE_NETALERTX_NEW_DEVICE
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from utils.hitl.notify import NotifierProtocol

log = get_logger("netalertx.event_router")


class NetAlertXEventRouter:
    """Route and deduplicate REST /events new-device alerts.

    Tracks recently-seen MAC addresses within ``dedup_window_seconds``.
    When a new-device event arrives the router deduplicates, checks the
    autonomy gate, and sends an approval notification card when required.
    """

    def __init__(
        self,
        notifier: "NotifierProtocol",
        gate: Any,
        dedup_window_seconds: float = 60.0,
    ) -> None:
        self._notifier = notifier
        self._gate = gate
        self._dedup_window = dedup_window_seconds
        self._seen: dict[str, float] = {}

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
        key = mac.upper().strip() if mac.strip() else f"unknown_{time.monotonic()}"
        if self._is_duplicate(key):
            log.info("netalertx_new_device_deduplicated", mac=mac)
            return
        if self._gate.should_auto_execute(RiskLevel.LOW):
            log.info("netalertx_new_device_auto_logged", mac=mac, ip=ip, vendor=vendor)
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
                "suppression_key": f"netalertx:new_device:{mac.upper()}",
                "severity": "INFO",
                "mac": mac,
                "ip": ip,
                "vendor": vendor,
                "name": name,
            },
        )
        log.info("netalertx_new_device_card_sent", mac=mac)
