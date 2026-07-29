"""Integration tests: NetAlertX cross-module seams.

Tests the continuous health monitor loop (currently 0% covered) and the
installer-HITL-card → dashboard rendering path.
"""

import asyncio
import json

import pytest


class TestHealthMonitorContinuousLoop:
    """NetAlertXHealthMonitor.run() — lines 145–166 of netalertx/health.py are 0% covered.

    Uses FakeMQTTSubscriber (already defined in mqtt_subscriber.py) so no live
    MQTT broker is needed.
    """

    def _make_api_client(self, devices=None):
        class _FakeAPI:
            def __init__(self, devices):
                self._devices = devices or []

            async def get_devices(self):
                return self._devices

            async def get_about(self):
                return {"version": "v26.7.1"}

        return _FakeAPI(devices)

    def test_run_starts_mqtt_subscriber_and_polls_once(self, monkeypatch):
        """run() starts MQTT subscriber task and calls poll_once at least once."""
        from netalertx.health import NetAlertXHealthMonitor
        from netalertx.mqtt_subscriber import DevicePresenceEvent, FakeMQTTSubscriber

        api = self._make_api_client(devices=[])
        events = [
            DevicePresenceEvent(
                topic="NetAlertX/presence/AA:BB:CC:DD:EE:FF/state", payload="home"
            )
        ]
        mqtt = FakeMQTTSubscriber(events=events)

        poll_calls = {"n": 0}
        original_poll = NetAlertXHealthMonitor.poll_once

        async def counting_poll(self, queue):
            poll_calls["n"] += 1
            result = await original_poll(self, queue)
            await asyncio.sleep(
                0
            )  # yield so subscriber task can start before we cancel
            raise asyncio.CancelledError()

        monkeypatch.setattr(NetAlertXHealthMonitor, "poll_once", counting_poll)

        monitor = NetAlertXHealthMonitor(api_client=api, mqtt_subscriber=mqtt)
        try:
            asyncio.run(monitor.run())
        except asyncio.CancelledError:
            pass

        assert poll_calls["n"] == 1
        assert mqtt.subscribe_calls == 1

    def test_run_without_mqtt_subscriber_does_not_raise(self, monkeypatch):
        """run() with mqtt_subscriber=None polls once and exits cleanly."""
        from netalertx.health import NetAlertXHealthMonitor

        api = self._make_api_client(devices=[])

        async def one_shot_poll(self, queue):
            raise asyncio.CancelledError()

        monkeypatch.setattr(NetAlertXHealthMonitor, "poll_once", one_shot_poll)

        monitor = NetAlertXHealthMonitor(api_client=api, mqtt_subscriber=None)
        try:
            asyncio.run(monitor.run())
        except asyncio.CancelledError:
            pass  # expected — loop cancelled cleanly

    def test_run_mqtt_subscriber_error_cancels_task(self, monkeypatch):
        """Subscriber error causes the MQTT task to fail; monitor still polls."""
        from netalertx.health import NetAlertXHealthMonitor
        from netalertx.mqtt_subscriber import FakeMQTTSubscriber

        api = self._make_api_client(devices=[])
        mqtt = FakeMQTTSubscriber(error=RuntimeError("broker down"))

        async def one_shot_poll(self, queue):
            await asyncio.sleep(0)  # yield so the subscriber task can run
            raise asyncio.CancelledError()

        monkeypatch.setattr(NetAlertXHealthMonitor, "poll_once", one_shot_poll)

        monitor = NetAlertXHealthMonitor(api_client=api, mqtt_subscriber=mqtt)
        try:
            asyncio.run(monitor.run())
        except (asyncio.CancelledError, RuntimeError):
            # RuntimeError propagates when a failed mqtt_task is awaited in the finally block
            pass

        # Subscriber was attempted even though it errored
        assert mqtt.subscribe_calls == 1

    def test_poll_once_drains_mqtt_queue_and_sets_mqtt_active(self):
        """poll_once detects MQTT events from the queue and sets mqtt_active=True."""
        from netalertx.health import NetAlertXHealthMonitor
        from netalertx.mqtt_subscriber import DevicePresenceEvent

        api = self._make_api_client(devices=[])
        monitor = NetAlertXHealthMonitor(api_client=api)

        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(DevicePresenceEvent(topic="t/state", payload="home"))

        report = asyncio.run(monitor.poll_once(queue))
        assert report.mqtt_active is True
        assert queue.empty()  # drained

    def test_poll_once_empty_queue_mqtt_inactive(self):
        """poll_once with no MQTT events produces mqtt_active=False."""
        from netalertx.health import NetAlertXHealthMonitor

        api = self._make_api_client(devices=[])
        monitor = NetAlertXHealthMonitor(api_client=api)

        report = asyncio.run(monitor.poll_once(asyncio.Queue()))
        assert report.mqtt_active is False


class TestInstallerCardToRoute:
    """Installer HITL cards written via FileNotifier → GET / dashboard renders them.

    Existing installer tests use FakeNotifier (in-memory). This class uses
    FileNotifier so the card JSON lands on disk, then verifies the dashboard
    can load and render it.
    """

    def test_installer_failure_card_appears_on_dashboard_index(
        self, monkeypatch, tmp_path
    ):
        """When the installer cannot verify MQTT, a card file is written and the dashboard shows it."""
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils.notify import FileNotifier

        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        # Simulate what the installer would write: a JSON card for a step failure
        card_id = "installer_step2_failure"
        card = {
            "notification_id": card_id,
            "subject": "NetAlertX Setup — Step 2 Failed",
            "body": "MQTT broker not responding on port 1883",
            "payload": {
                "notification_id": card_id,
                "step": 2,
                "error": "Connection refused on port 1883",
                "diagnosis": {
                    "error": "Connection refused on port 1883",
                    "analysis": "Mosquitto may not be running",
                },
            },
            "sent_at": 1700000000,
        }
        (watch_dir / f"{card_id}.json").write_text(json.dumps(card))

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "NetAlertX Setup" in resp.text or "Step 2" in resp.text

    def test_approve_installer_card_writes_signal_file(self, monkeypatch, tmp_path):
        """POST /approve/{card_id} for a non-YAML-fix card creates a .approved signal file."""
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard

        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        card_id = "installer_step2_failure"
        card = {
            "notification_id": card_id,
            "subject": "NetAlertX Setup Step 2",
            "body": "Retry?",
            "payload": {"notification_id": card_id},
            "sent_at": 1700000000,
        }
        (watch_dir / f"{card_id}.json").write_text(json.dumps(card))

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post(f"/approve/{card_id}", follow_redirects=False)
        assert resp.status_code == 303
        assert (watch_dir / f"{card_id}.approved").exists()
