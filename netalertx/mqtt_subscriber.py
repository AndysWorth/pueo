"""NetAlertX MQTT subscriber — device presence events via aiomqtt (item 16).

Subscribes to:
  system-sensors/binary_sensor/+/state   — feeds DevicePresenceEvent into queue
  system-sensors/sensor/+/state          — feeds DevicePresenceEvent into queue
  NetAlertX/alert/+                      — feeds (topic, payload) into alert_queue
  NetAlertX/device/+/state               — feeds (topic, payload) into alert_queue
  NetAlertX/scan/complete                — feeds (topic, payload) into alert_queue

Reconnects automatically on broker disconnection.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import aiomqtt
from pydantic import BaseModel

from utils.core.logging import get_logger

log = get_logger("netalertx.mqtt_subscriber")

_TOPICS = [
    "system-sensors/binary_sensor/+/state",
    "system-sensors/sensor/+/state",
]

_NAX_TOPICS = [
    "NetAlertX/alert/+",
    "NetAlertX/device/+/state",
    "NetAlertX/scan/complete",
]


class DevicePresenceEvent(BaseModel):
    topic: str
    payload: str


class MQTTSubscriber:
    """Async MQTT subscriber that reconnects on broker drop."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        reconnect_delay: float = 5.0,
        username: str = "",
        password: str = "",  # nosec B107 — empty string signals anonymous; caller supplies real value
    ) -> None:
        self._host = host
        self._port = port
        self._reconnect_delay = reconnect_delay
        self._username = username or None
        self._password = password or None

    async def subscribe(
        self,
        queue: "asyncio.Queue[DevicePresenceEvent]",
        alert_queue: "Optional[asyncio.Queue[tuple[str, str]]]" = None,
    ) -> None:
        """Subscribe and feed events into queues. Runs until cancelled.

        system-sensors messages → queue (DevicePresenceEvent).
        NetAlertX/* messages → alert_queue as (topic, payload) if provided.
        """
        while True:
            try:
                async with aiomqtt.Client(
                    self._host,
                    self._port,
                    username=self._username,
                    password=self._password,
                ) as client:
                    for topic in _TOPICS + _NAX_TOPICS:
                        await client.subscribe(topic)
                    log.info(
                        "mqtt_subscriber_connected",
                        host=self._host,
                        port=self._port,
                    )
                    async for message in client.messages:
                        topic_str = str(message.topic)
                        payload_str = message.payload.decode(errors="replace")
                        if topic_str.startswith("NetAlertX/"):
                            if alert_queue is not None:
                                await alert_queue.put((topic_str, payload_str))
                        else:
                            await queue.put(
                                DevicePresenceEvent(
                                    topic=topic_str, payload=payload_str
                                )
                            )
            except asyncio.CancelledError:
                log.info("mqtt_subscriber_cancelled")
                return
            except aiomqtt.MqttError as exc:
                log.warning(
                    "mqtt_subscriber_disconnected",
                    error=str(exc),
                    reconnect_in=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)


async def probe_mqtt_active(
    host: str,
    port: int = 1883,
    timeout: float = 5.0,
    username: str = "",
    password: str = "",
) -> bool:  # pragma: no cover
    """Return True if the broker is reachable and any message arrives within timeout.

    Subscribes to '#' (all topics) so any publisher on the broker satisfies the
    probe — not just the narrow _TOPICS the daemon uses. Returns False on timeout
    or any broker connection error.
    """
    _user = username or None
    _pass = password or None
    try:
        async with aiomqtt.Client(host, port, username=_user, password=_pass) as client:
            await client.subscribe("#")
            try:
                async with asyncio.timeout(timeout):
                    async for _ in client.messages:
                        return True
            except TimeoutError:
                pass
    except aiomqtt.MqttError:
        pass
    return False


class FakeMQTTSubscriber:
    """Test double — puts pre-configured events into the queue, then optionally raises."""

    def __init__(
        self,
        events: list[DevicePresenceEvent] | None = None,
        alert_events: list[tuple[str, str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._events: list[DevicePresenceEvent] = events or []
        self._alert_events: list[tuple[str, str]] = alert_events or []
        self._error = error
        self.subscribe_calls: int = 0

    async def subscribe(
        self,
        queue: "asyncio.Queue[DevicePresenceEvent]",
        alert_queue: "Optional[asyncio.Queue[tuple[str, str]]]" = None,
    ) -> None:
        self.subscribe_calls += 1
        for event in self._events:
            await queue.put(event)
        if alert_queue is not None:
            for item in self._alert_events:
                await alert_queue.put(item)
        if self._error is not None:
            raise self._error
