#!/usr/bin/env python3
"""NetAlertX subsystem tests — detector, API client, installer, device name sync, log/health monitoring, AI diagnosis, healing, maintenance, version guard."""

import asyncio
import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).parent.parent
# ── netalertx/detector.py ───────────────────────────────────────────────────────


class TestNetAlertXDetector:
    def _make_http_client(self, version: str):
        import httpx
        import json

        class _VersionTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                body = json.dumps({"success": True, "value": version}).encode()
                return httpx.Response(
                    200,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )

        return httpx.AsyncClient(transport=_VersionTransport())

    def test_supervisor_path_returns_addon_mode(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={"ha supervisor info": (0, "supervisor: ok\n", "")}
        )
        http = self._make_http_client("v26.7.1")
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert result.mode == "addon"
        assert result.api_base_url == "http://ha.local:20212"
        assert result.version == "v26.7.1"
        assert result.log_path == "/data/app.log"
        assert result.container_name == "netalertx"

    def test_docker_fallback_when_supervisor_fails(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "command not found"),
                "docker info": (0, "Server Version: 24.0\n", ""),
            }
        )
        http = self._make_http_client("v26.7.1")
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert result.mode == "docker"

    def test_raises_when_neither_supervisor_nor_docker(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "not found"),
                "docker info": (1, "", "not found"),
            }
        )
        http = self._make_http_client("")
        with pytest.raises(RuntimeError, match="deployment detection failed"):
            asyncio.run(
                detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
            )

    def test_version_empty_when_api_unreachable(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment
        import httpx

        class _ErrorTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("refused")

        ssh = FakeSSHClient(command_results={"ha supervisor info": (0, "ok", "")})
        http = httpx.AsyncClient(transport=_ErrorTransport())
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert result.version == ""

    def test_supervisor_command_is_tried_first(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "docker info": (0, "ok", ""),
            }
        )
        http = self._make_http_client("")
        asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert "ha supervisor info" in ssh.commands_run
        # Docker should not be probed when supervisor succeeds
        assert "docker info" not in ssh.commands_run

    def test_fetch_version_without_injected_client(self, monkeypatch):
        import netalertx.detector as det_mod
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        version_client = self._make_http_client("v99.0.0")
        monkeypatch.setattr(
            det_mod.httpx, "AsyncClient", lambda **kwargs: version_client
        )

        ssh = FakeSSHClient(command_results={"ha supervisor info": (0, "ok", "")})
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=None)
        )
        assert result.version == "v99.0.0"


# ── netalertx/api_client.py ─────────────────────────────────────────────────────


class TestNetAlertXAPIClient:
    def _mock_client(self, routes: list):
        """Build an httpx.AsyncClient backed by a simple in-memory transport.

        routes: list of (method, url_fragment, status_code, json_body)
        """
        import httpx
        import json

        class _MockTransport(httpx.AsyncBaseTransport):
            def __init__(self, routes):
                self._routes = routes

            async def handle_async_request(self, request):
                for method, fragment, status, body in self._routes:
                    if request.method == method and fragment in str(request.url):
                        if isinstance(body, str):
                            content = body.encode()
                            ct = "text/plain"
                        else:
                            content = json.dumps(body).encode()
                            ct = "application/json"
                        return httpx.Response(
                            status,
                            content=content,
                            headers={"Content-Type": ct},
                        )
                return httpx.Response(
                    404,
                    content=b'{"error":"not found"}',
                    headers={"Content-Type": "application/json"},
                )

        return httpx.AsyncClient(transport=_MockTransport(routes))

    def _client(self, routes):
        from netalertx.api_client import NetAlertXAPIClient

        return NetAlertXAPIClient(
            base_url="http://nax.local:20212",
            api_token="testtoken",
            http_client=self._mock_client(routes),
        )

    def test_get_devices_returns_list(self):
        devices = [
            {"devMAC": "AA:BB:CC:DD:EE:FF", "devName": "laptop"},
            {"devMAC": "11:22:33:44:55:66", "devName": "phone"},
        ]
        c = self._client(
            [("GET", "/devices", 200, {"success": True, "devices": devices})]
        )
        result = asyncio.run(c.get_devices())
        assert len(result) == 2
        assert result[0]["devMAC"] == "AA:BB:CC:DD:EE:FF"

    def test_get_events_returns_list(self):
        events = [
            {
                "eveMac": "AA:BB:CC:DD:EE:FF",
                "eveIp": "192.168.1.10",
                "eveEventType": "New Device",
            }
        ]
        c = self._client([("GET", "/events", 200, {"success": True, "events": events})])
        result = asyncio.run(c.get_events())
        assert len(result) == 1
        assert result[0]["eveEventType"] == "New Device"

    def test_get_metrics_parses_prometheus_text(self):
        prometheus_text = (
            "# HELP netalertx_connected_devices\n"
            "# TYPE netalertx_connected_devices gauge\n"
            "netalertx_connected_devices 31\n"
            "netalertx_offline_devices 54\n"
            "netalertx_down_devices 0\n"
            'netalertx_device_status{device="laptop",mac="AA:BB"} 1\n'
        )
        c = self._client([("GET", "/metrics", 200, prometheus_text)])
        result = asyncio.run(c.get_metrics())
        assert result["connected_devices"] == 31.0
        assert result["offline_devices"] == 54.0
        assert result["down_devices"] == 0.0
        assert "device_status" not in result  # labeled metric skipped

    def test_get_settings_via_graphql(self):
        gql_response = {
            "data": {
                "settings": {
                    "settings": [
                        {"setKey": "LOADED_PLUGINS", "setValue": "ARPSCAN,MQTT"},
                        {"setKey": "MQTT_BROKER", "setValue": "192.168.1.1"},
                    ],
                    "count": 2,
                }
            }
        }
        c = self._client([("POST", "/graphql", 200, gql_response)])
        result = asyncio.run(c.get_settings())
        assert result["LOADED_PLUGINS"] == "ARPSCAN,MQTT"
        assert result["MQTT_BROKER"] == "192.168.1.1"

    def test_trigger_scan_posts_to_correct_endpoint(self):
        c = self._client(
            [
                (
                    "POST",
                    "/nettools/trigger-scan",
                    200,
                    {"success": True, "message": "queued"},
                )
            ]
        )
        # Should not raise
        asyncio.run(c.trigger_scan())

    def test_trigger_scan_raises_on_error(self):
        c = self._client(
            [
                (
                    "POST",
                    "/nettools/trigger-scan",
                    500,
                    {"success": False, "error": "fail"},
                )
            ]
        )
        with pytest.raises(Exception):
            asyncio.run(c.trigger_scan())

    def test_get_about_returns_health_dict(self):
        health = {"success": True, "db_size_mb": 12.4, "mem_usage_pct": 45}
        c = self._client([("GET", "/health", 200, health)])
        result = asyncio.run(c.get_about())
        assert result["success"] is True
        assert result["db_size_mb"] == 12.4

    def test_get_devices_empty_list_when_no_devices(self):
        c = self._client([("GET", "/devices", 200, {"success": True, "devices": []})])
        result = asyncio.run(c.get_devices())
        assert result == []

    def test_get_without_injected_client_creates_own_session(self, monkeypatch):
        import httpx
        import netalertx.api_client as ac_mod
        from netalertx.api_client import NetAlertXAPIClient

        mock = self._mock_client(
            [("GET", "/devices", 200, {"success": True, "devices": []})]
        )
        monkeypatch.setattr(ac_mod.httpx, "AsyncClient", lambda **kwargs: mock)

        c = NetAlertXAPIClient(base_url="http://nax.local:20212", api_token="tok")
        result = asyncio.run(c.get_devices())
        assert result == []

    def test_post_without_injected_client_creates_own_session(self, monkeypatch):
        import netalertx.api_client as ac_mod
        from netalertx.api_client import NetAlertXAPIClient

        mock = self._mock_client(
            [
                (
                    "POST",
                    "/nettools/trigger-scan",
                    200,
                    {"success": True, "message": "ok"},
                )
            ]
        )
        monkeypatch.setattr(ac_mod.httpx, "AsyncClient", lambda **kwargs: mock)

        c = NetAlertXAPIClient(base_url="http://nax.local:20212", api_token="tok")
        asyncio.run(c.trigger_scan())

    def test_parse_prometheus_metrics_skips_invalid_float(self):
        prometheus_text = (
            "netalertx_connected_devices 31\n" "netalertx_bad_metric not_a_number\n"
        )
        c = self._client([("GET", "/metrics", 200, prometheus_text)])
        result = asyncio.run(c.get_metrics())
        assert result["connected_devices"] == 31.0
        assert "bad_metric" not in result

    def test_lock_device_field_raises_on_empty_mac(self):
        c = self._client([])
        with pytest.raises(ValueError, match="non-empty MAC"):
            asyncio.run(c.lock_device_field("", "devName"))


# ── netalertx SQLite migration ──────────────────────────────────────────────────


