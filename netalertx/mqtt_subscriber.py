"""NetAlertX MQTT subscriber — device presence events via aiomqtt (item 16).

Subscribes to:
  system-sensors/binary_sensor/+/state
  system-sensors/sensor/+/state

Feeds DevicePresenceEvent into an asyncio.Queue consumed by health.py.
Reconnects automatically on broker disconnection.
"""

from __future__ import annotations

import asyncio

import aiomqtt
from pydantic import BaseModel

from utils.logging import get_logger

log = get_logger("netalertx.mqtt_subscriber")

_TOPICS = [
    "system-sensors/binary_sensor/+/state",
    "system-sensors/sensor/+/state",
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
    ) -> None:
        self._host = host
        self._port = port
        self._reconnect_delay = reconnect_delay

    async def subscribe(self, queue: asyncio.Queue[DevicePresenceEvent]) -> None:
        """Subscribe and feed events into queue. Runs until cancelled."""
        while True:
            try:
                async with aiomqtt.Client(self._host, self._port) as client:
                    for topic in _TOPICS:
                        await client.subscribe(topic)
                    log.info(
                        "mqtt_subscriber_connected",
                        host=self._host,
                        port=self._port,
                    )
                    async for message in client.messages:
                        event = DevicePresenceEvent(
                            topic=str(message.topic),
                            payload=message.payload.decode(errors="replace"),
                        )
                        await queue.put(event)
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
    host: str, port: int = 1883, timeout: float = 5.0
) -> bool:  # pragma: no cover
    """Return True if the broker accepts a connection and publishes a message within timeout.

    Connects, subscribes to _TOPICS, and waits up to `timeout` seconds for one
    message. Returns False on timeout or any broker connection error.
    """
    try:
        async with aiomqtt.Client(host, port) as client:
            for topic in _TOPICS:
                await client.subscribe(topic)
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
        error: Exception | None = None,
    ) -> None:
        self._events: list[DevicePresenceEvent] = events or []
        self._error = error
        self.subscribe_calls: int = 0

    async def subscribe(self, queue: asyncio.Queue[DevicePresenceEvent]) -> None:
        self.subscribe_calls += 1
        for event in self._events:
            await queue.put(event)
        if self._error is not None:
            raise self._error
