"""NetAlertX event router — merges MQTT alert stream with webhook events, deduplicates.

Typed MQTT event models:
  NewDeviceAlertEvent  — NetAlertX/alert/+
  DeviceStateEvent     — NetAlertX/device/+/state
  ScanCompleteEvent    — NetAlertX/scan/complete

Usage:
  router = NetAlertXEventRouter(notifier, gate)
  asyncio.create_task(router.run(alert_queue))   # consumes (topic, payload) tuples
  await router.route_webhook(mac, event_type)    # called from HA notification poller
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Union

from pydantic import BaseModel

from utils.autonomy import RiskLevel
from utils.card_types import CARD_TYPE_NETALERTX_NEW_DEVICE
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.event_router")


# ---------------------------------------------------------------------------
# Typed event models
# ---------------------------------------------------------------------------


class NewDeviceAlertEvent(BaseModel):
    mac: str
    ip: str = ""
    name: str = ""
    vendor: str = ""
    topic: str = ""


class DeviceStateEvent(BaseModel):
    mac: str
    state: str
    topic: str = ""


class ScanCompleteEvent(BaseModel):
    topic: str = "NetAlertX/scan/complete"
    payload: str = ""


NetAlertXMQTTEvent = Union[NewDeviceAlertEvent, DeviceStateEvent, ScanCompleteEvent]


# ---------------------------------------------------------------------------
# MQTT payload parser
# ---------------------------------------------------------------------------


def parse_mqtt_event(topic: str, payload: str) -> NetAlertXMQTTEvent | None:
    """Parse a raw MQTT topic+payload into a typed event, or None if unrecognised."""
    if topic.startswith("NetAlertX/alert/"):
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            data = {}
        return NewDeviceAlertEvent(
            mac=data.get("mac", data.get("eveMac", "")),
            ip=data.get("ip", data.get("eveIp", "")),
            name=data.get("name", data.get("devName", "")),
            vendor=data.get("vendor", data.get("devVendor", "")),
            topic=topic,
        )
    if topic.startswith("NetAlertX/device/") and topic.endswith("/state"):
        parts = topic.split("/")
        mac = parts[2] if len(parts) >= 4 else ""  # NetAlertX/device/<mac>/state
        return DeviceStateEvent(mac=mac, state=payload, topic=topic)
    if topic == "NetAlertX/scan/complete":
        return ScanCompleteEvent(topic=topic, payload=payload)
    return None


# ---------------------------------------------------------------------------
# Event router
# ---------------------------------------------------------------------------


class NetAlertXEventRouter:
    """Route and deduplicate MQTT alert and webhook new-device events.

    Tracks recently-seen MAC addresses within ``dedup_window_seconds``.
    When a new-device alert arrives (via MQTT or webhook callback), the router
    deduplicates, checks the autonomy gate, and sends an approval notification card
    when the gate requires it.
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
    # MQTT routing
    # ------------------------------------------------------------------

    async def route(self, topic: str, payload: str) -> None:
        """Route a raw MQTT message to the appropriate handler."""
        event = parse_mqtt_event(topic, payload)
        if isinstance(event, NewDeviceAlertEvent):
            await self._handle_new_device(
                mac=event.mac, ip=event.ip, vendor=event.vendor, name=event.name
            )
        elif isinstance(event, DeviceStateEvent):
            log.info("netalertx_device_state", mac=event.mac, state=event.state)
        elif isinstance(event, ScanCompleteEvent):
            log.info("netalertx_scan_complete")
        else:
            log.debug("netalertx_event_unrecognised", topic=topic)

    async def run(self, alert_queue: "asyncio.Queue[tuple[str, str]]") -> None:
        """Consume (topic, payload) tuples from alert_queue; runs until cancelled."""
        while True:
            try:
                topic, payload = await alert_queue.get()
                await self.route(topic, payload)
            except asyncio.CancelledError:
                log.info("netalertx_event_router_cancelled")
                return

    # ------------------------------------------------------------------
    # Webhook routing (called from HA notification poller)
    # ------------------------------------------------------------------

    async def route_webhook(self, mac: str, event_type: str = "") -> None:
        """Route a new-device event that arrived via the HA webhook automation."""
        await self._handle_new_device(mac=mac, ip="", vendor="", name="")

    # ------------------------------------------------------------------
    # Shared new-device handler
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