class TestNetAlertXMigration:
    def test_migration_creates_install_state_table(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()

        with sqlite3.connect(db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "netalertx_install_state" in tables

    def test_install_state_table_has_correct_columns(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()

        with sqlite3.connect(db) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(netalertx_install_state)")
            }
        assert {"id", "state", "correlation_id", "timestamp", "details_json"} == cols

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()  # second run must not raise

        with sqlite3.connect(db) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 4


# ── netalertx/installer.py (steps 1–4) ───────────────────────────────────────


def _make_installer_db(tmp_path, monkeypatch):
    """Create and migrate a test SQLite DB, patch DB_PATH in installer module."""
    import ha_agent_advanced
    import netalertx.installer as inst

    db = tmp_path / "installer_test.db"
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
    ha_agent_advanced.init_local_database()
    monkeypatch.setattr(inst, "DB_PATH", str(db))
    return str(db)


class TestNetAlertXInstallerSteps1to4:
    # ── helpers ──────────────────────────────────────────────────────────────

    def _make_supervisor_ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "supervisor_info: ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 192.168.1.1 dev eth0 proto dhcp",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (
                    0,
                    "slug: netalertx_fa\nrepository: alexbelgium/hassio-addons",
                    "",
                ),
            }
        )

    def _make_docker_ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "not found"),
                "docker info": (0, "docker info ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 10.0.0.1 dev wlan0 proto dhcp",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (0, "", ""),
            }
        )

    def _notifier(self, approve: bool = True):
        from utils.notify import FakeNotifier

        return FakeNotifier(approve=approve)

    def _gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def _gate_ask(self):
        """Gate that always requests HITL approval (outcome determined by notifier)."""
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=False)

    # ── step 1: detect deployment ─────────────────────────────────────────────

    def test_step1_supervisor_sets_mode_addon(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        state = asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        assert state == "ADDON_REPO_ADDED"
        _, details = _read_install_state(db)
        assert details["mode"] == "addon"

    def test_step1_docker_fallback_requires_approval(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "wlan0")

        gate = self._gate_ask()
        state = asyncio.run(
            run_steps_1_to_4(
                self._make_docker_ssh(), gate, self._notifier(approve=True), db_path=db
            )
        )
        assert state == "ADDON_REPO_ADDED"
        _, details = _read_install_state(db)
        assert details["mode"] == "docker"
        assert any(
            c["subject"] == "NetAlertX installer: Docker fallback"
            for c in gate.require_approval_calls
        )

    def test_step1_docker_fallback_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        state = asyncio.run(
            run_steps_1_to_4(
                self._make_docker_ssh(),
                self._gate_ask(),
                self._notifier(approve=False),
                db_path=db,
            )
        )
        assert state == "NOT_INSTALLED"

    def test_step1_no_target_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", ""),
                "docker info": (1, "", ""),
            }
        )
        gate = self._gate_auto()
        state = asyncio.run(run_steps_1_to_4(ssh, gate, self._notifier(), db_path=db))
        assert state == "NOT_INSTALLED"
        # CRITICAL approval requested even though auto_execute_result=True
        # (level 4 still notifies on CRITICAL, but gate returns True — state won't advance
        #  because no mode was set and step returns False)
        assert any(
            "no deployment target" in c["subject"] for c in gate.require_approval_calls
        )

    # ── step 2: mosquitto ─────────────────────────────────────────────────────

    def test_step2_mosquitto_already_running_skips_install(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        gate = self._gate_auto()
        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(), gate, self._notifier(), db_path=db
            )
        )
        # No Mosquitto install approval should have been requested
        assert not any(
            "install Mosquitto" in c["subject"] for c in gate.require_approval_calls
        )

    def test_normalize_addon_state_maps_started_to_running(self):
        from netalertx.installer import _normalize_addon_state

        assert _normalize_addon_state("started") == "running"
        assert _normalize_addon_state("STARTED") == "running"
        assert _normalize_addon_state("running") == "running"
        assert _normalize_addon_state("stopped") == "stopped"
        assert _normalize_addon_state("error") == "error"

    def test_step2_mosquitto_state_started_treated_as_running(
        self, tmp_path, monkeypatch
    ):
        """HA supervisor returns 'started', not 'running' — installer must accept it."""
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "supervisor_info: ok", ""),
                "ha apps info core_mosquitto": (0, "state: started", ""),
                "ip route show default": (
                    0,
                    "default via 192.168.1.1 dev eth0 proto dhcp",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (
                    0,
                    "slug: netalertx_fa\nrepository: alexbelgium/hassio-addons",
                    "",
                ),
            }
        )
        gate = self._gate_auto()
        ok = asyncio.run(run_steps_1_to_4(ssh, gate, self._notifier(), db_path=db))
        assert ok
        state, _ = _read_install_state(db)
        # All 4 steps complete, so state advances past MQTT_RUNNING to ADDON_REPO_ADDED.
        assert state == "ADDON_REPO_ADDED"
        assert not any(
            "install Mosquitto" in c["subject"] for c in gate.require_approval_calls
        )

    def test_step2_mosquitto_not_installed_requires_approval(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        call_counts: dict[str, int] = {}

        class TrackingSSHClient(FakeSSHClient):
            async def run(self, command, check=False):  # type: ignore[override]
                call_counts[command] = call_counts.get(command, 0) + 1
                # After install+start, polling returns running
                if (
                    "ha apps info core_mosquitto" in command
                    and call_counts.get(command, 0) > 1
                ):
                    return 0, "state: running", ""
                return await super().run(command, check=check)

        ssh = TrackingSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "not found", ""),
                "ha apps install core_mosquitto": (0, "", ""),
                "ha apps start core_mosquitto": (0, "", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (0, "slug: netalertx_fa", ""),
            }
        )
        gate = self._gate_ask()
        asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=True), db_path=db)
        )
        assert any(
            "install Mosquitto" in c["subject"] for c in gate.require_approval_calls
        )

    def test_step2_mosquitto_install_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "not found", ""),
            }
        )
        state = asyncio.run(
            run_steps_1_to_4(
                ssh, self._gate_ask(), self._notifier(approve=False), db_path=db
            )
        )
        # State advanced to MQTT_INSTALLED (step 1 done) but not MQTT_RUNNING
        assert state == "MQTT_INSTALLED"

    # ── step 3: interface detection ───────────────────────────────────────────

    def test_step3_single_interface_auto_detected(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details["scan_interface"] == "eth0"

    def test_step3_config_interface_overrides_detection(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "br0")

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details["scan_interface"] == "br0"

    def test_step3_multiple_interfaces_requires_approval(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 1.1.1.1 dev eth0\ndefault via 2.2.2.2 dev wlan0",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (0, "slug: netalertx_fa", ""),
            }
        )
        gate = self._gate_ask()
        state = asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=True), db_path=db)
        )
        assert state == "ADDON_REPO_ADDED"
        _, details = _read_install_state(db)
        assert details["scan_interface"] == "eth0"
        assert any(
            "confirm scan interface" in c["subject"]
            for c in gate.require_approval_calls
        )

    def test_step3_multiple_interfaces_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 1.1.1.1 dev eth0\ndefault via 2.2.2.2 dev wlan0",
                    "",
                ),
            }
        )
        state = asyncio.run(
            run_steps_1_to_4(
                ssh, self._gate_ask(), self._notifier(approve=False), db_path=db
            )
        )
        assert state == "MQTT_RUNNING"

    def test_step3_no_interface_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (0, "no route found", ""),
            }
        )
        state = asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )
        assert state == "MQTT_RUNNING"

    # ── step 4: add repo + slug ────────────────────────────────────────────────

    def test_step4_repo_already_present_skips_add(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        ssh = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )

        add_calls = [c for c in ssh.commands_run if "repositories add" in c]
        assert len(add_calls) == 0

    def test_step4_repo_add_aborts_if_verify_fails(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr(
            "netalertx.installer.NETALERTX_ADDON_REPOSITORY_URL",
            "https://github.com/alexbelgium/hassio-addons",
        )

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                # List returns empty both times → add appears to fail
                "ha store repositories list": (0, "", ""),
                "ha store repositories add": (0, "", ""),
                "ha store addons": (0, "", ""),
            }
        )
        gate = self._gate_auto()
        state = asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=True), db_path=db)
        )
        assert state == "MQTT_RUNNING"
        assert any(
            "repository add failed" in c["subject"] for c in gate.require_approval_calls
        )

    def test_step4_slug_resolved_from_store(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details.get("addon_slug") == "netalertx_fa"

    def test_step4_slug_from_config_takes_precedence(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr(
            "netalertx.installer.NETALERTX_ADDON_SLUG", "my_custom_slug"
        )

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details["addon_slug"] == "my_custom_slug"

    # ── idempotency ────────────────────────────────────────────────────────────

    def test_second_run_is_noop_at_addon_repo_added(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        ssh = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )

        # Second run with a fresh SSH client tracking commands
        ssh2 = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh2, self._gate_auto(), self._notifier(), db_path=db)
        )

        # On second run no commands should have been executed (all steps skipped)
        assert len(ssh2.commands_run) == 0

    def test_parse_slug_from_store_finds_slug(self):
        from netalertx.installer import _parse_slug_from_store

        output = (
            "- name: NetAlertX\n"
            "  slug: netalertx_fa\n"
            "  repository: https://github.com/alexbelgium/hassio-addons\n"
        )
        slug = _parse_slug_from_store(
            output, "https://github.com/alexbelgium/hassio-addons"
        )
        assert slug == "netalertx_fa"

    def test_parse_slug_from_store_returns_empty_when_not_found(self):
        from netalertx.installer import _parse_slug_from_store

        slug = _parse_slug_from_store(
            "no relevant content here", "https://github.com/alexbelgium/hassio-addons"
        )
        assert slug == ""

    def test_parse_slug_from_store_ignores_other_addons_in_same_repo(self):
        # alexbelgium/hassio-addons hosts many add-ons; Gazpar2MQTT appears
        # before NetAlertX alphabetically — the resolver must not return it.
        from netalertx.installer import _parse_slug_from_store

        output = (
            "- name: Gazpar2MQTT\n"
            "  slug: db21ed7f_gazpar2mqtt\n"
            "  repository: https://github.com/alexbelgium/hassio-addons\n"
            "- name: NetAlertX\n"
            "  slug: db21ed7f_netalertx\n"
            "  repository: https://github.com/alexbelgium/hassio-addons\n"
        )
        slug = _parse_slug_from_store(
            output, "https://github.com/alexbelgium/hassio-addons"
        )
        assert slug == "db21ed7f_netalertx"

    def test_step2_mosquitto_not_running_starts_without_installing(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        call_counts: dict[str, int] = {}

        class TrackingSSHClient(FakeSSHClient):
            async def run(self, command, check=False):  # type: ignore[override]
                call_counts[command] = call_counts.get(command, 0) + 1
                if "ha apps info core_mosquitto" in command:
                    if call_counts[command] == 1:
                        return 0, "state: stopped", ""
                    return 0, "state: running", ""
                return await super().run(command, check=check)

        ssh = TrackingSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps start core_mosquitto": (0, "", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (0, "slug: netalertx_fa", ""),
            }
        )
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )
        assert "ha apps start core_mosquitto" in ssh.commands_run
        assert "ha apps install core_mosquitto" not in ssh.commands_run

    def test_step2_mosquitto_start_poll_fails_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from netalertx.installer import run_steps_1_to_4
        from netalertx.installer_diagnostics import InstallerDiagnostic

        db = _make_installer_db(tmp_path, monkeypatch)

        async def poll_false(*a, **k):
            return False

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_false)

        diag = InstallerDiagnostic(
            primary_hypothesis="Port 1883 is in use",
            confidence=0.8,
            supporting_evidence=["state: stopped"],
            alternative_hypotheses=[],
            recommended_action="Stop the conflicting process",
            can_auto_fix=False,
        )
        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: stopped", ""),
                "ha apps start core_mosquitto": (0, "", ""),
            }
        )
        gate = self._gate_ask()
        state = asyncio.run(
            run_steps_1_to_4(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                llm_client=FakeLLMClient(diag.model_dump_json()),
            )
        )
        assert state == "MQTT_INSTALLED"
        assert any(
            "failed to start" in c["subject"].lower()
            for c in gate.require_approval_calls
        )

    def test_step3_interface_already_in_details_is_idempotent(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        from netalertx.installer import _write_install_state

        _write_install_state(
            str(db),
            "MQTT_RUNNING",
            {"scan_interface": "eth1"},
            "test-cid",
        )

        ssh = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )
        _, details = _read_install_state(str(db))
        assert details["scan_interface"] == "eth1"
        assert not any("interface" in c.lower() for c in ssh.commands_run)

    def test_step4_repo_freshly_added_advances_state(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        from netalertx.installer import _write_install_state

        _write_install_state(str(db), "MQTT_RUNNING", {}, "test-cid")

        _repo_url = "https://github.com/alexbelgium/hassio-addons"
        list_call_count = [0]

        class TrackingSSHClient(FakeSSHClient):
            async def run(self, command, check=False):  # type: ignore[override]
                if "ha store repositories list" in command:
                    list_call_count[0] += 1
                    if list_call_count[0] == 1:
                        return 0, "", ""
                    return 0, _repo_url, ""
                return await super().run(command, check=check)

        ssh = TrackingSSHClient(
            command_results={
                f"ha store repositories add {_repo_url}": (0, "", ""),
                "ha store addons": (0, "slug: netalertx_fa", ""),
            }
        )
        gate = self._gate_auto()
        state = asyncio.run(run_steps_1_to_4(ssh, gate, self._notifier(), db_path=db))
        assert state == "ADDON_REPO_ADDED"
        assert list_call_count[0] == 2


# ── installer DB helper (shared by Steps1to4 and Steps5to8 tests) ─────────────


def _make_installer_db_at_state(tmp_path, monkeypatch, state: str, details=None):
    """Create and migrate a test DB pre-seeded at *state*, patch DB_PATH."""
    import ha_agent_advanced
    import netalertx.installer as inst

    db = tmp_path / "installer_test.db"
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
    ha_agent_advanced.init_local_database()
    monkeypatch.setattr(inst, "DB_PATH", str(db))

    from netalertx.installer import _write_install_state

    _write_install_state(str(db), state, details or {}, "test-cid")
    return str(db)


# ── Steps 5–8 helper function unit tests ─────────────────────────────────────


class TestInstallerHelpers5to8:
    def test_merge_app_conf_updates_existing_key(self):
        from netalertx.installer import _merge_app_conf

        original = "MQTT_BROKER = 'old'\nMQTT_PORT = 1234\n"
        merged, diff = _merge_app_conf(original, {"MQTT_BROKER": "'new'"})
        assert "MQTT_BROKER = 'new'" in merged
        assert "MQTT_PORT = 1234" in merged
        assert "MQTT_BROKER" in diff

    def test_merge_app_conf_appends_missing_key(self):
        from netalertx.installer import _merge_app_conf

        original = "MQTT_PORT = 1883\n"
        merged, diff = _merge_app_conf(original, {"HA_URL": "'http://ha.local:8123'"})
        assert "HA_URL = 'http://ha.local:8123'" in merged
        assert "HA_URL" in diff

    def test_merge_app_conf_preserves_unchanged_key(self):
        from netalertx.installer import _merge_app_conf

        original = "MQTT_BROKER = 'same'\n"
        merged, diff = _merge_app_conf(original, {"MQTT_BROKER": "'same'"})
        assert "MQTT_BROKER = 'same'" in merged
        assert "MQTT_BROKER" not in diff  # no change → not in diff

    def test_merge_plugins_adds_missing_plugins(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("['ARPSCAN']", ["MQTT", "ARPSCAN"])
        import ast

        lst = ast.literal_eval(result)
        assert "MQTT" in lst
        assert "ARPSCAN" in lst

    def test_merge_plugins_preserves_existing_plugins(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("['ARPSCAN', 'NMAP', 'MQTT']", ["MQTT", "ARPSCAN"])
        import ast

        lst = ast.literal_eval(result)
        assert "NMAP" in lst
        assert lst.count("MQTT") == 1  # no duplicates

    def test_merge_plugins_handles_empty_original(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("", ["MQTT", "ARPSCAN"])
        import ast

        lst = ast.literal_eval(result)
        assert set(lst) == {"MQTT", "ARPSCAN"}

    def test_parse_data_path_returns_path(self):
        from netalertx.installer import _parse_data_path

        info = "name: NetAlertX\ndata: /data/netalertx\nstate: running\n"
        assert _parse_data_path(info) == "/data/netalertx"

    def test_parse_data_path_returns_empty_when_absent_no_slug(self):
        from netalertx.installer import _parse_data_path

        assert _parse_data_path("name: NetAlertX\nstate: running\n") == ""

    def test_parse_data_path_falls_back_to_addon_configs_when_absent(self):
        # HA Supervisor omits the data: field for some add-ons; fall back to
        # /addon_configs/{slug} which is the conventional host path.
        from netalertx.installer import _parse_data_path

        info = "name: NetAlertX\nstate: running\n"
        assert (
            _parse_data_path(info, "db21ed7f_netalertx_fa")
            == "/addon_configs/db21ed7f_netalertx_fa"
        )

    def test_parse_data_path_prefers_data_field_over_fallback(self):
        from netalertx.installer import _parse_data_path

        info = "name: NetAlertX\ndata: /custom/path\nstate: running\n"
        assert _parse_data_path(info, "db21ed7f_netalertx_fa") == "/custom/path"

    def test_check_automation_exists_true(self):
        from netalertx.installer import _check_automation_exists

        content = "- id: netalertx_event_handler\n  trigger:\n    - platform: webhook\n"
        assert _check_automation_exists(content) is True

    def test_check_automation_exists_false(self):
        from netalertx.installer import _check_automation_exists

        content = "- id: other_automation\n  trigger:\n    - platform: state\n"
        assert _check_automation_exists(content) is False

    def test_merge_plugins_non_list_literal_resets(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("'hello'", ["MQTT"])
        import ast

        lst = ast.literal_eval(result)
        assert lst == ["MQTT"]

    def test_merge_plugins_invalid_literal_resets(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("{broken syntax!!", ["MQTT"])
        import ast

        lst = ast.literal_eval(result)
        assert lst == ["MQTT"]

    def test_poll_addon_state_timeout_returns_false(self):
        import asyncio
        from netalertx.installer import _poll_addon_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha apps info slug_x": (0, "state: stopped", "")}
        )
        result = asyncio.run(
            _poll_addon_state(ssh, "slug_x", "running", attempts=2, delay=0.0)
        )
        assert result is False

    def test_poll_addon_state_logs_each_attempt(self, caplog):
        import asyncio
        import logging
        from netalertx.installer import _poll_addon_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha apps info slug_p": (0, "state: stopped", "")}
        )
        with caplog.at_level(logging.INFO):
            result = asyncio.run(
                _poll_addon_state(ssh, "slug_p", "running", attempts=3, delay=0.0)
            )
        assert result is False
        poll_records = [r for r in caplog.records if "poll_waiting" in r.message]
        assert len(poll_records) == 3

    def test_poll_addon_not_state_timeout_returns_false(self):
        import asyncio
        from netalertx.installer import _poll_addon_not_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha apps info slug_y": (0, "state: unknown", "")}
        )
        result = asyncio.run(
            _poll_addon_not_state(ssh, "slug_y", "unknown", attempts=2, delay=0.0)
        )
        assert result is False

    def test_poll_addon_not_state_returns_true_when_state_changes(self):
        import asyncio
        from netalertx.installer import _poll_addon_not_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha apps info slug_z": (0, "state: running", "")}
        )
        result = asyncio.run(
            _poll_addon_not_state(ssh, "slug_z", "unknown", attempts=2, delay=0.0)
        )
        assert result is True

    def test_detect_subnet_returns_empty_when_no_inet(self):
        import asyncio
        from netalertx.installer import _detect_subnet
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ip addr show eth0": (0, "link/ether aa:bb:cc:dd:ee:ff\n", "")
            }
        )
        result = asyncio.run(_detect_subnet(ssh, "eth0"))
        assert result == ""

    def test_detect_subnet_returns_empty_on_invalid_ip(self):
        import asyncio
        from netalertx.installer import _detect_subnet
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ip addr show eth0": (0, "inet 999.999.999.999/99\n", "")}
        )
        result = asyncio.run(_detect_subnet(ssh, "eth0"))
        assert result == ""


# ── TestNetAlertXInstallerSteps5to8 ──────────────────────────────────────────

_SLUG = "netalertx_fa"
_DATA_PATH = "/data/netalertx"
_CONF_PATH = f"{_DATA_PATH}/config/app.conf"
_ORIG_APP_CONF = "MQTT_BROKER = 'localhost'\nLOADED_PLUGINS = ['ARPSCAN']\n"
_HA_CONF = "homeassistant:\n  time_zone: America/New_York\n"
_AUTOMATIONS_PATH = "/config/automations.yaml"


def _make_mock_http(routes):
    """Build an httpx.AsyncClient backed by an in-memory transport.

    routes: list of (method, url_fragment, status_code, response_body)
    """
    import json

    import httpx

    class _T(httpx.AsyncBaseTransport):
        def __init__(self, rs):
            self._rs = rs

        async def handle_async_request(self, request):
            for method, frag, status, body in self._rs:
                if request.method == method and frag in str(request.url):
                    if isinstance(body, str):
                        content, ct = body.encode(), "text/plain"
                    else:
                        content, ct = json.dumps(body).encode(), "application/json"
                    return httpx.Response(
                        status, content=content, headers={"Content-Type": ct}
                    )
            return httpx.Response(
                404,
                content=b"not found",
                headers={"Content-Type": "text/plain"},
            )

    return httpx.AsyncClient(transport=_T(routes))


class TestNetAlertXInstallerSteps5to8:
    # ── helpers ──────────────────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def _zero_restart_wait(self, monkeypatch):
        monkeypatch.setattr("netalertx.installer._HA_RESTART_WAIT_S", 0)

    def _notifier(self, approve: bool = True):
        from utils.notify import FakeNotifier

        return FakeNotifier(approve=approve)

    def _gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def _gate_ask(self, approval: bool = True):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=False, approval_result=approval)

    _MQTT_GREP_CMD = (
        'grep -c \'"domain":"mqtt"\' '
        "/config/.storage/core.config_entries 2>/dev/null || true"
    )

    def _make_full_ssh(
        self, app_conf=_ORIG_APP_CONF, automations="", mqtt_present=True
    ):
        """SSH client configured for a typical steps-5-8 run (all pass)."""
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={
                _CONF_PATH: app_conf,
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: automations,
            },
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                f"ha apps install {_SLUG}": (0, "", ""),
                f"ha apps start {_SLUG}": (0, "", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: test-backup-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                f"ip addr show": (
                    0,
                    "inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\n",
                    "",
                ),
                self._MQTT_GREP_CMD: (0, "1" if mqtt_present else "0", ""),
            },
        )

    def _make_full_ssh_no_mqtt(self, **kwargs):
        """SSH client where the MQTT config entry is absent."""
        return self._make_full_ssh(mqtt_present=False, **kwargs)

    def _http_with_mqtt(self):
        return _make_mock_http(
            [
                ("GET", "/api/config/config_entries/entry", 200, [{"domain": "mqtt"}]),
                ("GET", "/health", 200, {"status": "ok"}),
            ]
        )

    def _http_no_mqtt(self):
        return _make_mock_http(
            [
                ("GET", "/api/config/config_entries/entry", 200, [{"domain": "other"}]),
                ("GET", "/health", 200, {"status": "ok"}),
            ]
        )

    # ── step 5: install addon ─────────────────────────────────────────────────

    def test_step5_fresh_install_happy_path(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import _read_install_state, run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_true)
        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: "",
            },
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: unknown\ndata: {_DATA_PATH}\n",
                    "",
                ),
                f"ha apps install {_SLUG}": (0, "", ""),
                f"ha apps start {_SLUG}": (0, "", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: fresh-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "ip addr show": (
                    0,
                    "inet 192.168.1.5/24 scope global eth0\n",
                    "",
                ),
            },
        )

        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert any("ha apps install" in c for c in ssh.commands_run)
        assert any("ha apps start" in c for c in ssh.commands_run)

    def test_step5_already_running_skips_install_and_start(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import _read_install_state, run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        # No install or start commands issued since addon reported running
        assert not any("ha apps install" in c for c in ssh.commands_run)
        assert not any("ha apps start" in c for c in ssh.commands_run)

    def test_step5_install_timeout_triggers_critical_gate(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from netalertx.installer_diagnostics import InstallerDiagnostic
        from utils.ollama_client import FakeLLMClient

        async def poll_false(*a, **k):
            return False

        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_false)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        diag = InstallerDiagnostic(
            primary_hypothesis="Network issue downloading image",
            confidence=0.5,
            supporting_evidence=[],
            alternative_hypotheses=["Supervisor not yet indexed the repo"],
            recommended_action="Wait and re-run setup",
            can_auto_fix=False,
        )
        ssh = FakeSSHClient(
            command_results={
                f"ha apps info {_SLUG}": (0, "state: unknown\n", ""),
                f"ha apps install {_SLUG}": (0, "", ""),
            }
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
                llm_client=FakeLLMClient(diag.model_dump_json()),
            )
        )
        assert state == "ADDON_REPO_ADDED"
        assert any(
            c.get("risk").name == "CRITICAL" for c in gate.require_approval_calls
        )

    def test_step5_start_timeout_triggers_critical_gate(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from netalertx.installer_diagnostics import InstallerDiagnostic
        from utils.ollama_client import FakeLLMClient

        async def poll_not_state_true(*a, **k):
            return True

        async def poll_state_false(*a, **k):
            return False

        monkeypatch.setattr(
            "netalertx.installer._poll_addon_not_state", poll_not_state_true
        )
        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_state_false)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        diag = InstallerDiagnostic(
            primary_hypothesis="Add-on config issue causing immediate exit",
            confidence=0.6,
            supporting_evidence=[],
            alternative_hypotheses=["NET_RAW capability missing"],
            recommended_action="Check add-on logs for specific error",
            can_auto_fix=False,
        )
        ssh = FakeSSHClient(
            command_results={
                f"ha apps info {_SLUG}": (0, "state: unknown\n", ""),
                f"ha apps install {_SLUG}": (0, "", ""),
                f"ha apps start {_SLUG}": (0, "", ""),
            }
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
                llm_client=FakeLLMClient(diag.model_dump_json()),
            )
        )
        assert state == "ADDON_INSTALLED"
        assert any(
            c.get("risk").name == "CRITICAL" for c in gate.require_approval_calls
        )

    def test_step5_no_slug_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        db = _make_installer_db_at_state(
            tmp_path, monkeypatch, "ADDON_REPO_ADDED", {}  # no addon_slug in details
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_REPO_ADDED"
        assert any(
            c.get("risk").name == "CRITICAL" for c in gate.require_approval_calls
        )

    def test_step5_idempotent_from_addon_running(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        # Step 5 is entirely skipped — no install or start commands
        assert not any("ha apps install" in c for c in ssh.commands_run)
        assert not any("ha apps start" in c for c in ssh.commands_run)

    # ── step 6: configure app.conf ────────────────────────────────────────────

    def test_step6_writes_app_conf_and_merges_plugins(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )

        written = ssh.written_files.get(_CONF_PATH, "")
        assert "MQTT" in written
        assert "ARPSCAN" in written
        assert "MQTT_BROKER" in written

    def test_step6_backup_failure_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
            },
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (1, "", "backup failed"),  # backup fails
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_RUNNING"
        assert _CONF_PATH not in ssh.written_files

    def test_step6_idempotent_from_configured(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        # Step 6 was skipped — no app.conf write
        assert _CONF_PATH not in ssh.written_files

    # ── step 7: verify MQTT integration ──────────────────────────────────────

    def test_step7_mqtt_found_advances(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import (
            _read_install_state,
            run_steps_5_to_8,
        )  # noqa: F401

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_step7_mqtt_not_found_approval_advances(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh_no_mqtt()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        # Gate asks and approves — MQTT not found by SSH or HTTP, but user confirmed
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=self._http_no_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert gate.require_approval_calls  # HITL notification was sent

    def test_step7_mqtt_not_found_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        ssh = self._make_full_ssh_no_mqtt()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_no_mqtt(),
            )
        )
        assert state == "NETALERTX_CONFIGURED"

    def test_step7_idempotent_from_mqtt_verified(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)  # no gate calls expected
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert not gate.require_approval_calls  # step 7 was skipped

    # ── step 8: create webhook automation ────────────────────────────────────

    def test_step8_creates_automation_and_reaches_fully_operational(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh(automations="")  # empty automations file
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        written = ssh.written_files.get(_AUTOMATIONS_PATH, "")
        assert "netalertx_event_handler" in written
        assert "platform: webhook" in written
        assert "eveMac" in written  # camelCase field

    def test_step8_automation_exists_skips_write(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        existing = (
            "- id: netalertx_event_handler\n"
            "  trigger:\n    - platform: webhook\n      webhook_id: netalertx_event\n"
        )
        ssh = self._make_full_ssh(automations=existing)
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert _AUTOMATIONS_PATH not in ssh.written_files  # no write — already present

    def test_step8_ha_check_fails_restores_original(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        original_automations = "- id: existing_automation\n  trigger: []\n"

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _AUTOMATIONS_PATH: original_automations,
                "/config/configuration.yaml": _HA_CONF,
                _CONF_PATH: _ORIG_APP_CONF,
            },
            command_results={
                "ha backup new": (0, "Slug: step8-slug\n", ""),
                "ha core check": (1, "", "automation error"),  # check fails
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "HA_MQTT_INTEGRATION_VERIFIED"
        # Original was restored
        assert ssh.written_files.get(_AUTOMATIONS_PATH) == original_automations
        assert any(c.get("risk").name == "HIGH" for c in gate.require_approval_calls)

    def test_step8_idempotent_from_fully_operational(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "FULLY_OPERATIONAL",
            {"addon_slug": _SLUG},
        )

        ssh2 = self._make_full_ssh()
        asyncio.run(
            run_steps_5_to_8(
                ssh2,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert len(ssh2.commands_run) == 0  # complete no-op

    # ── full run tests ────────────────────────────────────────────────────────

    def test_full_run_from_addon_repo_added_reaches_fully_operational(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: "",
            },
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: unknown\ndata: {_DATA_PATH}\n",
                    "",
                ),
                f"ha apps install {_SLUG}": (0, "", ""),
                f"ha apps start {_SLUG}": (0, "", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: full-run-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.2/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_full_run_second_call_is_noop(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "FULLY_OPERATIONAL",
            {"addon_slug": _SLUG},
        )

        ssh2 = self._make_full_ssh()
        state = asyncio.run(
            run_steps_5_to_8(
                ssh2,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert len(ssh2.commands_run) == 0

    # ── step 6: error paths ───────────────────────────────────────────────────

    def test_step6_falls_back_to_addon_configs_when_data_field_absent(
        self, tmp_path, monkeypatch
    ):
        # When ha apps info omits the data: field, step 6 falls back to
        # /addon_configs/{slug}/config/app.conf instead of aborting.
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        fallback_conf = f"/addon_configs/{_SLUG}/config/app.conf"
        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": _HA_CONF,
                fallback_conf: _ORIG_APP_CONF,
            },
            command_results={
                f"ha apps info {_SLUG}": (0, "state: running\n", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: test-backup-slug\n", ""),
                "ha core check": (0, "", ""),
                f"ip addr show eth0": (
                    0,
                    "inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\n",
                    "",
                ),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert "MQTT" in ssh.written_files.get(fallback_conf, "")
        assert state == "FULLY_OPERATIONAL"

    def test_step6_app_conf_missing_uses_empty_original(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: "",
            },
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (0, "Slug: bk-slug\n", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.2/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert _CONF_PATH in ssh.written_files

    def test_step6_no_scan_interface_uses_empty_subnets(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        written = ssh.written_files.get(_CONF_PATH, "")
        assert "SCAN_SUBNETS = []" in written

    def test_step6_ha_conf_read_fails_uses_utc_timezone(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={_CONF_PATH: _ORIG_APP_CONF, _AUTOMATIONS_PATH: ""},
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (0, "Slug: bk-tz\n", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        written = ssh.written_files.get(_CONF_PATH, "")
        assert "TIMEZONE = 'UTC'" in written

    def test_step6_restart_poll_fails_triggers_gate(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        call_counts: dict[str, int] = {}

        async def poll_once_false(ssh_client, addon_id, expected, **kwargs):
            key = f"{addon_id}:{expected}"
            call_counts[key] = call_counts.get(key, 0) + 1
            if expected == "running" and call_counts[key] == 1:
                return False
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_once_false)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
            },
            command_results={
                f"ha apps info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (0, "Slug: bk-restart\n", ""),
                f"ha apps restart {_SLUG}": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_RUNNING"
        assert any(
            "restart" in c["subject"].lower() for c in gate.require_approval_calls
        )

    def test_step6_api_health_check_non_200_is_non_fatal(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        class _Non200HealthTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/health" in str(request.url):
                    return httpx.Response(
                        503, content=b"", headers={"Content-Type": "text/plain"}
                    )
                return httpx.Response(
                    200,
                    content=b'[{"domain":"mqtt"}]',
                    headers={"Content-Type": "application/json"},
                )

        http = httpx.AsyncClient(transport=_Non200HealthTransport())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_step6_api_health_check_exception_is_non_fatal(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        class _ErrorTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/health" in str(request.url):
                    raise httpx.ConnectError("refused")
                return httpx.Response(
                    200,
                    content=b'[{"domain":"mqtt"}]',
                    headers={"Content-Type": "application/json"},
                )

        http = httpx.AsyncClient(transport=_ErrorTransport())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"

    # ── step 7: _mqtt_configured edge paths ──────────────────────────────────

    def test_step7_mqtt_check_non_200_triggers_hitl(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        class _Non200Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(
                    503, content=b"", headers={"Content-Type": "text/plain"}
                )

        http = httpx.AsyncClient(transport=_Non200Transport())
        # Set a fake token so the HTTP path is actually exercised (not skipped).
        monkeypatch.setattr("netalertx.installer.HA_API_TOKEN", "test-token")
        ssh = self._make_full_ssh_no_mqtt()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert gate.require_approval_calls

    def test_step7_mqtt_check_exception_triggers_hitl(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        class _ExceptionTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("refused")

        http = httpx.AsyncClient(transport=_ExceptionTransport())
        # Set a fake token so the HTTP path is actually exercised (not skipped).
        monkeypatch.setattr("netalertx.installer.HA_API_TOKEN", "test-token")
        ssh = self._make_full_ssh_no_mqtt()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert gate.require_approval_calls

    def test_step7_approved_and_recheck_returns_true(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        call_count = [0]

        class _MqttAppearsAfterApproval(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/api/config/config_entries/entry" in str(request.url):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        body = b'[{"domain":"other"}]'
                    else:
                        body = b'[{"domain":"mqtt"}]'
                    return httpx.Response(
                        200, content=body, headers={"Content-Type": "application/json"}
                    )
                return httpx.Response(
                    200, content=b"{}", headers={"Content-Type": "application/json"}
                )

        http = httpx.AsyncClient(transport=_MqttAppearsAfterApproval())
        # Force the HTTP path so both initial check and recheck use the mock.
        monkeypatch.setattr("netalertx.installer.HA_API_TOKEN", "test-token")
        ssh = self._make_full_ssh_no_mqtt()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert call_count[0] == 2

    # ── step 8: error paths ───────────────────────────────────────────────────

    def test_step8_backup_fails_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = FakeSSHClient(
            file_contents={_AUTOMATIONS_PATH: ""},
            command_results={
                "ha backup new": (1, "", "backup error"),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG},
        )
        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "HA_MQTT_INTEGRATION_VERIFIED"
        assert any(
            "backup" in c["subject"].lower() for c in gate.require_approval_calls
        )

    def test_step8_fallback_to_directory_automation_path(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        _fallback_path = "/config/automations/netalertx_webhook.yaml"
        ssh = FakeSSHClient(
            file_contents={_fallback_path: ""},
            command_results={
                "ha backup new": (0, "Slug: bk-fallback\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert _fallback_path in ssh.written_files

    def test_step8_both_automation_paths_missing_creates_new_file(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = FakeSSHClient(
            file_contents={},
            command_results={
                "ha backup new": (0, "Slug: bk-new\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert "/config/automations/netalertx_webhook.yaml" in ssh.written_files


# ── run_installer ─────────────────────────────────────────────────────────────


class TestRunInstaller:
    def _gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def _notifier(self):
        from utils.notify import FakeNotifier

        return FakeNotifier(approve=True)

    def test_run_installer_chains_steps_1_to_4_then_5_to_8(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_installer
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        _slug = "netalertx_fa"
        _data = "/data/netalertx"
        _conf = f"{_data}/app.conf"

        import ha_agent_advanced
        import netalertx.installer as inst

        db = tmp_path / "installer_full.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        monkeypatch.setattr(inst, "DB_PATH", str(db))

        import httpx

        class _FullTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                body = b'[{"domain":"mqtt"}]'
                return httpx.Response(
                    200, content=body, headers={"Content-Type": "application/json"}
                )

        http = httpx.AsyncClient(transport=_FullTransport())

        ssh = FakeSSHClient(
            file_contents={
                _conf: "MQTT_BROKER = 'localhost'\n",
                "/config/configuration.yaml": "homeassistant:\n  time_zone: UTC\n",
                "/config/automations.yaml": "",
            },
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                "ha store repositories list": (
                    0,
                    "https://github.com/alexbelgium/hassio-addons",
                    "",
                ),
                "ha store addons": (0, f"slug: {_slug}", ""),
                f"ha apps info {_slug}": (0, f"state: running\ndata: {_data}\n", ""),
                f"ha apps install {_slug}": (0, "", ""),
                f"ha apps start {_slug}": (0, "", ""),
                f"ha apps restart {_slug}": (0, "", ""),
                "ha backup new": (0, "Slug: full-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        state = asyncio.run(
            run_installer(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=str(db),
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_run_installer_aborts_when_steps_1_to_4_fail(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_installer
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate

        import ha_agent_advanced
        import netalertx.installer as inst

        db = tmp_path / "installer_abort.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        monkeypatch.setattr(inst, "DB_PATH", str(db))

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "not found"),
                "docker info": (1, "", "not found"),
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        state = asyncio.run(
            run_installer(
                ssh,
                gate,
                self._notifier(),
                db_path=str(db),
            )
        )
        assert state == "NOT_INSTALLED"


# ── netalertx/ha_name_sync.py ────────────────────────────────────────────────


class _FakeNAXClient:
    """Minimal NetAlertXAPIClient double that records write calls."""

    def __init__(self, devices: list[dict]) -> None:
        self._devices = devices
        self.updates: list[tuple[str, str, str]] = []  # (mac, col, val)
        self.locks: list[tuple[str, str, bool]] = []  # (mac, field, lock)

    async def get_devices(self) -> list[dict]:
        return self._devices

    async def update_device_column(
        self, mac: str, column_name: str, column_value: str
    ) -> None:
        self.updates.append((mac, column_name, column_value))

    async def lock_device_field(
        self, mac: str, field_name: str, lock: bool = True
    ) -> None:
        self.locks.append((mac, field_name, lock))


def _ha_states_transport(states: list[dict]):
    """Return an httpx.AsyncClient whose /api/states response is ``states``."""
    import json as _json

    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=_json.dumps(states).encode())

    return httpx.AsyncClient(transport=_T())


def _make_syncer(
    ssh,
    nax_client,
    ha_http_client,
    patterns=None,
    gate=None,
    notifier=None,
):
    from netalertx.ha_name_sync import HaNameSync
    from utils.autonomy import FakeAutonomyGate
    from utils.notify import FakeNotifier

    return HaNameSync(
        ssh_client=ssh,
        api_client=nax_client,
        gate=gate or FakeAutonomyGate(auto_execute_result=True),
        notifier=notifier or FakeNotifier(approve=True),
        ha_host="ha.local",
        ha_api_token="test-tok",
        auto_patterns=patterns or ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"],
        http_client=ha_http_client,
    )


class TestHaNameSyncReadSources:
    def test_source3_only_returns_device_tracker_names(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        states = [
            {
                "entity_id": "device_tracker.phone",
                "attributes": {
                    "mac_address": "aa:bb:cc:dd:ee:ff",
                    "friendly_name": "Andy's Phone",
                },
            }
        ]
        ssh = FakeSSHClient()  # no files → Source 1 and 2 raise/skip
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names["AA:BB:CC:DD:EE:FF"] == "Andy's Phone"

    def test_source2_fills_gaps_not_covered_by_source3(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        # Source 3 has phone; Source 2 has a tablet not in Source 3
        states = [
            {
                "entity_id": "device_tracker.phone",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "Phone",
                },
            }
        ]
        known_devices_yaml = "tablet:\n" "  mac: AA:BB:CC:DD:EE:02\n" "  name: Tablet\n"
        ssh = FakeSSHClient(
            file_contents={"/config/known_devices.yaml": known_devices_yaml}
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names["AA:BB:CC:DD:EE:01"] == "Phone"
        assert names["AA:BB:CC:DD:EE:02"] == "Tablet"

    def test_source1_wins_over_source3_for_same_mac(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:FF"
        states = [
            {
                "entity_id": "device_tracker.x",
                "attributes": {"mac_address": mac, "friendly_name": "Old Name"},
            }
        ]
        registry_json = {
            "data": {
                "devices": [
                    {
                        "name": "Authoritative Name",
                        "name_by_user": None,
                        "connections": [["mac", mac]],
                    }
                ]
            }
        }
        import json as _json

        ssh = FakeSSHClient(
            file_contents={
                "/config/.storage/core.device_registry": _json.dumps(registry_json)
            }
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names[mac] == "Authoritative Name"

    def test_source1_prefers_name_by_user_over_name(self):
        import asyncio
        import json as _json

        from utils.ssh_client import FakeSSHClient

        mac = "11:22:33:44:55:66"
        registry_json = {
            "data": {
                "devices": [
                    {
                        "name": "Auto Name",
                        "name_by_user": "Custom Name",
                        "connections": [["mac", mac]],
                    }
                ]
            }
        }
        ssh = FakeSSHClient(
            file_contents={
                "/config/.storage/core.device_registry": _json.dumps(registry_json)
            }
        )
        ha_http = _ha_states_transport([])
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names[mac] == "Custom Name"

    def test_source2_filenotfounderror_silently_skipped(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        states = [
            {
                "entity_id": "device_tracker.x",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "X",
                },
            }
        ]
        # No known_devices.yaml → FakeSSHClient raises FileNotFoundError
        ssh = FakeSSHClient()
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert "AA:BB:CC:DD:EE:01" in names  # Source 3 still works

    def test_mac_normalization_various_formats(self):
        import asyncio
        import json as _json

        from utils.ssh_client import FakeSSHClient

        registry_json = {
            "data": {
                "devices": [
                    {
                        "name": "Dash Device",
                        "name_by_user": None,
                        "connections": [["mac", "aa-bb-cc-dd-ee-ff"]],
                    },
                ]
            }
        }
        states = [
            {
                "entity_id": "device_tracker.nocolon",
                "attributes": {
                    "mac_address": "aabbccddeeff",
                    "friendly_name": "No Colon",
                },
            }
        ]
        ssh = FakeSSHClient(
            file_contents={
                "/config/.storage/core.device_registry": _json.dumps(registry_json)
            }
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        # dash-delimited MAC in Source 1 and no-delimiter MAC in Source 3
        # both normalize to the same key → Source 1 wins
        assert names.get("AA:BB:CC:DD:EE:FF") == "Dash Device"

    def test_source1_failure_falls_back_to_lower_sources(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        states = [
            {
                "entity_id": "device_tracker.phone",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "Phone",
                },
            }
        ]
        # Bad JSON → Source 1 logs warning and skips
        ssh = FakeSSHClient(
            file_contents={"/config/.storage/core.device_registry": "not json {{{{"}
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names["AA:BB:CC:DD:EE:01"] == "Phone"


class TestHaNameSyncCases1And2:
    _PATTERNS = ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"]

    def _simple_states(self, mac: str, name: str) -> list[dict]:
        return [
            {
                "entity_id": "device_tracker.x",
                "attributes": {"mac_address": mac, "friendly_name": name},
            }
        ]

    def test_case1_blank_devname_writes_and_locks(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:01"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""}]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Living Room TV"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert ("AA:BB:CC:DD:EE:01", "devName", "Living Room TV") in nax.updates
        assert ("AA:BB:CC:DD:EE:01", "devName", True) in nax.locks
        assert report.locked == []
        assert report.conflicted == []
        assert report.unnamed == []

    def test_case1_mac_as_devname_triggers_write(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:02"
        # devName is the MAC address itself → matches auto-generated pattern
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": mac,
                    "devVendor": "Acme",
                    "devLastIP": "10.0.0.2",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Office Printer"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert len(nax.updates) == 1
        assert nax.updates[0][2] == "Office Printer"

    def test_case1_unknown_prefix_triggers_write(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:03"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "unknown-abc123",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Thermostat"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written

    def test_case2_matching_name_locks_only(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:04"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Kitchen Hub",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Kitchen Hub"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.locked
        assert report.written == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 1

    def test_case2_case_insensitive_match_locks_only(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:05"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "kitchen hub",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Kitchen Hub"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.locked
        assert report.written == []

    def test_case3_conflict_collected_no_write_on_rejection(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:06"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Bob's Laptop",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Alice's Laptop"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=False),
        )
        report = asyncio.run(syncer.sync_names())

        assert len(report.conflicted) == 1
        assert report.conflicted[0].mac == mac
        assert report.conflicted[0].ha_name == "Alice's Laptop"
        assert report.conflicted[0].netalertx_name == "Bob's Laptop"
        assert report.written == []
        assert report.locked == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 0

    def test_case4_no_ha_name_and_auto_devname_collected_as_unnamed(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:07"
        # devName is a MAC address (auto-generated) → Step A does not apply;
        # FakeSSHClient returns empty stdout → Step B (DNS) fails → Step C (unnamed)
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": mac,  # MAC-as-name matches auto-generated pattern
                    "devVendor": "Synology",
                    "devLastIP": "10.0.0.100",
                }
            ]
        )
        ha_http = _ha_states_transport([])
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        report = asyncio.run(syncer.sync_names())

        assert len(report.unnamed) == 1
        assert report.unnamed[0].mac == mac
        assert report.unnamed[0].vendor == "Synology"
        assert report.unnamed[0].last_ip == "10.0.0.100"
        assert report.written == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 0

    def test_zero_ha_names_triggers_low_hitl(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        nax = _FakeNAXClient([])
        ha_http = _ha_states_transport([])  # empty → zero MAC entries
        gate = FakeAutonomyGate(auto_execute_result=True)
        notifier = FakeNotifier(approve=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, gate=gate, notifier=notifier
        )
        asyncio.run(syncer.sync_names())

        assert len(gate.require_approval_calls) == 1
        from utils.autonomy import RiskLevel

        assert gate.require_approval_calls[0]["risk"] == RiskLevel.LOW

    def test_sync_report_json_round_trip(self):
        from netalertx.ha_name_sync import ConflictEntry, SyncReport, UnnamedEntry

        report = SyncReport(
            written=["AA:BB:CC:DD:EE:01"],
            locked=["AA:BB:CC:DD:EE:02"],
            conflicted=[
                ConflictEntry(mac="AA:BB:CC:DD:EE:03", ha_name="X", netalertx_name="Y")
            ],
            unnamed=[
                UnnamedEntry(mac="AA:BB:CC:DD:EE:04", vendor="Acme", last_ip="10.0.0.4")
            ],
        )
        restored = SyncReport.model_validate_json(report.model_dump_json())
        assert restored.written == ["AA:BB:CC:DD:EE:01"]
        assert restored.conflicted[0].ha_name == "X"
        assert restored.unnamed[0].vendor == "Acme"

    def test_multiple_devices_mixed_cases(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:01",
                "devName": "",
                "devVendor": "",
                "devLastIP": "",
            },
            {
                "devMAC": "AA:BB:CC:DD:EE:02",
                "devName": "Hub",
                "devVendor": "",
                "devLastIP": "",
            },
            {
                "devMAC": "AA:BB:CC:DD:EE:03",
                "devName": "Old",
                "devVendor": "",
                "devLastIP": "",
            },
            {
                "devMAC": "AA:BB:CC:DD:EE:04",
                "devName": "",  # blank → no HA name + no DNS → Step C (unnamed)
                "devVendor": "X",
                "devLastIP": "",
            },
        ]
        states = [
            {
                "entity_id": "device_tracker.a",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "Phone",
                },
            },
            {
                "entity_id": "device_tracker.b",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:02",
                    "friendly_name": "Hub",
                },
            },
            {
                "entity_id": "device_tracker.c",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:03",
                    "friendly_name": "New",
                },
            },
            # No entry for 04 → Case 4 (blank devName + no DNS → unnamed)
        ]
        nax = _FakeNAXClient(devices)
        ha_http = _ha_states_transport(states)
        # Use rejection gate so Case 3 conflict is not auto-resolved
        from utils.autonomy import FakeAutonomyGate

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        report = asyncio.run(syncer.sync_names())

        assert "AA:BB:CC:DD:EE:01" in report.written  # Case 1: blank
        assert "AA:BB:CC:DD:EE:02" in report.locked  # Case 2: match
        assert any(c.mac == "AA:BB:CC:DD:EE:03" for c in report.conflicted)  # Case 3
        assert any(u.mac == "AA:BB:CC:DD:EE:04" for u in report.unnamed)  # Case 4

    def test_sync_names_skips_devices_with_empty_mac(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:01"
        devices = [
            # Device with no MAC — should be skipped entirely
            {
                "devMAC": "",
                "devName": "",
                "devVendor": "Unknown",
                "devLastIP": "10.0.0.5",
            },
            {"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""},
        ]
        nax = _FakeNAXClient(devices)
        ha_http = _ha_states_transport(self._simple_states(mac, "Phone"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        # No update or lock call should have an empty MAC
        assert all(m != "" for m, _, _ in nax.updates)
        assert all(m != "" for m, _, _ in nax.locks)
        # The valid device is still processed
        assert mac in report.written


class TestHaNameSyncCases3And4:
    """Item 14 — conflict resolution (Case 3) and unknown-device fallbacks (Case 4)."""

    _PATTERNS = ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"]

    def _simple_states(self, mac: str, name: str) -> list[dict]:
        return [
            {
                "entity_id": "device_tracker.x",
                "attributes": {"mac_address": mac, "friendly_name": name},
            }
        ]

    # ── Case 3: name conflict ─────────────────────────────────────────────────

    def test_case3_approval_writes_and_locks_all(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:10"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Bob's Laptop",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Alice's Laptop"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=True),
        )
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert ("AA:BB:CC:DD:EE:10", "devName", "Alice's Laptop") in nax.updates
        assert ("AA:BB:CC:DD:EE:10", "devName", True) in nax.locks
        assert len(report.conflicted) == 1  # still recorded
        assert len(gate.require_approval_calls) == 1
        from utils.autonomy import RiskLevel

        assert gate.require_approval_calls[0]["risk"] == RiskLevel.MEDIUM

    def test_case3_rejection_skips_all(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:11"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Bob's Laptop",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Alice's Laptop"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=False),
        )
        report = asyncio.run(syncer.sync_names())

        assert report.written == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 0
        assert len(report.conflicted) == 1

    def test_case3_multiple_conflicts_single_hitl_call(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        macs = ["AA:BB:CC:DD:EE:12", "AA:BB:CC:DD:EE:13"]
        devices = [
            {"devMAC": m, "devName": "Old Name", "devVendor": "", "devLastIP": ""}
            for m in macs
        ]
        states = [
            {
                "entity_id": f"device_tracker.d{i}",
                "attributes": {"mac_address": m, "friendly_name": "New Name"},
            }
            for i, m in enumerate(macs)
        ]
        nax = _FakeNAXClient(devices)
        ha_http = _ha_states_transport(states)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        asyncio.run(syncer.sync_names())

        # Two conflicts → only one require_approval call (batched)
        assert len(gate.require_approval_calls) == 1

    # ── Case 4 Step A: plausible existing name ────────────────────────────────

    def test_case4_step_a_plausible_name_locked(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:20"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "NAS",  # non-empty, not auto-generated → Step A
                    "devVendor": "Synology",
                    "devLastIP": "10.0.0.50",
                }
            ]
        )
        ha_http = _ha_states_transport([])  # no HA name for this MAC
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.locked
        assert report.written == []
        assert report.unnamed == []
        assert len(nax.updates) == 0
        assert ("AA:BB:CC:DD:EE:20", "devName", True) in nax.locks

    # ── Case 4 Step B: reverse DNS ───────────────────────────────────────────

    def test_case4_step_b_reverse_dns_written(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:21"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "",
                    "devVendor": "HP",
                    "devLastIP": "10.0.0.5",
                }
            ]
        )
        ha_http = _ha_states_transport([])
        ssh = FakeSSHClient(
            command_results={
                "host 10.0.0.5": (
                    0,
                    "5.0.0.10.in-addr.arpa domain name pointer myprinter.local.",
                    "",
                )
            }
        )
        syncer = _make_syncer(ssh, nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert mac in report.reverse_dns
        assert ("AA:BB:CC:DD:EE:21", "devName", "myprinter.local") in nax.updates
        assert report.unnamed == []

    def test_case4_step_b_in_addr_arpa_hostname_skipped(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:22"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": "10.0.0.6"}]
        )
        ha_http = _ha_states_transport([])
        # DNS returns the PTR record itself — unusable
        ssh = FakeSSHClient(
            command_results={
                "host 10.0.0.6": (
                    0,
                    "6.0.0.10.in-addr.arpa domain name pointer 6.0.0.10.in-addr.arpa.",
                    "",
                )
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(ssh, nax, ha_http, patterns=self._PATTERNS, gate=gate)
        report = asyncio.run(syncer.sync_names())

        assert report.written == []
        assert len(report.unnamed) == 1
        assert report.unnamed[0].mac == mac

    def test_case4_step_b_no_dns_response_falls_to_step_c(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:23"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "",
                    "devVendor": "Acme",
                    "devLastIP": "10.0.0.7",
                }
            ]
        )
        ha_http = _ha_states_transport([])
        # FakeSSHClient returns empty stdout for unknown commands
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        report = asyncio.run(syncer.sync_names())

        assert report.written == []
        assert len(report.unnamed) == 1
        assert report.unnamed[0].vendor == "Acme"

    # ── Case 4 Step C: unnamed HITL ──────────────────────────────────────────

    def test_case4_step_c_unnamed_hitl_fires_with_low_risk(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:24"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""}]
        )
        # Provide one HA name so the zero-MAC gate does not fire; the device's
        # MAC is absent so it still goes through Step C (unnamed).
        other_state = [
            {
                "entity_id": "device_tracker.other",
                "attributes": {
                    "mac_address": "11:22:33:44:55:66",
                    "friendly_name": "Other Device",
                },
            }
        ]
        ha_http = _ha_states_transport(other_state)
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        asyncio.run(syncer.sync_names())

        # unnamed HITL fires exactly once with LOW risk
        unnamed_calls = [
            c
            for c in gate.require_approval_calls
            if c["risk"] == RiskLevel.LOW and "Unnamed" in c["subject"]
        ]
        assert len(unnamed_calls) == 1

    # ── sync_device ───────────────────────────────────────────────────────────

    def test_sync_device_case1_writes_ha_name(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:30"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""}]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Front Door Camera"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        asyncio.run(syncer.sync_device(mac))

        assert ("AA:BB:CC:DD:EE:30", "devName", "Front Door Camera") in nax.updates
        assert ("AA:BB:CC:DD:EE:30", "devName", True) in nax.locks

    def test_sync_device_conflict_triggers_medium_hitl(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:31"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "Old Name", "devVendor": "", "devLastIP": ""}]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "New HA Name"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=False),
        )
        asyncio.run(syncer.sync_device(mac))

        conflict_calls = [
            c for c in gate.require_approval_calls if c["risk"] == RiskLevel.MEDIUM
        ]
        assert len(conflict_calls) == 1
        assert len(nax.updates) == 0

    def test_sync_device_not_found_does_not_crash(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        nax = _FakeNAXClient([])  # no devices at all
        ha_http = _ha_states_transport([])
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        asyncio.run(syncer.sync_device("AA:BB:CC:DD:EE:99"))  # should log and return

        assert len(nax.updates) == 0
        assert len(nax.locks) == 0

    def test_sync_device_step_a_locks_plausible_name(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:32"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Printer",
                    "devVendor": "Canon",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport([])  # no HA name
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        asyncio.run(syncer.sync_device(mac))

        assert ("AA:BB:CC:DD:EE:32", "devName", True) in nax.locks
        assert len(nax.updates) == 0


# ── netalertx/log_monitor.py ──────────────────────────────────────────────────


class TestNetAlertXLogMonitor:
    # ── CRITICAL_LOG_PATTERN ──────────────────────────────────────────────────

    def test_pattern_matches_scan_error(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search(
            "ERROR: ArpScan failed — network unreachable"
        )

    def test_pattern_matches_mqtt_error(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search("ERROR MQTT broker connection refused")

    def test_pattern_matches_plugin_exception(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search("Exception in plugin ARPSCAN execution")

    def test_pattern_no_match_on_info_line(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert not CRITICAL_LOG_PATTERN.search("INFO scan completed successfully")

    # ── LogEvaluation schema ──────────────────────────────────────────────────

    def test_log_evaluation_valid_construction(self):
        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed",
            confidence_score=0.9,
        )
        assert ev.is_actionable is True
        assert ev.confidence_score == 0.9

    def test_log_evaluation_missing_field_raises(self):
        from pydantic import ValidationError

        from netalertx.log_monitor import LogEvaluation

        with pytest.raises(ValidationError):
            LogEvaluation(is_actionable=True)  # type: ignore[call-arg]

    def test_log_evaluation_json_round_trip(self):
        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=False,
            root_cause_summary="Transient noise",
            confidence_score=0.2,
        )
        assert LogEvaluation.model_validate_json(ev.model_dump_json()) == ev

    # ── analyze_log_line_with_ai ──────────────────────────────────────────────

    @pytest.fixture
    def llm_actionable(self):
        from utils.ollama_client import FakeLLMClient

        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed: network unreachable",
            confidence_score=0.95,
        )
        return FakeLLMClient(ev.model_dump_json())

    @pytest.fixture
    def llm_not_actionable(self):
        from utils.ollama_client import FakeLLMClient

        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=False,
            root_cause_summary="Transient warning",
            confidence_score=0.1,
        )
        return FakeLLMClient(ev.model_dump_json())

    def test_analyze_returns_log_evaluation(self, llm_actionable):
        import asyncio

        from netalertx.log_monitor import analyze_log_line_with_ai

        result, _trace = asyncio.run(
            analyze_log_line_with_ai(["ERROR ArpScan failed"], llm_actionable)
        )
        assert result.is_actionable is True
        assert result.confidence_score == 0.95

    def test_analyze_calls_llm_once(self, llm_actionable):
        import asyncio

        from netalertx.log_monitor import analyze_log_line_with_ai

        asyncio.run(analyze_log_line_with_ai(["ERROR scan error"], llm_actionable))
        assert len(llm_actionable.calls) == 1

    def test_analyze_returns_safe_default_on_llm_error(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient

        from netalertx.log_monitor import analyze_log_line_with_ai

        # Malformed JSON triggers the except branch
        broken_llm = FakeLLMClient("{not valid json}")
        result, _trace = asyncio.run(
            analyze_log_line_with_ai(["ERROR ..."], broken_llm)
        )
        assert result.is_actionable is False
        assert result.confidence_score == 0.0

    # ── stream behaviour ──────────────────────────────────────────────────────

    def test_stream_non_critical_lines_skip_triage(self, llm_not_actionable):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.log_monitor import tail_netalertx_log_stream

        ssh = FakeSSHClient(stream_data=["INFO scan completed", "DEBUG heartbeat"])
        asyncio.run(
            tail_netalertx_log_stream(ssh_client=ssh, llm_client=llm_not_actionable)
        )
        assert len(llm_not_actionable.calls) == 0

    def test_stream_critical_line_invokes_triage(self, llm_not_actionable, monkeypatch):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        import netalertx.log_monitor as mod
        from netalertx.log_monitor import tail_netalertx_log_stream

        monkeypatch.setattr(mod._debouncer, "record", lambda: False)
        ssh = FakeSSHClient(
            stream_data=["ERROR ArpScan failed: network unreachable", "INFO ok"]
        )
        asyncio.run(
            tail_netalertx_log_stream(ssh_client=ssh, llm_client=llm_not_actionable)
        )
        assert len(llm_not_actionable.calls) == 1

    # ── autonomy gate: level 1 sends notifier, no healer ─────────────────────

    def test_level_1_sends_notifier_no_healer(self, monkeypatch):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.log_monitor import LogEvaluation

        import netalertx.log_monitor as mod

        healer_calls: list = []

        async def fake_healer(ev):
            healer_calls.append(ev)

        monkeypatch.setattr(mod, "_dispatch_to_healer", fake_healer)
        monkeypatch.setattr(mod._debouncer, "record", lambda: True)

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed",
            confidence_score=0.95,
        )
        llm = FakeLLMClient(ev.model_dump_json())
        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()
        ssh = FakeSSHClient(stream_data=["ERROR ArpScan failed: network unreachable"])

        asyncio.run(
            mod.tail_netalertx_log_stream(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )

        assert len(notifier.sent) == 1
        assert healer_calls == []

    # ── reconnect on stream failure ───────────────────────────────────────────

    def test_stream_reconnects_on_ose_error(self, monkeypatch):
        import asyncio as asyncio_mod

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        import netalertx.log_monitor as mod

        async def no_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(asyncio_mod, "sleep", no_sleep)

        call_count = [0]

        class FailOnceFakeSSH:
            async def read_file(self, path: str) -> str:
                raise FileNotFoundError(path)

            async def write_file(self, path: str, content: str) -> None:
                pass

            async def run(self, command: str, check: bool = False):
                return 0, "", ""

            async def stream_lines(self, command: str):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise OSError("connection lost")
                return
                yield  # makes this an async generator

        ssh = FailOnceFakeSSH()
        llm = FakeLLMClient("{}")
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        asyncio_mod.run(
            mod.tail_netalertx_log_stream(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )

        assert call_count[0] == 2


# ── netalertx/health.py and netalertx/mqtt_subscriber.py ─────────────────────


class TestNetAlertXHealthMonitor:
    # ── DevicePresenceEvent schema ────────────────────────────────────────────

    def test_device_presence_event_valid(self):
        from netalertx.mqtt_subscriber import DevicePresenceEvent

        ev = DevicePresenceEvent(
            topic="system-sensors/binary_sensor/AA:BB:CC/state",
            payload="home",
        )
        assert ev.topic == "system-sensors/binary_sensor/AA:BB:CC/state"
        assert ev.payload == "home"

    def test_device_presence_event_json_round_trip(self):
        from netalertx.mqtt_subscriber import DevicePresenceEvent

        ev = DevicePresenceEvent(
            topic="system-sensors/sensor/11:22:33/state",
            payload='{"state": true}',
        )
        assert DevicePresenceEvent.model_validate_json(ev.model_dump_json()) == ev

    # ── HealthReport schema ───────────────────────────────────────────────────

    def test_health_report_valid_construction(self):
        from netalertx.health import HealthReport

        r = HealthReport(
            last_scan_age_minutes=5,
            device_counts={"total": 10, "online": 8},
            mqtt_active=True,
            anomalies=[],
            netalertx_version="v26.7.1",
        )
        assert r.last_scan_age_minutes == 5
        assert r.mqtt_active is True

    def test_health_report_missing_field_raises(self):
        from pydantic import ValidationError

        from netalertx.health import HealthReport

        with pytest.raises(ValidationError):
            HealthReport(  # type: ignore[call-arg]
                last_scan_age_minutes=5,
                device_counts={"total": 1},
                mqtt_active=False,
                # missing anomalies and netalertx_version
            )

    def test_health_report_json_round_trip(self):
        from netalertx.health import HealthReport

        r = HealthReport(
            last_scan_age_minutes=12,
            device_counts={"total": 3, "online": 1},
            mqtt_active=False,
            anomalies=["Last scan is 25 minutes old"],
            netalertx_version="v26.7.1",
        )
        assert HealthReport.model_validate_json(r.model_dump_json()) == r

    # ── _compute_scan_age ─────────────────────────────────────────────────────

    def test_scan_age_empty_devices_returns_zero(self):
        from netalertx.health import _compute_scan_age

        assert _compute_scan_age([]) == 0

    def test_scan_age_fresh_device(self):
        from datetime import datetime, timezone

        from netalertx.health import _compute_scan_age

        now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        devices = [{"devLastSeen": "2026-07-20 11:58:00"}]
        assert _compute_scan_age(devices, now=now) == 2

    def test_scan_age_stale_scan_exceeds_threshold(self):
        from datetime import datetime, timezone

        from netalertx.health import _compute_scan_age

        now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        devices = [{"devLastSeen": "2026-07-20 11:00:00"}]
        assert _compute_scan_age(devices, now=now) == 60

    def test_scan_age_no_timestamps_returns_zero(self):
        from netalertx.health import _compute_scan_age

        devices = [{"devMAC": "AA:BB:CC:DD:EE:FF"}]
        assert _compute_scan_age(devices) == 0

    def test_scan_age_picks_most_recent(self):
        from datetime import datetime, timezone

        from netalertx.health import _compute_scan_age

        now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        devices = [
            {"devLastSeen": "2026-07-20 11:00:00"},  # 60 min ago
            {"devLastSeen": "2026-07-20 11:55:00"},  # 5 min ago
        ]
        assert _compute_scan_age(devices, now=now) == 5

    # ── FakeMQTTSubscriber ────────────────────────────────────────────────────

    def test_fake_mqtt_puts_events_in_queue(self):
        import asyncio

        from netalertx.mqtt_subscriber import DevicePresenceEvent, FakeMQTTSubscriber

        events = [
            DevicePresenceEvent(topic="t/1/state", payload="home"),
            DevicePresenceEvent(topic="t/2/state", payload="not_home"),
        ]
        sub = FakeMQTTSubscriber(events=events)
        queue: asyncio.Queue[DevicePresenceEvent] = asyncio.Queue()

        asyncio.run(sub.subscribe(queue))

        assert queue.qsize() == 2
        assert sub.subscribe_calls == 1

    def test_fake_mqtt_raises_error(self):
        import asyncio

        from netalertx.mqtt_subscriber import FakeMQTTSubscriber

        sub = FakeMQTTSubscriber(error=RuntimeError("broker down"))
        with pytest.raises(RuntimeError, match="broker down"):
            asyncio.run(sub.subscribe(asyncio.Queue()))

    # ── poll_once ─────────────────────────────────────────────────────────────

    def _make_api_client(self, devices, version="v26.7.1"):
        """Return a minimal fake API client for health tests."""
        import json

        import httpx

        class _FakeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/devices" in str(request.url):
                    body = json.dumps({"devices": devices}).encode()
                elif "/health" in str(request.url):
                    body = json.dumps({"version": version}).encode()
                else:
                    return httpx.Response(404, content=b"not found")
                return httpx.Response(200, content=body)

        from netalertx.api_client import NetAlertXAPIClient

        return NetAlertXAPIClient(
            base_url="http://fake-netalertx",
            api_token="tok",
            http_client=httpx.AsyncClient(transport=_FakeTransport()),
        )

    def test_poll_once_returns_health_report(self, monkeypatch):
        import asyncio
        from datetime import datetime, timezone

        import netalertx.health as health_mod

        fixed_now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        _orig = health_mod._compute_scan_age
        monkeypatch.setattr(
            health_mod,
            "_compute_scan_age",
            lambda devices, now=None: _orig(devices, now=fixed_now),
        )
        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:FF",
                "devName": "router",
                "devLastSeen": "2026-07-20 11:58:00",  # 2 min before fixed_now
                "devStatus": "online",
                "devIsNew": False,
            }
        ]
        api = self._make_api_client(devices)
        from netalertx.health import NetAlertXHealthMonitor

        monitor = NetAlertXHealthMonitor(api_client=api, max_scan_age_minutes=20)
        queue: asyncio.Queue = asyncio.Queue()
        report = asyncio.run(monitor.poll_once(queue))

        assert report.netalertx_version == "v26.7.1"
        assert report.device_counts["total"] == 1
        assert report.device_counts["online"] == 1
        assert report.anomalies == []

    def test_poll_stale_scan_adds_anomaly(self, monkeypatch):
        import asyncio
        from datetime import datetime, timezone

        import netalertx.health as health_mod

        fixed_now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        _orig = health_mod._compute_scan_age
        monkeypatch.setattr(
            health_mod,
            "_compute_scan_age",
            lambda devices, now=None: _orig(devices, now=fixed_now),
        )
        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:FF",
                "devName": "router",
                "devLastSeen": "2026-07-20 11:00:00",  # 60 min ago from fixed_now
                "devStatus": "online",
                "devIsNew": False,
            }
        ]
        api = self._make_api_client(devices)
        from netalertx.health import NetAlertXHealthMonitor

        monitor = NetAlertXHealthMonitor(api_client=api, max_scan_age_minutes=20)
        queue: asyncio.Queue = asyncio.Queue()
        report = asyncio.run(monitor.poll_once(queue))

        assert len(report.anomalies) == 1
        assert "minutes old" in report.anomalies[0]

    def test_poll_mqtt_active_when_events_in_queue(self):
        import asyncio

        devices: list[dict] = []
        api = self._make_api_client(devices)
        from netalertx.health import NetAlertXHealthMonitor
        from netalertx.mqtt_subscriber import DevicePresenceEvent

        monitor = NetAlertXHealthMonitor(api_client=api, max_scan_age_minutes=20)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(DevicePresenceEvent(topic="t/1/state", payload="home"))

        report = asyncio.run(monitor.poll_once(queue))

        assert report.mqtt_active is True
        assert queue.empty()

    def test_poll_no_events_mqtt_inactive(self):
        import asyncio

        devices: list[dict] = []
        api = self._make_api_client(devices)
        from netalertx.health import NetAlertXHealthMonitor

        monitor = NetAlertXHealthMonitor(api_client=api, max_scan_age_minutes=20)
        queue: asyncio.Queue = asyncio.Queue()
        report = asyncio.run(monitor.poll_once(queue))

        assert report.mqtt_active is False

    def test_poll_new_device_triggers_sync(self):
        import asyncio

        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:11",
                "devName": "",
                "devLastSeen": "",
                "devStatus": "online",
                "devIsNew": True,
            }
        ]
        api = self._make_api_client(devices)

        class _FakeHaNameSync:
            def __init__(self):
                self.synced_macs: list[str] = []

            async def sync_device(self, mac: str) -> None:
                self.synced_macs.append(mac)

        fake_sync = _FakeHaNameSync()
        from netalertx.health import NetAlertXHealthMonitor

        monitor = NetAlertXHealthMonitor(
            api_client=api,
            ha_name_sync=fake_sync,
            max_scan_age_minutes=20,
        )
        asyncio.run(monitor.poll_once(asyncio.Queue()))
        assert "AA:BB:CC:DD:EE:11" in fake_sync.synced_macs

    def test_poll_blank_name_triggers_sync(self):
        import asyncio

        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:22",
                "devName": "",
                "devLastSeen": "",
                "devStatus": "online",
                "devIsNew": False,
            }
        ]
        api = self._make_api_client(devices)

        class _FakeHaNameSync:
            def __init__(self):
                self.synced_macs: list[str] = []

            async def sync_device(self, mac: str) -> None:
                self.synced_macs.append(mac)

        fake_sync = _FakeHaNameSync()
        from netalertx.health import NetAlertXHealthMonitor

        monitor = NetAlertXHealthMonitor(
            api_client=api,
            ha_name_sync=fake_sync,
            max_scan_age_minutes=20,
        )
        asyncio.run(monitor.poll_once(asyncio.Queue()))
        assert "AA:BB:CC:DD:EE:22" in fake_sync.synced_macs

    def test_poll_named_existing_device_no_sync(self):
        import asyncio

        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:33",
                "devName": "laptop",
                "devLastSeen": "",
                "devStatus": "online",
                "devIsNew": False,
            }
        ]
        api = self._make_api_client(devices)

        class _FakeHaNameSync:
            def __init__(self):
                self.synced_macs: list[str] = []

            async def sync_device(self, mac: str) -> None:
                self.synced_macs.append(mac)

        fake_sync = _FakeHaNameSync()
        from netalertx.health import NetAlertXHealthMonitor

        monitor = NetAlertXHealthMonitor(
            api_client=api,
            ha_name_sync=fake_sync,
            max_scan_age_minutes=20,
        )
        asyncio.run(monitor.poll_once(asyncio.Queue()))
        assert fake_sync.synced_macs == []

    def test_poll_about_failure_returns_unknown_version(self):
        import asyncio
        import json

        import httpx

        class _PartialTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/devices" in str(request.url):
                    return httpx.Response(
                        200, content=json.dumps({"devices": []}).encode()
                    )
                return httpx.Response(500, content=b"error")

        from netalertx.api_client import NetAlertXAPIClient
        from netalertx.health import NetAlertXHealthMonitor

        api = NetAlertXAPIClient(
            base_url="http://fake",
            api_token="tok",
            http_client=httpx.AsyncClient(transport=_PartialTransport()),
        )
        monitor = NetAlertXHealthMonitor(api_client=api, max_scan_age_minutes=20)
        report = asyncio.run(monitor.poll_once(asyncio.Queue()))
        assert report.netalertx_version == "unknown"


class TestNetAlertXConfigIssue:
    def test_valid_construction(self):
        from netalertx.config_validator import ConfigIssue

        issue = ConfigIssue(field="MQTT_BROKER", message="missing", severity="HIGH")
        assert issue.field == "MQTT_BROKER"
        assert issue.severity == "HIGH"

    def test_missing_field_raises(self):
        from pydantic import ValidationError

        from netalertx.config_validator import ConfigIssue

        with pytest.raises(ValidationError):
            ConfigIssue(field="x", severity="HIGH")  # type: ignore[call-arg]

    def test_json_round_trip(self):
        from netalertx.config_validator import ConfigIssue

        issue = ConfigIssue(field="SCAN_SUBNETS", message="empty", severity="MEDIUM")
        assert ConfigIssue.model_validate_json(issue.model_dump_json()) == issue


class TestNetAlertXConfigValidator:
    # ── validate_app_conf ─────────────────────────────────────────────────────

    def test_valid_app_conf_returns_no_issues(self):
        from netalertx.config_validator import validate_app_conf

        conf = "\n".join(
            [
                "MQTT_BROKER=192.168.1.1",
                "MQTT_PORT=1883",
                "HA_URL=http://homeassistant.local:8123",
                "HA_BEARER_TOKEN=abc123",
                "SCAN_SUBNETS=192.168.1.0/24--eth0",
                "TIMEZONE=America/New_York",
                "LOADED_PLUGINS=ARPSCAN,MQTT,NMAPDEV",
            ]
        )
        assert validate_app_conf(conf) == []

    def test_missing_required_key_returns_issue(self):
        from netalertx.config_validator import validate_app_conf

        conf = "\n".join(
            [
                "MQTT_PORT=1883",
                "HA_URL=http://homeassistant.local:8123",
                "HA_BEARER_TOKEN=abc123",
                "SCAN_SUBNETS=192.168.1.0/24--eth0",
                "TIMEZONE=America/New_York",
                "LOADED_PLUGINS=ARPSCAN,MQTT",
            ]
        )
        issues = validate_app_conf(conf)
        fields = [i.field for i in issues]
        assert "MQTT_BROKER" in fields

    def test_empty_required_value_returns_issue(self):
        from netalertx.config_validator import validate_app_conf

        conf = "\n".join(
            [
                "MQTT_BROKER=",
                "MQTT_PORT=1883",
                "HA_URL=http://homeassistant.local:8123",
                "HA_BEARER_TOKEN=abc123",
                "SCAN_SUBNETS=192.168.1.0/24--eth0",
                "TIMEZONE=America/New_York",
                "LOADED_PLUGINS=ARPSCAN,MQTT",
            ]
        )
        issues = validate_app_conf(conf)
        fields = [i.field for i in issues]
        assert "MQTT_BROKER" in fields

    def test_missing_mqtt_plugin_returns_issue(self):
        from netalertx.config_validator import validate_app_conf

        conf = "\n".join(
            [
                "MQTT_BROKER=192.168.1.1",
                "MQTT_PORT=1883",
                "HA_URL=http://homeassistant.local:8123",
                "HA_BEARER_TOKEN=abc123",
                "SCAN_SUBNETS=192.168.1.0/24--eth0",
                "TIMEZONE=America/New_York",
                "LOADED_PLUGINS=ARPSCAN,NMAPDEV",
            ]
        )
        issues = validate_app_conf(conf)
        assert any(i.field == "LOADED_PLUGINS" and "MQTT" in i.message for i in issues)

    def test_missing_arpscan_plugin_returns_issue(self):
        from netalertx.config_validator import validate_app_conf

        conf = "\n".join(
            [
                "MQTT_BROKER=192.168.1.1",
                "MQTT_PORT=1883",
                "HA_URL=http://homeassistant.local:8123",
                "HA_BEARER_TOKEN=abc123",
                "SCAN_SUBNETS=192.168.1.0/24--eth0",
                "TIMEZONE=America/New_York",
                "LOADED_PLUGINS=MQTT,NMAPDEV",
            ]
        )
        issues = validate_app_conf(conf)
        assert any(
            i.field == "LOADED_PLUGINS" and "ARPSCAN" in i.message for i in issues
        )

    def test_comment_lines_ignored(self):
        from netalertx.config_validator import validate_app_conf

        conf = "\n".join(
            [
                "# This is a comment",
                "MQTT_BROKER=192.168.1.1",
                "MQTT_PORT=1883",
                "HA_URL=http://homeassistant.local:8123",
                "HA_BEARER_TOKEN=abc123",
                "SCAN_SUBNETS=192.168.1.0/24--eth0",
                "TIMEZONE=America/New_York",
                "LOADED_PLUGINS=ARPSCAN,MQTT",
            ]
        )
        assert validate_app_conf(conf) == []

    # ── validate_ha_config ────────────────────────────────────────────────────

    def test_mqtt_key_detected(self):
        from netalertx.config_validator import validate_ha_config

        config_yaml = "homeassistant:\n  name: Home\nmqtt:\n  broker: localhost\n"
        issues = validate_ha_config(config_yaml)
        assert len(issues) == 1
        assert issues[0].field == "mqtt"
        assert issues[0].severity == "HIGH"

    def test_no_mqtt_key_returns_no_issues(self):
        from netalertx.config_validator import validate_ha_config

        config_yaml = "homeassistant:\n  name: Home\n  time_zone: UTC\n"
        assert validate_ha_config(config_yaml) == []

    def test_invalid_yaml_returns_issue(self):
        from netalertx.config_validator import validate_ha_config

        issues = validate_ha_config(":\tnot: valid: yaml\n\t\t")
        assert len(issues) == 1
        assert "parse" in issues[0].message.lower()

    def test_empty_config_returns_no_issues(self):
        from netalertx.config_validator import validate_ha_config

        assert validate_ha_config("") == []

    # ── validate_webhook_automation ───────────────────────────────────────────

    def test_snake_case_field_detected(self):
        from netalertx.config_validator import validate_webhook_automation

        automation_yaml = (
            "trigger:\n"
            "  - platform: webhook\n"
            "    webhook_id: netalertx_event\n"
            "action:\n"
            "  - data:\n"
            "      mac: '{{ trigger.json.eve_mac }}'\n"
        )
        issues = validate_webhook_automation(automation_yaml)
        assert any(i.field == "eve_mac" for i in issues)

    def test_camelcase_fields_return_no_issues(self):
        from netalertx.config_validator import validate_webhook_automation

        automation_yaml = (
            "trigger:\n"
            "  - platform: webhook\n"
            "    webhook_id: netalertx_event\n"
            "action:\n"
            "  - data:\n"
            "      mac: '{{ trigger.json.eveMac }}'\n"
            "      ip: '{{ trigger.json.eveIp }}'\n"
        )
        assert validate_webhook_automation(automation_yaml) == []

    def test_multiple_snake_case_fields_all_detected(self):
        from netalertx.config_validator import validate_webhook_automation

        automation_yaml = "eve_mac: x\neve_ip: y\ndev_vendor: z\n"
        issues = validate_webhook_automation(automation_yaml)
        fields = [i.field for i in issues]
        assert "eve_mac" in fields
        assert "eve_ip" in fields
        assert "dev_vendor" in fields


class TestNetAlertXDiagnostic:
    # ── NetAlertXDiagnostic schema ────────────────────────────────────────────

    def test_valid_construction(self):
        from netalertx.diagnosis import NetAlertXDiagnostic

        d = NetAlertXDiagnostic(
            issue="No devices discovered",
            severity="HIGH",
            category="networking",
            recommended_fix="Add --network=host to Docker run command.",
            affected_netalertx_version="v26.7.1",
        )
        assert d.category == "networking"

    def test_missing_field_raises(self):
        from pydantic import ValidationError

        from netalertx.diagnosis import NetAlertXDiagnostic

        with pytest.raises(ValidationError):
            NetAlertXDiagnostic(issue="x", severity="LOW", category="networking")  # type: ignore[call-arg]

    def test_json_round_trip(self):
        from netalertx.diagnosis import NetAlertXDiagnostic

        d = NetAlertXDiagnostic(
            issue="MQTT broker down",
            severity="MEDIUM",
            category="mqtt",
            recommended_fix="Restart Mosquitto add-on.",
            affected_netalertx_version="v26.7.1",
        )
        assert NetAlertXDiagnostic.model_validate_json(d.model_dump_json()) == d

    # ── diagnose_health_report ────────────────────────────────────────────────

    def _zero_devices_report(self):
        from netalertx.health import HealthReport

        return HealthReport(
            last_scan_age_minutes=25,
            device_counts={"total": 0, "online": 0},
            mqtt_active=False,
            anomalies=[
                "Last scan is 25 minutes old (threshold: 20)",
                "No devices discovered",
            ],
            netalertx_version="v26.7.1",
        )

    def _make_fake_llm(self, category: str = "networking") -> "object":
        from utils.ollama_client import FakeLLMClient

        from netalertx.diagnosis import NetAlertXDiagnostic

        diag = NetAlertXDiagnostic(
            issue="No devices discovered",
            severity="HIGH",
            category=category,
            recommended_fix="Add --network=host to the Docker run command.",
            affected_netalertx_version="v26.7.1",
        )
        return FakeLLMClient(diag.model_dump_json())

    def test_zero_devices_anomaly_returns_networking_diagnostic(self):
        import asyncio

        from netalertx.diagnosis import diagnose_health_report

        report = self._zero_devices_report()
        llm = self._make_fake_llm("networking")
        result, _trace = asyncio.run(diagnose_health_report(report, llm_client=llm))
        assert result is not None
        assert result.category == "networking"
        assert "--network=host" in result.recommended_fix

    def test_no_anomalies_returns_none(self):
        import asyncio

        from netalertx.health import HealthReport

        from netalertx.diagnosis import diagnose_health_report

        report = HealthReport(
            last_scan_age_minutes=5,
            device_counts={"total": 10, "online": 8},
            mqtt_active=True,
            anomalies=[],
            netalertx_version="v26.7.1",
        )
        result, _trace = asyncio.run(diagnose_health_report(report, config_issues=[]))
        assert result is None

    def test_config_issues_trigger_diagnosis(self):
        import asyncio

        from netalertx.config_validator import ConfigIssue
        from netalertx.health import HealthReport

        from netalertx.diagnosis import diagnose_health_report

        report = HealthReport(
            last_scan_age_minutes=5,
            device_counts={"total": 10, "online": 8},
            mqtt_active=True,
            anomalies=[],
            netalertx_version="v26.7.1",
        )
        issue = ConfigIssue(
            field="mqtt",
            message="Top-level mqtt: key found",
            severity="HIGH",
        )
        llm = self._make_fake_llm("mqtt")
        result, _trace = asyncio.run(
            diagnose_health_report(report, config_issues=[issue], llm_client=llm)
        )
        assert result is not None
        assert result.category == "mqtt"

    def test_llm_failure_returns_none(self):
        import asyncio

        from netalertx.diagnosis import diagnose_health_report

        class _CrashingLLM:
            async def chat(self, **_):
                raise RuntimeError("Ollama unavailable")

        result, _trace = asyncio.run(
            diagnose_health_report(
                self._zero_devices_report(), llm_client=_CrashingLLM()
            )
        )
        assert result is None

    def test_llm_called_with_anomaly_context(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient

        from netalertx.diagnosis import NetAlertXDiagnostic, diagnose_health_report

        diag = NetAlertXDiagnostic(
            issue="test",
            severity="LOW",
            category="networking",
            recommended_fix="none",
            affected_netalertx_version="v26.7.1",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        asyncio.run(diagnose_health_report(self._zero_devices_report(), llm_client=llm))
        assert len(llm.calls) == 1
        user_msg = llm.calls[0]["messages"][1]["content"]
        assert "No devices discovered" in user_msg


# ===========================================================================
# TestNetAlertXHealer  (item 18)
# ===========================================================================
class TestNetAlertXHealer:
    """Tests for netalertx/healer.py — all four autonomy levels + version bump."""

    # -----------------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------------

    def _make_diag(
        self,
        category: str = "networking",
        severity: str = "HIGH",
        recommended_fix: str = "Restart the container.",
    ):
        from netalertx.diagnosis import NetAlertXDiagnostic

        return NetAlertXDiagnostic(
            issue="scan failure",
            severity=severity,
            category=category,
            recommended_fix=recommended_fix,
            affected_netalertx_version="v26.7.1",
        )

    def _make_healer(
        self,
        gate,
        ssh_client=None,
        ha_ssh_client=None,
        api_client=None,
        notifier=None,
        db_path=":memory:",
    ):
        import sqlite3

        from netalertx.healer import NetAlertXHealer
        from utils.ssh_client import FakeSSHClient

        if ssh_client is None:
            ssh_client = FakeSSHClient()
        if ha_ssh_client is None:
            ha_ssh_client = FakeSSHClient()
        if notifier is None:
            from utils.notify import FakeNotifier

            notifier = FakeNotifier(approve=True)
        if api_client is None:
            api_client = _FakeAPIClient()

        # Ensure netalertx_state table exists in the in-memory DB
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS netalertx_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

        return NetAlertXHealer(
            gate=gate,
            ssh_client=ssh_client,
            ha_ssh_client=ha_ssh_client,
            api_client=api_client,
            notifier=notifier,
            db_path=db_path,
        )

    # -----------------------------------------------------------------------
    # Level 1 — report only: notifier event sent, no SSH writes
    # -----------------------------------------------------------------------

    def test_level_1_notifier_event_no_writes(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        ssh = FakeSSHClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh, notifier=notifier)
        asyncio.run(healer.heal(self._make_diag("networking")))

        # require_approval was called (gate was consulted)
        assert len(gate.require_approval_calls) >= 1
        # FakeAutonomyGate with approval_result=False calls notifier.send
        assert len(notifier.sent) >= 1
        # No SSH writes
        assert ssh.written_files == {}

    # -----------------------------------------------------------------------
    # Level 2 — suggest: approval requested; on rejection nothing executes
    # -----------------------------------------------------------------------

    def test_level_2_rejection_no_writes(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        ssh = FakeSSHClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh, notifier=notifier)
        asyncio.run(healer.heal(self._make_diag("mqtt")))

        assert len(gate.require_approval_calls) >= 1
        assert ssh.written_files == {}
        assert ssh.commands_run == []

    def test_level_2_approval_executes_mqtt_fix(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ssh = FakeSSHClient(file_contents={"/data/app.conf": "MQTT_BROKER=wronghost\n"})
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh, notifier=notifier
        )
        asyncio.run(
            healer.heal(
                self._make_diag(
                    "mqtt", recommended_fix="MQTT_BROKER=homeassistant.local"
                )
            )
        )

        # app.conf was written
        assert "/data/app.conf" in ssh.written_files

    # -----------------------------------------------------------------------
    # Level 3 — guided: app.conf auto-proceeds (MEDIUM); HA config approval
    # -----------------------------------------------------------------------

    def test_level_3_app_conf_auto_proceeds(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        # auto_execute_result=True means should_auto_execute returns True → MEDIUM auto-proceeds
        gate = FakeAutonomyGate(auto_execute_result=True, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ssh = FakeSSHClient(file_contents={"/data/app.conf": "MQTT_BROKER=oldhost\n"})
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh, notifier=notifier
        )
        asyncio.run(
            healer.heal(
                self._make_diag(
                    "mqtt", recommended_fix="MQTT_BROKER=homeassistant.local"
                )
            )
        )

        # app.conf written without require_approval being called for MEDIUM
        assert "/data/app.conf" in ssh.written_files
        # should_auto_execute was checked
        assert len(gate.should_auto_execute_calls) >= 1

    def test_level_3_ha_config_requires_approval(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        # auto_execute_result=False means should_auto_execute returns False → HIGH needs approval
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ssh = FakeSSHClient(
            file_contents={
                "/data/app.conf": "MQTT_BROKER=x\nLOADED_PLUGINS=MQTT ARPSCAN\n"
            }
        )
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "mqtt:\n  broker: homeassistant.local\nhomeassistant:\n  name: Home\n"
            }
        )
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh, notifier=notifier
        )
        asyncio.run(healer.heal(self._make_diag("mqtt")))

        # require_approval was called (gate gated the HA config action)
        ha_approval_calls = [
            c for c in gate.require_approval_calls if "mqtt" in c["subject"].lower()
        ]
        assert len(ha_approval_calls) >= 1

    # -----------------------------------------------------------------------
    # Level 4 — autonomous: networking triggers restart + rescan without HITL
    # -----------------------------------------------------------------------

    def test_level_4_networking_restart_rescan(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=True)
        ssh = FakeSSHClient()
        api = _FakeAPIClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh, api_client=api)
        asyncio.run(healer.heal(self._make_diag("networking")))

        # Container restart command issued
        assert any("docker restart" in cmd for cmd in ssh.commands_run)
        # API rescan triggered
        assert api.trigger_scan_calls == 1
        # No HITL calls at level 4 for HIGH risk
        assert len(gate.require_approval_calls) == 0

    def test_level_4_no_hitl_for_high_risk(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=True)
        ssh = FakeSSHClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh)
        asyncio.run(healer.heal(self._make_diag("networking")))

        # should_auto_execute was called and returned True — no require_approval
        assert len(gate.should_auto_execute_calls) >= 1
        assert len(gate.require_approval_calls) == 0

    # -----------------------------------------------------------------------
    # Version bump detection
    # -----------------------------------------------------------------------

    def test_version_bump_triggers_hitl_at_level_3(self, tmp_path):
        import asyncio
        import sqlite3

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        db_path = str(tmp_path / "test.db")

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS netalertx_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO netalertx_state (key, value) VALUES ('netalertx_version', 'v26.6.0')"
            )
            conn.commit()

        healer = self._make_healer(gate=gate, notifier=notifier, db_path=db_path)
        approved = asyncio.run(healer.check_version_bump("v26.7.1"))

        # HITL was requested for the version bump
        assert len(gate.require_approval_calls) == 1
        assert "v26.6.0" in gate.require_approval_calls[0]["body"]
        assert "v26.7.1" in gate.require_approval_calls[0]["body"]
        # With approval_result=True, healing is allowed to proceed
        assert approved is True

    def test_no_version_bump_returns_true_immediately(self, tmp_path):
        import asyncio

        from utils.autonomy import FakeAutonomyGate

        gate = FakeAutonomyGate(auto_execute_result=True)
        db_path = str(tmp_path / "test.db")
        healer = self._make_healer(gate=gate, db_path=db_path)
        # First call stores the version
        asyncio.run(healer.check_version_bump("v26.7.1"))
        # Second call with same version — no HITL
        result = asyncio.run(healer.check_version_bump("v26.7.1"))

        assert result is True
        assert len(gate.require_approval_calls) == 0

    def test_version_bump_blocked_returns_false(self, tmp_path):
        import asyncio
        import sqlite3

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        db_path = str(tmp_path / "test.db")

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS netalertx_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO netalertx_state (key, value) VALUES ('netalertx_version', 'v26.5.0')"
            )
            conn.commit()

        healer = self._make_healer(gate=gate, notifier=notifier, db_path=db_path)
        result = asyncio.run(healer.check_version_bump("v26.7.1"))

        assert result is False

    # -----------------------------------------------------------------------
    # ha_integration category
    # -----------------------------------------------------------------------

    def test_ha_integration_requires_approval(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        ssh = FakeSSHClient()
        ha_ssh = FakeSSHClient()
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh, notifier=notifier
        )
        asyncio.run(healer.heal(self._make_diag("ha_integration")))

        assert len(gate.require_approval_calls) >= 1
        assert ha_ssh.written_files == {}

    # -----------------------------------------------------------------------
    # Log monitor dispatch wiring
    # -----------------------------------------------------------------------

    def test_dispatch_to_healer_calls_heal(self):
        import asyncio

        from netalertx.log_monitor import LogEvaluation, _dispatch_to_healer

        healer = _FakeHealer()
        evaluation = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed: network unreachable",
            confidence_score=0.95,
        )
        asyncio.run(_dispatch_to_healer(evaluation, healer=healer))
        assert healer.heal_calls == 1
        assert healer.last_diagnostic is not None
        assert healer.last_diagnostic.category == "networking"

    def test_dispatch_without_healer_logs_skip(self):
        import asyncio

        from netalertx.log_monitor import LogEvaluation, _dispatch_to_healer

        evaluation = LogEvaluation(
            is_actionable=True,
            root_cause_summary="Something broke",
            confidence_score=0.8,
        )
        # Should not raise even without a healer
        asyncio.run(_dispatch_to_healer(evaluation, healer=None))

    def test_evaluation_to_diagnostic_mqtt_category(self):
        from netalertx.log_monitor import LogEvaluation, _evaluation_to_diagnostic

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="MQTT broker connection refused",
            confidence_score=0.85,
        )
        diag = _evaluation_to_diagnostic(ev)
        assert diag.category == "mqtt"
        assert diag.severity == "MEDIUM"

    def test_evaluation_to_diagnostic_networking_category(self):
        from netalertx.log_monitor import LogEvaluation, _evaluation_to_diagnostic

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed: network unreachable",
            confidence_score=0.95,
        )
        diag = _evaluation_to_diagnostic(ev)
        assert diag.category == "networking"
        assert diag.severity == "HIGH"

    # -----------------------------------------------------------------------
    # Additional edge-case coverage
    # -----------------------------------------------------------------------

    def test_merge_conf_appends_new_key(self):
        from netalertx.healer import _merge_conf

        current = "EXISTING_KEY=value\n"
        result = _merge_conf(current, {"NEW_KEY": "newval"})
        assert "NEW_KEY=newval" in result
        assert "EXISTING_KEY=value" in result

    def test_merge_conf_updates_existing_key(self):
        from netalertx.healer import _merge_conf

        current = "MQTT_BROKER=oldhost\nOTHER=x\n"
        result = _merge_conf(current, {"MQTT_BROKER": "newhost"})
        assert "MQTT_BROKER=newhost" in result
        assert "MQTT_BROKER=oldhost" not in result

    def test_merge_conf_preserves_comments(self):
        from netalertx.healer import _merge_conf

        current = "# comment\nKEY=val\n"
        result = _merge_conf(current, {})
        assert "# comment" in result

    def test_extract_conf_overrides_parses_key_value(self):
        from netalertx.healer import _extract_conf_overrides

        text = "MQTT_BROKER=homeassistant.local\nMQTT_PORT=1883"
        result = _extract_conf_overrides(text)
        assert result.get("MQTT_BROKER") == "homeassistant.local"
        assert result.get("MQTT_PORT") == "1883"

    def test_extract_conf_overrides_ignores_lowercase(self):
        from netalertx.healer import _extract_conf_overrides

        result = _extract_conf_overrides("restart the container")
        assert result == {}

    def test_level_2_approval_triggers_networking_restart(self):
        """require_approval returning True at level 2 should execute restart + rescan."""
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        ssh = FakeSSHClient()
        api = _FakeAPIClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh, api_client=api)
        asyncio.run(healer.heal(self._make_diag("networking")))

        assert any("docker restart" in cmd for cmd in ssh.commands_run)
        assert api.trigger_scan_calls == 1

    def test_version_bump_blocked_log_and_false(self, tmp_path):
        """When version bump is blocked, returns False and logs the block."""
        import asyncio
        import sqlite3

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        db_path = str(tmp_path / "test.db")

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS netalertx_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO netalertx_state (key, value) VALUES ('netalertx_version', 'v26.6.0')"
            )
            conn.commit()

        healer = self._make_healer(gate=gate, notifier=notifier, db_path=db_path)
        result = asyncio.run(healer.check_version_bump("v26.7.1"))
        assert result is False
        assert len(gate.require_approval_calls) == 1

    def test_ha_integration_approval_writes_automation(self):
        """When approved, _fix_ha_automation_fields writes fixed content."""
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        old_yaml = "- trigger:\n    platform: webhook\n    eve_mac: '{{trigger.json.eve_mac}}'\n"
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ssh = FakeSSHClient()
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/automations.yaml": old_yaml,
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n",
            }
        )
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh, notifier=notifier
        )
        asyncio.run(healer.heal(self._make_diag("ha_integration")))

        assert "/config/automations.yaml" in ha_ssh.written_files
        written = ha_ssh.written_files["/config/automations.yaml"]
        assert "eveMac" in written
        assert "eve_mac" not in written

    def test_ha_automation_no_changes_does_not_write(self):
        """If automation already uses camelCase, no write happens."""
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        camel_yaml = (
            "- trigger:\n    platform: webhook\n    eveMac: '{{trigger.json.eveMac}}'\n"
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ha_ssh = FakeSSHClient(file_contents={"/config/automations.yaml": camel_yaml})
        healer = self._make_healer(gate=gate, ha_ssh_client=ha_ssh, notifier=notifier)
        asyncio.run(healer.heal(self._make_diag("ha_integration")))

        # Already correct — no write
        assert "/config/automations.yaml" not in ha_ssh.written_files

    def test_rewrite_app_conf_creates_file_if_missing(self):
        """app.conf write proceeds even when file doesn't exist yet."""
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=True)
        ssh = FakeSSHClient()  # no file contents → FileNotFoundError on read
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        healer = self._make_healer(gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh)
        asyncio.run(
            healer.heal(
                self._make_diag(
                    "mqtt", recommended_fix="MQTT_BROKER=homeassistant.local"
                )
            )
        )
        assert "/data/app.conf" in ssh.written_files

    def test_database_category_uses_require_approval(self):
        """database/version categories send notification and don't write files."""
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        ssh = FakeSSHClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh, notifier=notifier)
        asyncio.run(healer.heal(self._make_diag("database")))

        assert len(gate.require_approval_calls) >= 1
        assert ssh.written_files == {}

    def test_mqtt_ha_config_no_mqtt_key_skips_write(self):
        """When HA configuration.yaml has no mqtt: key, _remove_ha_mqtt_key does nothing."""
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=True, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ssh = FakeSSHClient(file_contents={"/data/app.conf": "MQTT_BROKER=x\n"})
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, ha_ssh_client=ha_ssh, notifier=notifier
        )
        asyncio.run(healer.heal(self._make_diag("mqtt")))

        # HA config had no mqtt: key, so only app.conf was written
        assert "/config/configuration.yaml" not in ha_ssh.written_files


# ---------------------------------------------------------------------------
# Test-only helpers (not part of production code)
# ---------------------------------------------------------------------------


class _FakeAPIClient:
    """Minimal API client double for healer tests."""

    def __init__(self) -> None:
        self.trigger_scan_calls = 0
        self.trigger_scan_types: list[str] = []

    async def trigger_scan(self, scan_type: str = "ARPSCAN") -> None:
        self.trigger_scan_calls += 1
        self.trigger_scan_types.append(scan_type)

    async def get_devices(self):
        return []

    async def get_about(self):
        return {"version": "v26.7.1"}


class _FakeHealer:
    """Minimal healer double for log_monitor dispatch tests."""

    def __init__(self) -> None:
        self.heal_calls = 0
        self.last_diagnostic = None

    async def heal(self, diagnostic) -> None:
        self.heal_calls += 1
        self.last_diagnostic = diagnostic


# ===========================================================================
# TestNetAlertXMaintenanceValidator  (item 19)
# ===========================================================================


class TestNetAlertXMaintenanceValidator:
    """Tests for the three new validators in config_validator.py (item 19)."""

    # ── validate_ha_automation_files ─────────────────────────────────────────

    def test_netalertx_webhook_snake_case_detected(self):
        from netalertx.config_validator import validate_ha_automation_files

        files = {
            "automations.yaml": (
                "- trigger:\n"
                "  - platform: webhook\n"
                "    webhook_id: netalertx_event\n"
                "  action:\n"
                "  - data:\n"
                "      mac: '{{ trigger.json.eve_mac }}'\n"
            )
        }
        issues = validate_ha_automation_files(files)
        assert any(i.field == "eve_mac" for i in issues)

    def test_non_netalertx_automation_ignored(self):
        from netalertx.config_validator import validate_ha_automation_files

        files = {
            "automations.yaml": (
                "- trigger:\n"
                "  - platform: webhook\n"
                "    webhook_id: some_other_event\n"
                "  action:\n"
                "  - data:\n"
                "      mac: '{{ trigger.json.eve_mac }}'\n"
            )
        }
        # No 'netalertx' in the content → ignored
        assert validate_ha_automation_files(files) == []

    def test_camelcase_fields_return_no_issues(self):
        from netalertx.config_validator import validate_ha_automation_files

        files = {
            "automations.yaml": (
                "- trigger:\n"
                "  - platform: webhook\n"
                "    webhook_id: netalertx_event\n"
                "  action:\n"
                "  - data:\n"
                "      mac: '{{ trigger.json.eveMac }}'\n"
                "      ip: '{{ trigger.json.eveIp }}'\n"
            )
        }
        assert validate_ha_automation_files(files) == []

    def test_multiple_files_only_netalertx_checked(self):
        from netalertx.config_validator import validate_ha_automation_files

        files = {
            "light_auto.yaml": "- trigger:\n  - platform: webhook\n    webhook_id: lights\n  action:\n  - data:\n      eve_mac: x\n",
            "nax_auto.yaml": "- trigger:\n  - platform: webhook\n    webhook_id: netalertx_event\n  action:\n  - data:\n      eve_mac: '{{ trigger.json.eve_mac }}'\n",
        }
        issues = validate_ha_automation_files(files)
        # Only nax_auto.yaml is a NetAlertX automation
        assert all("nax_auto.yaml" in i.message for i in issues)
        assert len(issues) == 1

    def test_filename_appears_in_issue_message(self):
        from netalertx.config_validator import validate_ha_automation_files

        files = {
            "my_netalertx_automation.yaml": "trigger:\n- platform: webhook\n  webhook_id: netalertx_hook\naction:\n- data:\n    mac: '{{ trigger.json.eve_mac }}'\n"
        }
        issues = validate_ha_automation_files(files)
        assert any("my_netalertx_automation.yaml" in i.message for i in issues)

    # ── validate_mqtt_entity_coverage ────────────────────────────────────────

    def test_device_absent_from_ha_returns_warning(self):
        from netalertx.config_validator import validate_mqtt_entity_coverage

        devices = [{"devMAC": "AA:BB:CC:DD:EE:FF", "devName": "router"}]
        ha_states: list[dict] = []
        issues = validate_mqtt_entity_coverage(devices, ha_states)
        assert len(issues) == 1
        assert issues[0].field == "mqtt_entity_divergence"
        assert issues[0].severity == "WARNING"
        assert "AA:BB:CC:DD:EE:FF" in issues[0].message

    def test_device_present_in_ha_returns_no_issue(self):
        from netalertx.config_validator import validate_mqtt_entity_coverage

        devices = [{"devMAC": "AA:BB:CC:DD:EE:FF", "devName": "router"}]
        ha_states = [
            {
                "entity_id": "device_tracker.router",
                "attributes": {"mac_address": "AA:BB:CC:DD:EE:FF"},
            }
        ]
        assert validate_mqtt_entity_coverage(devices, ha_states) == []

    def test_mac_normalization_matches_different_formats(self):
        from netalertx.config_validator import validate_mqtt_entity_coverage

        devices = [{"devMAC": "aabbccddeeff", "devName": "switch"}]
        ha_states = [
            {
                "entity_id": "device_tracker.switch",
                "attributes": {"mac_address": "AA:BB:CC:DD:EE:FF"},
            }
        ]
        # Both normalize to AA:BB:CC:DD:EE:FF → no divergence
        assert validate_mqtt_entity_coverage(devices, ha_states) == []

    def test_empty_mac_skipped(self):
        from netalertx.config_validator import validate_mqtt_entity_coverage

        devices = [{"devMAC": "", "devName": "unknown"}]
        assert validate_mqtt_entity_coverage(devices, []) == []

    def test_ha_state_without_mac_attribute_ignored(self):
        from netalertx.config_validator import validate_mqtt_entity_coverage

        devices = [{"devMAC": "AA:BB:CC:DD:EE:FF", "devName": "router"}]
        ha_states = [{"entity_id": "device_tracker.router", "attributes": {}}]
        issues = validate_mqtt_entity_coverage(devices, ha_states)
        assert len(issues) == 1

    # ── validate_db_row_counts ────────────────────────────────────────────────

    def test_over_threshold_returns_warning(self):
        from netalertx.config_validator import validate_db_row_counts

        metrics = {"Plugins_History": 150000.0, "Events": 200000.0}
        issues = validate_db_row_counts(metrics, max_rows=100000)
        fields = [i.field for i in issues]
        assert "Plugins_History" in fields
        assert "Events" in fields
        assert all(i.severity == "WARNING" for i in issues)

    def test_under_threshold_returns_no_issues(self):
        from netalertx.config_validator import validate_db_row_counts

        metrics = {"Plugins_History": 50000.0, "Events": 30000.0}
        assert validate_db_row_counts(metrics, max_rows=100000) == []

    def test_missing_metric_key_skipped(self):
        from netalertx.config_validator import validate_db_row_counts

        metrics: dict[str, float] = {}
        assert validate_db_row_counts(metrics, max_rows=100000) == []

    def test_exactly_at_threshold_no_issue(self):
        from netalertx.config_validator import validate_db_row_counts

        metrics = {"Plugins_History": 100000.0}
        assert validate_db_row_counts(metrics, max_rows=100000) == []


# ===========================================================================
# TestNetAlertXMaintenanceHealer  (item 19)
# ===========================================================================


class TestNetAlertXMaintenanceHealer:
    """Tests for heal_maintenance_issues in healer.py (item 19)."""

    def _make_issue(self, field: str, message: str = "test", severity: str = "MEDIUM"):
        from netalertx.config_validator import ConfigIssue

        return ConfigIssue(field=field, message=message, severity=severity)

    def _make_healer(
        self, gate, ssh_client=None, ha_ssh_client=None, api_client=None, notifier=None
    ):
        import sqlite3

        from netalertx.healer import NetAlertXHealer
        from utils.ssh_client import FakeSSHClient

        if ssh_client is None:
            ssh_client = FakeSSHClient()
        if ha_ssh_client is None:
            ha_ssh_client = FakeSSHClient(
                file_contents={
                    "/config/automations.yaml": "- trigger:\n  - platform: webhook\n    webhook_id: netalertx_event\n  action:\n  - data:\n      mac: eve_mac\n"
                }
            )
        if notifier is None:
            from utils.notify import FakeNotifier

            notifier = FakeNotifier(approve=True)
        if api_client is None:
            api_client = _FakeAPIClient()

        db_path = ":memory:"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS netalertx_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

        return NetAlertXHealer(
            gate=gate,
            ssh_client=ssh_client,
            ha_ssh_client=ha_ssh_client,
            api_client=api_client,
            notifier=notifier,
            db_path=db_path,
        )

    # ── webhook field fix ────────────────────────────────────────────────────

    def test_webhook_snake_case_level_4_auto_fixes(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=True)
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/automations.yaml": (
                    "- trigger:\n  - platform: webhook\n    webhook_id: netalertx_event\n"
                    "  action:\n  - data:\n      mac: eve_mac\n"
                )
            }
        )
        healer = self._make_healer(gate=gate, ha_ssh_client=ha_ssh)
        issue = self._make_issue("eve_mac", severity="MEDIUM")
        asyncio.run(healer.heal_maintenance_issues([issue]))

        # automations.yaml was written (snake_case replaced)
        assert "/config/automations.yaml" in ha_ssh.written_files
        # No HITL call needed at level 4
        assert len(gate.require_approval_calls) == 0

    def test_webhook_snake_case_level_3_requires_approval(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/automations.yaml": "- trigger:\n  - platform: webhook\n    webhook_id: netalertx_event\n"
            }
        )
        healer = self._make_healer(gate=gate, ha_ssh_client=ha_ssh, notifier=notifier)
        issue = self._make_issue("eve_mac", severity="MEDIUM")
        asyncio.run(healer.heal_maintenance_issues([issue]))

        # Approval was requested but rejected → no write
        assert len(gate.require_approval_calls) == 1
        assert ha_ssh.written_files == {}

    def test_webhook_snake_case_level_3_approval_writes(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        ha_ssh = FakeSSHClient(
            file_contents={
                "/config/automations.yaml": (
                    "- trigger:\n  - platform: webhook\n    webhook_id: netalertx_event\n"
                    "  action:\n  - data:\n      mac: eve_mac\n"
                )
            }
        )
        healer = self._make_healer(gate=gate, ha_ssh_client=ha_ssh, notifier=notifier)
        issue = self._make_issue("eve_mac", severity="MEDIUM")
        asyncio.run(healer.heal_maintenance_issues([issue]))

        assert "/config/automations.yaml" in ha_ssh.written_files

    # ── MQTT divergence: notify only at all levels ───────────────────────────

    def test_mqtt_divergence_sends_notifier_event_no_writes(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        ssh = FakeSSHClient()
        healer = self._make_healer(gate=gate, ssh_client=ssh, notifier=notifier)
        issue = self._make_issue(
            "mqtt_entity_divergence",
            message="Device AA:BB:CC:DD:EE:FF not found as MQTT entity",
            severity="WARNING",
        )
        asyncio.run(healer.heal_maintenance_issues([issue]))

        assert len(notifier.sent) == 1
        assert "mqtt_entity_divergence" in notifier.sent[0]["payload"]["field"]
        assert ssh.written_files == {}
        assert ssh.commands_run == []

    def test_mqtt_divergence_no_hitl_gate_called(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=True)
        notifier = FakeNotifier(approve=True)
        healer = self._make_healer(gate=gate, notifier=notifier)
        issue = self._make_issue("mqtt_entity_divergence", severity="WARNING")
        asyncio.run(healer.heal_maintenance_issues([issue]))

        assert len(gate.require_approval_calls) == 0

    # ── DB row count: DBCLNP cleanup at level 4 only ─────────────────────────

    def test_db_issue_level_4_triggers_dbclnp(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate

        gate = FakeAutonomyGate(auto_execute_result=True)
        api = _FakeAPIClient()
        healer = self._make_healer(gate=gate, api_client=api)
        issue = self._make_issue(
            "Plugins_History", message="150k rows", severity="WARNING"
        )
        asyncio.run(healer.heal_maintenance_issues([issue]))

        assert api.trigger_scan_calls == 1
        assert "DBCLNP" in api.trigger_scan_types

    def test_db_issue_level_3_no_dbclnp(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate

        # should_auto_execute returns False → no DBCLNP at level < 4
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        api = _FakeAPIClient()
        healer = self._make_healer(gate=gate, api_client=api)
        issue = self._make_issue("Events", message="200k rows", severity="WARNING")
        asyncio.run(healer.heal_maintenance_issues([issue]))

        assert api.trigger_scan_calls == 0

    def test_no_issues_is_noop(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        gate = FakeAutonomyGate(auto_execute_result=True)
        notifier = FakeNotifier(approve=True)
        ssh = FakeSSHClient()
        api = _FakeAPIClient()
        healer = self._make_healer(
            gate=gate, ssh_client=ssh, notifier=notifier, api_client=api
        )
        asyncio.run(healer.heal_maintenance_issues([]))

        assert ssh.written_files == {}
        assert api.trigger_scan_calls == 0
        assert len(notifier.sent) == 0


# ── HITL Request model ────────────────────────────────────────────────────────────


class TestNetAlertXVersionGuard:
    def test_parse_version_with_v_prefix(self):
        from netalertx.detector import parse_version

        assert parse_version("v26.7.1") == (26, 7, 1)

    def test_parse_version_without_v_prefix(self):
        from netalertx.detector import parse_version

        assert parse_version("26.5.4") == (26, 5, 4)

    def test_parse_version_empty_returns_none(self):
        from netalertx.detector import parse_version

        assert parse_version("") is None

    def test_parse_version_malformed_returns_none(self):
        from netalertx.detector import parse_version

        assert parse_version("not-a-version") is None

    def test_check_min_version_at_minimum_returns_true(self):
        from netalertx.detector import NETALERTX_MIN_VERSION, check_min_version

        version = "v" + ".".join(str(v) for v in NETALERTX_MIN_VERSION)
        assert check_min_version(version) is True

    def test_check_min_version_above_minimum_returns_true(self):
        from netalertx.detector import check_min_version

        assert check_min_version("v26.7.1") is True

    def test_check_min_version_below_minimum_returns_false_and_warns(self, caplog):
        import logging

        from netalertx.detector import check_min_version

        with caplog.at_level(logging.WARNING):
            result = check_min_version("v26.3.0")
        assert result is False
        assert any("netalertx_version_too_old" in r.message for r in caplog.records)

    def test_check_min_version_empty_returns_false_and_warns(self, caplog):
        import logging

        from netalertx.detector import check_min_version

        with caplog.at_level(logging.WARNING):
            result = check_min_version("")
        assert result is False
        assert any("netalertx_version_unknown" in r.message for r in caplog.records)

    def test_check_min_version_unparseable_returns_false_and_warns(self, caplog):
        import logging

        from netalertx.detector import check_min_version

        with caplog.at_level(logging.WARNING):
            result = check_min_version("not-a-version")
        assert result is False
        assert any("netalertx_version_unparseable" in r.message for r in caplog.records)


class TestNetAlertXOneShotDiagnose:
    """Tests for netalertx/one_shot_diagnose.py (item 27)."""

    # ── shared helpers ────────────────────────────────────────────────────────

    class _FakeAPIClient:
        """Minimal API client double for one-shot diagnose tests."""

        def __init__(self, devices=None, about=None, raise_on_about=False):
            self._devices = devices or []
            self._about = about or {"version": "v26.7.1"}
            self._raise_on_about = raise_on_about

        async def get_about(self):
            if self._raise_on_about:
                raise OSError("connection refused")
            return self._about

        async def get_devices(self):
            return self._devices

    class _FakeHealer:
        """Records heal() calls without executing any real actions."""

        def __init__(self):
            self.heal_calls = []

        async def heal(self, diagnostic):
            self.heal_calls.append(diagnostic)

    @staticmethod
    async def _true_probe(_: str) -> bool:
        """Fake mqtt_probe_fn that simulates an active broker."""
        return True

    @staticmethod
    async def _false_probe(_: str) -> bool:
        """Fake mqtt_probe_fn that simulates no MQTT traffic."""
        return False

    def _valid_app_conf(self) -> str:
        return (
            "MQTT_BROKER='localhost'\n"
            "MQTT_PORT=1883\n"
            "HA_URL='http://homeassistant.local:8123'\n"
            "HA_BEARER_TOKEN='token'\n"
            "SCAN_SUBNETS=['192.168.1.0/24   eth0']\n"
            "TIMEZONE='UTC'\n"
            "LOADED_PLUGINS=['MQTT', 'ARPSCAN']\n"
        )

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_api_unreachable_exits_early(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import run_diagnose

        healer = self._FakeHealer()
        llm = FakeLLMClient("{}")

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=FakeSSHClient(),
                api_client=self._FakeAPIClient(raise_on_about=True),
                llm_client=llm,
                healer=healer,
                addon_slug="test_slug",
            )
        )

        assert llm.calls == [], "LLM should not be called when API is unreachable"
        assert (
            healer.heal_calls == []
        ), "Healer should not be called when API is unreachable"

    def _ha_ssh_client(self, app_conf=None, log_output="", mosquitto_state="started"):
        """Build a FakeSSHClient covering all ha_ssh interactions in run_diagnose.

        app_conf=None → valid app.conf; app_conf=<str> → that content;
        omit file_contents entirely to simulate a missing file (see test_app_conf_missing).
        """
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={
                "/addon_configs/test_slug/config/app.conf": (
                    self._valid_app_conf() if app_conf is None else app_conf
                )
            },
            command_results={
                "ha apps info core_mosquitto": (0, f"state: {mosquitto_state}\n", ""),
                "ha apps logs": (0, log_output, ""),
            },
        )

    def test_healthy_system_no_heal(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import run_diagnose

        healer = self._FakeHealer()
        llm = FakeLLMClient("{}")

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(),
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=healer,
                addon_slug="test_slug",
                mqtt_probe_fn=self._true_probe,
            )
        )

        assert llm.calls == [], "LLM should not run when system is healthy"
        assert healer.heal_calls == [], "Healer should not be called on healthy system"

    def test_stale_scan_triggers_diagnosis_and_heal(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="Scan is stale",
            severity="HIGH",
            category="networking",
            recommended_fix="Restart NetAlertX and trigger a new scan.",
            affected_netalertx_version="v26.7.1",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        healer = self._FakeHealer()
        # Devices with a timestamp far in the past → scan_age >> threshold
        stale_devices = [
            {"devLastSeen": "2020-01-01 00:00:00", "devMAC": "AA:BB:CC:DD:EE:FF"}
        ]
        # Gate must allow auto-execution so heal() is called without blocking on HITL
        from utils.autonomy import FakeAutonomyGate

        gate = FakeAutonomyGate(auto_execute_result=True)

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(),
                api_client=self._FakeAPIClient(devices=stale_devices),
                llm_client=llm,
                healer=healer,
                gate=gate,
                addon_slug="test_slug",
            )
        )

        assert len(llm.calls) == 1, "LLM should be called once for diagnosis"
        assert len(healer.heal_calls) == 1, "Healer should be called with diagnostic"
        assert healer.heal_calls[0].category == "networking"

    def test_config_issues_trigger_diagnosis(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="MQTT_BROKER missing from app.conf",
            severity="HIGH",
            category="mqtt",
            recommended_fix="Add MQTT_BROKER to app.conf.",
            affected_netalertx_version="v26.7.1",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        healer = self._FakeHealer()
        # app.conf with only TIMEZONE → MQTT_BROKER and other required keys missing

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(app_conf="TIMEZONE='UTC'\n"),
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=healer,
                addon_slug="test_slug",
            )
        )

        assert len(llm.calls) == 1, "LLM should be called when config issues are found"
        call_prompt = llm.calls[0]["messages"][-1]["content"]
        assert "MQTT_BROKER" in call_prompt, "Config issue should appear in LLM prompt"

    def test_critical_log_line_triggers_triage(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.log_monitor import LogEvaluation
        from netalertx.one_shot_diagnose import run_diagnose

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed: network unreachable",
            confidence_score=0.9,
        )
        llm = FakeLLMClient(ev.model_dump_json())
        healer = self._FakeHealer()
        log_output = "INFO Starting\nERROR scan failed: network unreachable\nINFO Done"

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(log_output=log_output),
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=healer,
                addon_slug="test_slug",
                mqtt_probe_fn=self._true_probe,
            )
        )

        assert len(llm.calls) == 1, "LLM should be called to triage critical log line"

    def test_mosquitto_not_running_triggers_diagnosis(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="Mosquitto not running",
            severity="HIGH",
            category="mqtt",
            recommended_fix="Run 'ha apps start core_mosquitto'.",
            affected_netalertx_version="unknown",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        healer = self._FakeHealer()

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(mosquitto_state="stopped"),
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=healer,
                addon_slug="test_slug",
            )
        )

        assert len(llm.calls) == 1, "LLM should be called when Mosquitto is not running"
        call_prompt = llm.calls[0]["messages"][-1]["content"]
        assert (
            "core_mosquitto" in call_prompt
        ), "Mosquitto issue should appear in prompt"

    def test_app_conf_missing_triggers_diagnosis(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="app.conf not found",
            severity="HIGH",
            category="mqtt",
            recommended_fix="Re-run netalertx-setup.",
            affected_netalertx_version="unknown",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        healer = self._FakeHealer()
        # No file_contents → FakeSSHClient.read_file raises FileNotFoundError
        # → _fetch_app_conf returns None → ConfigIssue generated
        ha_ssh = FakeSSHClient(
            command_results={
                "ha apps info core_mosquitto": (0, "state: started\n", ""),
                "ha apps logs": (0, "", ""),
            }
        )

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=ha_ssh,
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=healer,
                addon_slug="test_slug",
            )
        )

        assert len(llm.calls) == 1, "LLM should be called when app.conf is missing"
        call_prompt = llm.calls[0]["messages"][-1]["content"]
        assert "app.conf" in call_prompt, "Missing app.conf should appear in LLM prompt"

    def test_check_mosquitto_running_started(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _check_mosquitto_running

        ha_ssh = FakeSSHClient(
            command_results={"ha apps info core_mosquitto": (0, "state: started\n", "")}
        )
        result = asyncio.run(_check_mosquitto_running(ha_ssh))
        assert result is True

    def test_check_mosquitto_running_stopped(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _check_mosquitto_running

        ha_ssh = FakeSSHClient(
            command_results={"ha apps info core_mosquitto": (0, "state: stopped\n", "")}
        )
        result = asyncio.run(_check_mosquitto_running(ha_ssh))
        assert result is False

    def test_check_mosquitto_running_no_output(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _check_mosquitto_running

        ha_ssh = FakeSSHClient()  # no command results → empty stdout
        result = asyncio.run(_check_mosquitto_running(ha_ssh))
        assert result is False

    def test_fetch_log_snapshot_empty_slug_returns_empty(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _fetch_log_snapshot

        result = asyncio.run(_fetch_log_snapshot(FakeSSHClient(), ""))
        assert result == []

    def test_fetch_log_snapshot_returns_last_100_lines(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _fetch_log_snapshot

        lines = "\n".join(f"line {i}" for i in range(200))
        ha_ssh = FakeSSHClient(command_results={"ha apps logs": (0, lines, "")})
        result = asyncio.run(_fetch_log_snapshot(ha_ssh, "test_slug"))
        assert len(result) == 100
        assert result[0] == "line 100"

    def test_fetch_app_conf_empty_slug_returns_none(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _fetch_app_conf

        result = asyncio.run(_fetch_app_conf(FakeSSHClient(), ""))
        assert result is None

    def test_fetch_app_conf_returns_file_contents(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _fetch_app_conf

        ha_ssh = FakeSSHClient(
            file_contents={"/addon_configs/s/config/app.conf": "KEY=value\n"}
        )
        result = asyncio.run(_fetch_app_conf(ha_ssh, "s"))
        assert result == "KEY=value\n"

    def test_fetch_app_conf_missing_file_returns_none(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.one_shot_diagnose import _fetch_app_conf

        result = asyncio.run(_fetch_app_conf(FakeSSHClient(), "test_slug"))
        assert result is None

    def test_empty_addon_slug_generates_config_issue(self):
        """Empty addon_slug triggers the 'if not _slug:' ConfigIssue branch."""
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="addon_slug not configured",
            severity="HIGH",
            category="config",
            recommended_fix="Set netalertx.addon_slug in config.yaml.",
            affected_netalertx_version="unknown",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        ha_ssh = FakeSSHClient(
            command_results={"ha apps info core_mosquitto": (0, "state: started\n", "")}
        )

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=ha_ssh,
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=self._FakeHealer(),
                addon_slug="",
            )
        )

        assert len(llm.calls) == 1, "LLM should be called when addon_slug is empty"
        call_prompt = llm.calls[0]["messages"][-1]["content"]
        assert "addon_slug" in call_prompt

    def test_auto_execute_constructs_healer_when_none(self):
        """healer=None + should_auto_execute → constructs NetAlertXHealer internally."""
        import asyncio
        from unittest.mock import patch

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose
        from utils.autonomy import FakeAutonomyGate

        diag = NetAlertXDiagnostic(
            issue="Scan is stale",
            severity="HIGH",
            category="networking",
            recommended_fix="Restart NetAlertX.",
            affected_netalertx_version="v26.7.1",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        gate = FakeAutonomyGate(auto_execute_result=True)
        stale_devices = [
            {"devLastSeen": "2020-01-01 00:00:00", "devMAC": "AA:BB:CC:DD:EE:FF"}
        ]
        fake_healer = self._FakeHealer()

        with patch("netalertx.healer.NetAlertXHealer", return_value=fake_healer):
            asyncio.run(
                run_diagnose(
                    ssh_client=FakeSSHClient(),
                    ha_ssh_client=self._ha_ssh_client(),
                    api_client=self._FakeAPIClient(devices=stale_devices),
                    llm_client=llm,
                    healer=None,
                    gate=gate,
                    addon_slug="test_slug",
                )
            )

        assert (
            len(fake_healer.heal_calls) == 1
        ), "Internally-constructed healer should be called"

    def test_mqtt_probe_fn_called_and_true_sets_mqtt_active(self):
        """mqtt_probe_fn returning True → mqtt_active=True in LLM diagnosis prompt."""
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="Scan is stale",
            severity="HIGH",
            category="networking",
            recommended_fix="Restart scan.",
            affected_netalertx_version="v26.7.1",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        stale_devices = [
            {"devLastSeen": "2020-01-01 00:00:00", "devMAC": "AA:BB:CC:DD:EE:FF"}
        ]
        probe_calls: list[str] = []

        async def fake_probe(host: str) -> bool:
            probe_calls.append(host)
            return True

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(),
                api_client=self._FakeAPIClient(devices=stale_devices),
                llm_client=llm,
                healer=self._FakeHealer(),
                addon_slug="test_slug",
                mqtt_probe_fn=fake_probe,
            )
        )

        assert len(probe_calls) == 1, "mqtt_probe_fn must be called exactly once"
        prompt = llm.calls[0]["messages"][-1]["content"]
        assert "MQTT active: True" in prompt, "Probe True must propagate to LLM prompt"

    def test_mqtt_probe_fn_false_sets_mqtt_inactive(self):
        """mqtt_probe_fn returning False → mqtt_active=False in LLM diagnosis prompt."""
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="Scan is stale",
            severity="HIGH",
            category="networking",
            recommended_fix="Restart scan.",
            affected_netalertx_version="v26.7.1",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        stale_devices = [
            {"devLastSeen": "2020-01-01 00:00:00", "devMAC": "AA:BB:CC:DD:EE:FF"}
        ]

        async def false_probe(_: str) -> bool:
            return False

        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(),
                api_client=self._FakeAPIClient(devices=stale_devices),
                llm_client=llm,
                healer=self._FakeHealer(),
                addon_slug="test_slug",
                mqtt_probe_fn=false_probe,
            )
        )

        prompt = llm.calls[0]["messages"][-1]["content"]
        assert (
            "MQTT active: False" in prompt
        ), "Probe False must propagate to LLM prompt"

    def test_mqtt_inactive_with_mosquitto_running_triggers_config_issue(self):
        """mqtt_active=False + mosquitto running → ConfigIssue added → LLM called."""
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.diagnosis import NetAlertXDiagnostic
        from netalertx.one_shot_diagnose import run_diagnose

        diag = NetAlertXDiagnostic(
            issue="No MQTT traffic detected",
            severity="MEDIUM",
            category="mqtt",
            recommended_fix="Check MQTT_BROKER in app.conf.",
            affected_netalertx_version="unknown",
        )
        llm = FakeLLMClient(diag.model_dump_json())

        async def false_probe(_: str) -> bool:
            return False

        # Healthy devices (no stale scan) — LLM call must come from mqtt_traffic issue alone
        asyncio.run(
            run_diagnose(
                ssh_client=FakeSSHClient(),
                ha_ssh_client=self._ha_ssh_client(mosquitto_state="started"),
                api_client=self._FakeAPIClient(devices=[]),
                llm_client=llm,
                healer=self._FakeHealer(),
                addon_slug="test_slug",
                mqtt_probe_fn=false_probe,
            )
        )

        assert (
            len(llm.calls) == 1
        ), "LLM should be called when MQTT is silent but broker is up"
        prompt = llm.calls[0]["messages"][-1]["content"]
        assert (
            "mqtt_traffic" in prompt
        ), "mqtt_traffic ConfigIssue must appear in LLM prompt"
