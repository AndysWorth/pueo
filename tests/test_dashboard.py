#!/usr/bin/env python3
"""Dashboard, installer diagnostics, and LLM trace/evidence tests."""

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


def _make_async_return(value):
    """Return an async callable that resolves to ``value``."""

    async def _inner(*args, **kwargs):
        return value

    return _inner


def _make_installer_db(tmp_path, monkeypatch):
    """Create and migrate a test SQLite DB, patch DB_PATH in installer module."""
    from agents import ha_agent_advanced
    import netalertx.installer as inst

    db = tmp_path / "installer_test.db"
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
    ha_agent_advanced.init_local_database()
    monkeypatch.setattr(inst, "DB_PATH", str(db))
    return str(db)


class TestHITLRequestModel:
    def test_valid_construction(self):
        from web.dashboard import HITLRequest

        r = HITLRequest(
            notification_id="abc",
            subject="Test",
            body="body text",
            payload={"key": "value"},
            sent_at=1000,
            status="PENDING",
            elapsed_seconds=42,
        )
        assert r.notification_id == "abc"
        assert r.status == "PENDING"

    def test_invalid_status_raises(self):
        from pydantic import ValidationError
        from web.dashboard import HITLRequest

        with pytest.raises(ValidationError):
            HITLRequest(
                notification_id="x",
                subject="s",
                body="b",
                payload={},
                sent_at=0,
                status="UNKNOWN",
                elapsed_seconds=0,
            )

    def test_missing_required_field_raises(self):
        from pydantic import ValidationError
        from web.dashboard import HITLRequest

        with pytest.raises(ValidationError):
            HITLRequest(subject="s", body="b", payload={}, sent_at=0, status="PENDING", elapsed_seconds=0)  # type: ignore[call-arg]

    def test_json_roundtrip(self):
        from web.dashboard import HITLRequest

        r = HITLRequest(
            notification_id="id1",
            subject="Test",
            body="b",
            payload={"a": 1},
            sent_at=999,
            status="APPROVED",
            elapsed_seconds=10,
        )
        restored = HITLRequest.model_validate_json(r.model_dump_json())
        assert restored.notification_id == "id1"
        assert restored.status == "APPROVED"


# ── _load_requests ────────────────────────────────────────────────────────────────


class TestLoadHITLRequests:
    def _write_request(self, tmp_path: Path, nid: str, subject: str = "s") -> None:
        import json as _json
        import time as _time

        (tmp_path / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": subject,
                    "body": "body",
                    "payload": {},
                    "sent_at": int(_time.time()) - 60,
                }
            )
        )

    def test_pending_request_has_status_pending(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "aaa")
        results = _load_requests(tmp_path)
        assert len(results) == 1
        assert results[0].status == "PENDING"

    def test_approved_signal_yields_approved_status(self, tmp_path):
        from web.dashboard import _status

        self._write_request(tmp_path, "bbb")
        (tmp_path / "bbb.approved").touch()
        assert _status("bbb", tmp_path) == "APPROVED"

    def test_approved_card_excluded_from_load_requests(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "bbb")
        (tmp_path / "bbb.approved").touch()
        assert _load_requests(tmp_path) == []

    def test_rejected_signal_yields_rejected_status(self, tmp_path):
        from web.dashboard import _status

        self._write_request(tmp_path, "ccc")
        (tmp_path / "ccc.rejected").touch()
        assert _status("ccc", tmp_path) == "REJECTED"

    def test_rejected_card_excluded_from_load_requests(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "ccc")
        (tmp_path / "ccc.rejected").touch()
        assert _load_requests(tmp_path) == []

    def test_empty_directory_returns_empty_list(self, tmp_path):
        from web.dashboard import _load_requests

        assert _load_requests(tmp_path) == []

    def test_orphan_signal_files_are_ignored(self, tmp_path):
        from web.dashboard import _load_requests

        (tmp_path / "orphan.approved").touch()
        (tmp_path / "orphan.rejected").touch()
        assert _load_requests(tmp_path) == []

    def test_malformed_json_is_skipped(self, tmp_path):
        from web.dashboard import _load_requests

        (tmp_path / "bad.json").write_text("not json {{{")
        self._write_request(tmp_path, "good")
        results = _load_requests(tmp_path)
        assert len(results) == 1
        assert results[0].notification_id == "good"

    def test_resolved_cards_excluded_from_queue(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "p1")
        self._write_request(tmp_path, "r1")
        (tmp_path / "r1.approved").touch()
        results = _load_requests(tmp_path)
        assert len(results) == 1
        assert results[0].notification_id == "p1"
        assert results[0].status == "PENDING"


# ── main.py dashboard mode ────────────────────────────────────────────────────────


class TestMainDashboardMode:
    def test_dashboard_mode_in_help_output(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert "dashboard" in result.stdout


# ── Dashboard HTTP routes ─────────────────────────────────────────────────────────


class TestDashboardRoutes:
    """Tests for the FastAPI route handlers using TestClient."""

    def _write_request(self, watch_dir: Path, nid: str) -> None:
        import json as _json
        import time as _time

        (watch_dir / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Test subject",
                    "body": "Test body",
                    "payload": {"severity": "HIGH"},
                    "sent_at": int(_time.time()) - 30,
                }
            )
        )

    def test_index_returns_200(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/")
        assert response.status_code == 200
        assert "Pueo" in response.text

    def test_index_shows_pending_request(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "req1")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/queue")
        assert "PENDING" in response.text
        assert "Test subject" in response.text

    def test_approve_creates_signal_file(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "req2")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/approve/req2", follow_redirects=False)
        assert (tmp_path / "req2.approved").exists()

    def test_reject_creates_signal_file(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "req3")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/reject/req3", follow_redirects=False)
        assert (tmp_path / "req3.rejected").exists()

    def test_approve_already_resolved_is_noop(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "req4")
        (tmp_path / "req4.rejected").touch()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/approve/req4", follow_redirects=False)
        assert not (tmp_path / "req4.approved").exists()

    def test_approve_unknown_nid_is_noop(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/approve/no-such-id", follow_redirects=False)
        assert response.status_code == 303

    def test_approve_with_pending_fix_yaml_calls_execute_queued_fix(
        self, tmp_path, monkeypatch
    ):
        """Approving a HITL card that carries pending_fix_yaml runs _execute_queued_fix."""
        import json as _json
        import time as _time
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))

        nid = "fix-req-1"
        (tmp_path / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Pueo HITL: apply_fix",
                    "body": "Proposed fix:\nhomeassistant:\n  name: Home\n",
                    "payload": {
                        "severity": "HIGH",
                        "pending_fix_yaml": "homeassistant:\n  name: Home\n",
                        "pending_fix_description": "add missing name key",
                    },
                    "sent_at": int(_time.time()) - 60,
                }
            )
        )

        executed: list[dict] = []

        async def _fake_execute_queued_fix(nid_, yaml_, desc_, data_, jpath_, wdir_):
            executed.append({"nid": nid_, "yaml": yaml_, "desc": desc_})
            (wdir_ / f"{nid_}.approved").touch()

        monkeypatch.setattr(dashboard, "_execute_queued_fix", _fake_execute_queued_fix)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(f"/approve/{nid}", follow_redirects=False)
        assert response.status_code == 303
        assert len(executed) == 1
        assert executed[0]["yaml"] == "homeassistant:\n  name: Home\n"
        assert executed[0]["desc"] == "add missing name key"
        assert (tmp_path / f"{nid}.approved").exists()

    def test_approve_with_pending_fix_yaml_sandbox_failure_rejects(
        self, tmp_path, monkeypatch
    ):
        """If _execute_queued_fix writes .rejected, the notification is rejected."""
        import json as _json
        import time as _time
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))

        nid = "fix-req-2"
        (tmp_path / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Pueo HITL: apply_fix",
                    "body": "fix",
                    "payload": {
                        "severity": "HIGH",
                        "pending_fix_yaml": "bad yaml",
                        "pending_fix_description": "broken fix",
                    },
                    "sent_at": int(_time.time()) - 60,
                }
            )
        )

        async def _fake_execute_failed(nid_, yaml_, desc_, data_, jpath_, wdir_):
            (wdir_ / f"{nid_}.rejected").touch()

        monkeypatch.setattr(dashboard, "_execute_queued_fix", _fake_execute_failed)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post(f"/approve/{nid}", follow_redirects=False)
        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.approved").exists()

    # -- _execute_queued_fix internals -------------------------------------------

    def test_execute_queued_fix_success_writes_approved(self, tmp_path, monkeypatch):
        """Success path: backup → sandbox passes → commit → .approved written."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_agent_sandbox_engine
        from agents import ha_agent_advanced

        nid = "fix-internal-1"
        json_path = tmp_path / f"{nid}.json"
        data: dict = {"fix": "pending"}
        json_path.write_text(_json.dumps(data))

        monkeypatch.setattr(
            ha_agent_sandbox_engine,
            "execute_remote_backup",
            _make_async_return("slug1"),
        )
        monkeypatch.setattr(
            ha_agent_sandbox_engine, "record_backup_slug", lambda *a: None
        )
        monkeypatch.setattr(
            ha_agent_advanced, "offload_backup_to_local", _make_async_return(None)
        )
        monkeypatch.setattr(
            ha_agent_advanced, "enforce_ha_retention", _make_async_return(None)
        )
        monkeypatch.setattr(ha_agent_advanced, "purge_local_backups", lambda: None)
        monkeypatch.setattr(
            ha_agent_sandbox_engine,
            "deploy_and_test_in_sandbox",
            _make_async_return(True),
        )
        monkeypatch.setattr(
            ha_agent_sandbox_engine, "commit_atomic_swap", _make_async_return(None)
        )

        import asyncio
        import utils.ha.ssh_client as ssh_mod
        from utils.ha.ssh_client import FakeSSHClient

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())

        asyncio.run(
            dashboard._execute_queued_fix(
                nid,
                "homeassistant:\n  name: Home\n",
                "test fix",
                data,
                json_path,
                tmp_path,
            )
        )
        assert (tmp_path / f"{nid}.approved").exists()
        saved = _json.loads(json_path.read_text())
        assert saved["fix_applied"] is True
        assert saved["fix_backup_slug"] == "slug1"

    def test_execute_queued_fix_sandbox_failure_writes_rejected(
        self, tmp_path, monkeypatch
    ):
        """When sandbox test fails, .rejected is written and no commit happens."""
        import json as _json
        import asyncio
        import web.dashboard as dashboard
        from agents import ha_agent_sandbox_engine
        from agents import ha_agent_advanced

        nid = "fix-internal-2"
        json_path = tmp_path / f"{nid}.json"
        data: dict = {}
        json_path.write_text(_json.dumps(data))

        monkeypatch.setattr(
            ha_agent_sandbox_engine,
            "execute_remote_backup",
            _make_async_return("slug2"),
        )
        monkeypatch.setattr(
            ha_agent_sandbox_engine, "record_backup_slug", lambda *a: None
        )
        monkeypatch.setattr(
            ha_agent_advanced, "offload_backup_to_local", _make_async_return(None)
        )
        monkeypatch.setattr(
            ha_agent_advanced, "enforce_ha_retention", _make_async_return(None)
        )
        monkeypatch.setattr(ha_agent_advanced, "purge_local_backups", lambda: None)
        monkeypatch.setattr(
            ha_agent_sandbox_engine,
            "deploy_and_test_in_sandbox",
            _make_async_return(False),
        )
        import utils.ha.ssh_client as ssh_mod
        from utils.ha.ssh_client import FakeSSHClient

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())

        asyncio.run(
            dashboard._execute_queued_fix(
                nid, "bad yaml", "desc", data, json_path, tmp_path
            )
        )
        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.approved").exists()
        saved = _json.loads(json_path.read_text())
        assert "fix_error" in saved

    def test_execute_queued_fix_exception_writes_rejected(self, tmp_path, monkeypatch):
        """An exception during backup causes .rejected to be written with fix_error."""
        import json as _json
        import asyncio
        import web.dashboard as dashboard
        from agents import ha_agent_sandbox_engine

        nid = "fix-internal-3"
        json_path = tmp_path / f"{nid}.json"
        data: dict = {}
        json_path.write_text(_json.dumps(data))

        async def _fail(*args, **kwargs):
            raise RuntimeError("backup exploded")

        monkeypatch.setattr(ha_agent_sandbox_engine, "execute_remote_backup", _fail)
        import utils.ha.ssh_client as ssh_mod
        from utils.ha.ssh_client import FakeSSHClient

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())

        asyncio.run(
            dashboard._execute_queued_fix(
                nid, "yaml", "desc", data, json_path, tmp_path
            )
        )
        assert (tmp_path / f"{nid}.rejected").exists()
        saved = _json.loads(json_path.read_text())
        assert "backup exploded" in saved["fix_error"]

    def test_load_backup_inventory_returns_empty_on_db_error(self, monkeypatch):
        """_load_backup_inventory returns [] when the database is missing."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "DB_PATH", "/nonexistent/path/db.sqlite")
        result = dashboard._load_backup_inventory()
        assert result == []

    def test_run_dashboard_calls_uvicorn(self, monkeypatch):
        import web.dashboard as dashboard

        calls: list[dict] = []

        def fake_uvicorn_run(app, **kwargs):
            calls.append(kwargs)

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
        monkeypatch.setattr(dashboard, "DASHBOARD_PORT", 9999)
        dashboard.run_dashboard()
        assert calls[0]["port"] == 9999


# ── netalertx/installer_diagnostics.py ───────────────────────────────────────


class TestInstallerDiagnostics:
    # ── schema tests ─────────────────────────────────────────────────────────

    def test_installer_diagnostic_valid_construction(self):
        from netalertx.installer_diagnostics import InstallerDiagnostic

        d = InstallerDiagnostic(
            primary_hypothesis="Port 1883 is in use",
            confidence=0.85,
            supporting_evidence=["state: stopped"],
            alternative_hypotheses=["TLS cert mismatch"],
            recommended_action="Stop conflicting process",
            can_auto_fix=True,
            auto_fix_command="ha apps restart core_mosquitto",
            verification_command="ha apps info core_mosquitto",
        )
        assert d.confidence == 0.85
        assert d.can_auto_fix is True
        assert d.auto_fix_command == "ha apps restart core_mosquitto"

    def test_installer_diagnostic_missing_required_field_raises(self):
        from pydantic import ValidationError
        from netalertx.installer_diagnostics import InstallerDiagnostic

        with pytest.raises(ValidationError):
            InstallerDiagnostic(
                confidence=0.5,
                supporting_evidence=[],
                alternative_hypotheses=[],
                recommended_action="do something",
                can_auto_fix=False,
            )

    def test_installer_diagnostic_json_round_trip(self):
        from netalertx.installer_diagnostics import InstallerDiagnostic

        d = InstallerDiagnostic(
            primary_hypothesis="Slug not found",
            confidence=0.4,
            supporting_evidence=[],
            alternative_hypotheses=["Supervisor not indexed yet"],
            recommended_action="Run ha supervisor reload",
            can_auto_fix=True,
            auto_fix_command="ha supervisor reload",
            verification_command="ha store addons",
        )
        restored = InstallerDiagnostic.model_validate_json(d.model_dump_json())
        assert restored.primary_hypothesis == d.primary_hypothesis
        assert restored.auto_fix_command == d.auto_fix_command

    # ── evidence gatherer tests ───────────────────────────────────────────────

    def test_gather_mosquitto_evidence_returns_expected_keys(self):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from netalertx.installer_diagnostics import gather_mosquitto_evidence

        ssh = FakeSSHClient(
            command_results={
                "ha apps info core_mosquitto": (0, "state: error\n", ""),
                "ha apps logs core_mosquitto -n 50": (
                    0,
                    "Error: Port '1883' is already in use\n",
                    "",
                ),
                "ss -tlnp | grep 1883": (0, "tcp LISTEN 0 128 *:1883\n", ""),
                "ha supervisor info": (0, "version: 2024.1\n", ""),
            }
        )
        evidence = asyncio.run(gather_mosquitto_evidence(ssh))
        assert set(evidence.keys()) == {
            "addon_info",
            "addon_logs",
            "port_1883",
            "supervisor_info",
        }
        assert "state: error" in evidence["addon_info"]
        assert "Port '1883'" in evidence["addon_logs"]

    def test_gather_addon_install_evidence_passes_slug(self):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from netalertx.installer_diagnostics import gather_addon_install_evidence

        ssh = FakeSSHClient(
            command_results={
                "ha apps info netalertx_fa": (0, "state: installing\n", ""),
                "ha supervisor info": (0, "version: 2024.1\n", ""),
            }
        )
        evidence = asyncio.run(gather_addon_install_evidence(ssh, "netalertx_fa"))
        assert "addon_info" in evidence
        assert "supervisor_info" in evidence
        assert "ha apps info netalertx_fa" in ssh.commands_run

    def test_gather_addon_start_evidence_passes_slug(self):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from netalertx.installer_diagnostics import gather_addon_start_evidence

        ssh = FakeSSHClient(
            command_results={
                "ha apps info netalertx_fa": (0, "state: error\n", ""),
                "ha apps logs netalertx_fa -n 50": (
                    0,
                    "NET_RAW capability missing\n",
                    "",
                ),
            }
        )
        evidence = asyncio.run(gather_addon_start_evidence(ssh, "netalertx_fa"))
        assert "addon_info" in evidence
        assert "addon_logs" in evidence
        assert "NET_RAW" in evidence["addon_logs"]

    # ── diagnostic flow tests ─────────────────────────────────────────────────

    def test_diagnose_installer_failure_mosquitto_start_calls_llm(self):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient
        from netalertx.installer_diagnostics import (
            InstallerDiagnostic,
            diagnose_installer_failure,
        )

        diag = InstallerDiagnostic(
            primary_hypothesis="Port 1883 in use",
            confidence=0.9,
            supporting_evidence=["Port '1883' is already in use"],
            alternative_hypotheses=[],
            recommended_action="Stop the other MQTT broker",
            can_auto_fix=False,
        )
        llm = FakeLLMClient(diag.model_dump_json())
        ssh = FakeSSHClient()

        result, _trace, _evidence = asyncio.run(
            diagnose_installer_failure("mosquitto_start", ssh, llm)
        )
        assert isinstance(result, InstallerDiagnostic)
        assert result.primary_hypothesis == "Port 1883 in use"
        assert len(llm.calls) == 1

    def test_diagnose_installer_failure_addon_install_passes_slug(self):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient
        from netalertx.installer_diagnostics import (
            InstallerDiagnostic,
            diagnose_installer_failure,
        )

        diag = InstallerDiagnostic(
            primary_hypothesis="Supervisor not yet indexed the repo",
            confidence=0.7,
            supporting_evidence=[],
            alternative_hypotheses=["Network issue"],
            recommended_action="Run ha supervisor reload",
            can_auto_fix=True,
            auto_fix_command="ha supervisor reload",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        ssh = FakeSSHClient()

        result, _trace, _evidence = asyncio.run(
            diagnose_installer_failure("addon_install", ssh, llm, "netalertx_fa")
        )
        assert result.can_auto_fix is True
        assert "ha apps info netalertx_fa" in ssh.commands_run

    def test_diagnose_installer_failure_uses_model_factory(self, monkeypatch):
        import asyncio
        import netalertx.installer_diagnostics as inst_diag_mod
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient
        from netalertx.installer_diagnostics import (
            InstallerDiagnostic,
            diagnose_installer_failure,
        )

        diag = InstallerDiagnostic(
            primary_hypothesis="Port in use",
            confidence=0.9,
            supporting_evidence=[],
            alternative_hypotheses=[],
            recommended_action="Stop broker",
            can_auto_fix=False,
        )
        llm = FakeLLMClient(diag.model_dump_json())
        calls = []
        monkeypatch.setattr(
            inst_diag_mod,
            "_default_model_for_provider",
            lambda: calls.append(True) or "sentinel-model",
        )
        asyncio.run(diagnose_installer_failure("mosquitto_start", FakeSSHClient(), llm))
        assert calls, "_default_model_for_provider was not called"
        assert llm.calls[0]["model"] == "sentinel-model"

    # ── HITL formatter tests ──────────────────────────────────────────────────

    def test_format_diagnostic_for_hitl_renders_all_fields(self):
        from netalertx.installer_diagnostics import (
            InstallerDiagnostic,
            format_diagnostic_for_hitl,
        )

        d = InstallerDiagnostic(
            primary_hypothesis="Port 1883 is in use by another process",
            confidence=0.82,
            supporting_evidence=["Supervisor log: Port '1883' is already in use"],
            alternative_hypotheses=["SSL cert mismatch"],
            recommended_action="Stop the conflicting process",
            can_auto_fix=True,
            auto_fix_command="ha apps restart core_mosquitto",
            verification_command="ha apps info core_mosquitto",
        )
        text = format_diagnostic_for_hitl(d)
        assert "Port 1883 is in use" in text
        assert "82%" in text
        assert "Port '1883' is already in use" in text
        assert "SSL cert mismatch" in text
        assert "Stop the conflicting process" in text
        assert "ha apps restart core_mosquitto" in text
        assert "ha apps info core_mosquitto" in text
        assert "Pueo can attempt this fix automatically" in text

    # ── installer integration tests ───────────────────────────────────────────

    def _make_diag(self, **kwargs):
        from netalertx.installer_diagnostics import InstallerDiagnostic

        defaults = dict(
            primary_hypothesis="Test hypothesis",
            confidence=0.8,
            supporting_evidence=[],
            alternative_hypotheses=[],
            recommended_action="Do something",
            can_auto_fix=False,
        )
        defaults.update(kwargs)
        return InstallerDiagnostic(**defaults)

    def test_step2_failure_calls_llm_and_enriches_hitl_body(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from netalertx.installer import run_steps_1_to_4

        async def poll_false(*a, **k):
            return False

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_false)

        diag = self._make_diag(primary_hypothesis="Port 1883 conflict", confidence=0.9)
        llm = FakeLLMClient(diag.model_dump_json())
        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: stopped", ""),
                "ha apps start core_mosquitto": (0, "", ""),
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=False)

        db = _make_installer_db(tmp_path, monkeypatch)
        asyncio.run(
            run_steps_1_to_4(
                ssh, gate, FakeNotifier(approve=False), db_path=db, llm_client=llm
            )
        )
        assert len(llm.calls) == 1
        assert any(
            "Port 1883 conflict" in c.get("body", "")
            for c in gate.require_approval_calls
        )

    def test_step2_auto_fix_success_advances_state(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        poll_call_count = [0]

        async def poll_conditional(*a, **k):
            poll_call_count[0] += 1
            return poll_call_count[0] > 1

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_conditional)

        diag = self._make_diag(
            primary_hypothesis="Port conflict",
            can_auto_fix=True,
            auto_fix_command="ha apps restart core_mosquitto",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: stopped", ""),
                "ha apps start core_mosquitto": (0, "", ""),
                "ha apps restart core_mosquitto": (0, "", ""),
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)

        db = _make_installer_db(tmp_path, monkeypatch)
        state = asyncio.run(
            run_steps_1_to_4(
                ssh, gate, FakeNotifier(approve=True), db_path=db, llm_client=llm
            )
        )
        assert state == "MQTT_RUNNING"
        assert "ha apps restart core_mosquitto" in ssh.commands_run

    def test_step2_auto_fix_nonzero_ec_returns_false(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from netalertx.installer import run_steps_1_to_4

        async def poll_false(*a, **k):
            return False

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_false)

        diag = self._make_diag(
            primary_hypothesis="Port conflict",
            can_auto_fix=True,
            auto_fix_command="ha apps restart core_mosquitto",
        )
        llm = FakeLLMClient(diag.model_dump_json())
        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha apps info core_mosquitto": (0, "state: stopped", ""),
                "ha apps start core_mosquitto": (0, "", ""),
                "ha apps restart core_mosquitto": (1, "", "error"),
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)

        db = _make_installer_db(tmp_path, monkeypatch)
        state = asyncio.run(
            run_steps_1_to_4(
                ssh, gate, FakeNotifier(approve=True), db_path=db, llm_client=llm
            )
        )
        assert state == "MQTT_INSTALLED"


# ===========================================================================
# TestLLMTrace  (item 23)
# ===========================================================================
class TestLLMTrace:
    def test_construction(self):
        from utils.llm_trace import LLMTrace

        trace = LLMTrace(
            model="qwen2.5-coder:7b",
            system_prompt="You are an assistant.",
            user_prompt="Analyze this config.",
            raw_response='{"is_valid": true}',
        )
        assert trace.model == "qwen2.5-coder:7b"
        assert trace.system_prompt == "You are an assistant."
        assert trace.user_prompt == "Analyze this config."
        assert trace.raw_response == '{"is_valid": true}'
        assert isinstance(trace.timestamp, int)

    def test_as_dict_keys(self):
        from utils.llm_trace import LLMTrace

        trace = LLMTrace(
            model="m", system_prompt="sp", user_prompt="up", raw_response="r"
        )
        d = trace.as_dict()
        assert set(d.keys()) == {
            "model",
            "system_prompt",
            "user_prompt",
            "raw_response",
            "timestamp",
        }

    def test_episode_id_included_in_as_dict_when_set(self):
        from utils.llm_trace import LLMTrace

        trace = LLMTrace(
            model="m",
            system_prompt="sp",
            user_prompt="up",
            raw_response="r",
            episode_id="abc-123",
        )
        d = trace.as_dict()
        assert d["episode_id"] == "abc-123"

    def test_episode_id_absent_from_as_dict_when_none(self):
        from utils.llm_trace import LLMTrace

        trace = LLMTrace(
            model="m", system_prompt="sp", user_prompt="up", raw_response="r"
        )
        d = trace.as_dict()
        assert "episode_id" not in d

    def test_system_prompt_truncated_in_as_dict(self):
        from utils.llm_trace import LLMTrace

        long_prompt = "x" * 5000
        trace = LLMTrace(
            model="m", system_prompt=long_prompt, user_prompt="u", raw_response="r"
        )
        d = trace.as_dict()
        assert len(d["system_prompt"]) <= 4000
        assert d["system_prompt"].endswith("\n...[truncated]...")

    def test_user_prompt_truncated_in_as_dict(self):
        from utils.llm_trace import LLMTrace

        long_prompt = "y" * 5000
        trace = LLMTrace(
            model="m", system_prompt="s", user_prompt=long_prompt, raw_response="r"
        )
        d = trace.as_dict()
        assert len(d["user_prompt"]) <= 4000
        assert d["user_prompt"].endswith("\n...[truncated]...")

    def test_short_prompts_not_truncated(self):
        from utils.llm_trace import LLMTrace

        trace = LLMTrace(
            model="m", system_prompt="short", user_prompt="also short", raw_response="r"
        )
        d = trace.as_dict()
        assert d["system_prompt"] == "short"
        assert d["user_prompt"] == "also short"

    def test_analyze_config_returns_trace(self):
        import asyncio

        from utils.llm.ollama_client import FakeLLMClient
        from agents.ha_agent_core import DiagnosticsReport, analyze_config_locally

        report = DiagnosticsReport(
            is_valid=True,
            severity="LOW",
            identified_issues=[],
            recommended_fix_yaml=None,
        )
        llm = FakeLLMClient(report.model_dump_json())
        _result, trace = asyncio.run(
            analyze_config_locally("ha_version: 2026.7.3", llm_client=llm)
        )
        assert trace.model != ""
        assert trace.system_prompt != ""
        assert trace.raw_response != ""

    def test_analyze_log_returns_trace(self):
        import asyncio

        from utils.llm.ollama_client import FakeLLMClient
        from agents.ha_log_monitor import LogEvaluation, analyze_log_line_with_ai

        ev = LogEvaluation(
            is_actionable=False, root_cause_summary="benign", confidence_score=0.1
        )
        llm = FakeLLMClient(ev.model_dump_json())
        _result, trace = asyncio.run(
            analyze_log_line_with_ai(["INFO heartbeat"], llm_client=llm)
        )
        assert trace.model != ""
        assert "log lines" in trace.user_prompt

    def test_diagnose_installer_returns_evidence_dict(self):
        import asyncio

        from utils.llm.ollama_client import FakeLLMClient
        from utils.ha.ssh_client import FakeSSHClient
        from netalertx.installer_diagnostics import (
            InstallerDiagnostic,
            diagnose_installer_failure,
        )

        diag = InstallerDiagnostic(
            primary_hypothesis="Port in use",
            confidence=0.9,
            supporting_evidence=[],
            alternative_hypotheses=[],
            recommended_action="Stop conflicting service",
            can_auto_fix=False,
        )
        llm = FakeLLMClient(diag.model_dump_json())
        ssh = FakeSSHClient()
        _result, _trace, evidence = asyncio.run(
            diagnose_installer_failure("mosquitto_start", ssh, llm)
        )
        assert isinstance(evidence, dict)
        assert "addon_info" in evidence

    def test_hitl_payload_contains_notification_id(self):
        import asyncio

        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from agents import ha_agent_sandbox_engine

        _orig = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
        _fix = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"
        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "apply_fix",
                                "arguments": {
                                    "yaml_content": _fix,
                                    "description": "Fix server_port from 8123 to 8124",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _orig},
            command_results={"ha core check": (0, "OK", "")},
        )
        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, notifier=notifier, gate=gate
            )
        )
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert "notification_id" in payload
        assert "severity" in payload
        assert "correlation_id" in payload

    def test_hitl_payload_contains_description_and_severity(self):
        import asyncio

        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from agents import ha_agent_sandbox_engine

        _orig = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
        _fix = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"
        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "apply_fix",
                                "arguments": {
                                    "yaml_content": _fix,
                                    "description": "Fix server_port from 8123 to 8124",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _orig},
            command_results={"ha core check": (0, "OK", "")},
        )
        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, notifier=notifier, gate=gate
            )
        )
        payload = notifier.sent[0]["payload"]
        assert payload["severity"] == "HIGH"
        assert "Fix server_port" in payload["description"]

    def test_exception_branch_returns_sentinel_trace(self):
        import asyncio

        from utils.llm.ollama_client import FakeLLMClient
        from agents.ha_log_monitor import analyze_log_line_with_ai

        broken_llm = FakeLLMClient("{not valid json}")
        _result, trace = asyncio.run(
            analyze_log_line_with_ai(["ERROR crash"], broken_llm)
        )
        assert trace.raw_response == ""

    def test_transient_pattern_skips_llm(self):
        """_TRANSIENT_LOG_PATTERNS match should skip analyze_log_line_with_ai."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from agents.ha_log_monitor import _TRANSIENT_LOG_PATTERNS, CRITICAL_LOG_PATTERN

        # Confirm the patterns match as expected
        assert (
            _TRANSIENT_LOG_PATTERNS.search("NameResolutionError: Failed to resolve")
            is not None
        )
        assert (
            _TRANSIENT_LOG_PATTERNS.search("MaxRetryError: Max retries exceeded")
            is not None
        )
        assert _TRANSIENT_LOG_PATTERNS.search("Status Code: 503") is not None

        # Confirm CRITICAL_LOG_PATTERN still matches so the branch is entered
        trigger = "ERROR - Component error NameResolutionError: Failed to resolve api.example.com"
        assert CRITICAL_LOG_PATTERN.search(trigger) is not None
        # Transient pattern also matches this line
        assert _TRANSIENT_LOG_PATTERNS.search(trigger) is not None


# ── web/dashboard.py (rich payload rendering) ────────────────────────────────


class TestDashboardRichPayload:
    """Tests for Evidence, Diagnosis, and LLM Interaction sections."""

    def _write_request(self, watch_dir: Path, nid: str, payload: dict) -> None:
        import json as _json
        import time as _time

        (watch_dir / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Test subject",
                    "body": "Test body",
                    "payload": payload,
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )

    def test_evidence_section_rendered_when_present(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(
            tmp_path,
            "ev1",
            {
                "evidence_raw": {
                    "addon_info": "state: stopped",
                    "addon_logs": "ERROR: port in use",
                }
            },
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "Evidence" in html
        assert "addon_info" in html
        assert "state: stopped" in html

    def test_evidence_section_absent_when_missing(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "ev2", {"severity": "HIGH"})
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "Raw gathered data" not in html

    def test_diagnosis_section_rendered_when_present(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(
            tmp_path,
            "dg1",
            {
                "diagnosis": {
                    "is_valid": False,
                    "severity": "HIGH",
                    "identified_issues": ["port conflict"],
                    "recommended_fix_yaml": None,
                }
            },
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "Diagnosis" in html
        assert "severity" in html
        assert "HIGH" in html

    def test_llm_interaction_section_rendered_when_present(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(
            tmp_path,
            "llm1",
            {
                "llm_trace": {
                    "model": "qwen2.5-coder:7b",
                    "system_prompt": "You are helpful.",
                    "user_prompt": "Analyze this.",
                    "raw_response": '{"is_valid": true}',
                    "timestamp": 1753123200,
                }
            },
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "LLM Interaction" in html
        assert "qwen2.5-coder:7b" in html
        assert "System prompt" in html
        assert "User prompt" in html
        assert "Raw response" in html

    def test_log_buffer_snapshot_rendered_as_pre(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(
            tmp_path,
            "lb1",
            {"evidence_raw": {"log_buffer_snapshot": ["ERROR crash", "INFO ok"]}},
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "<pre>" in html
        assert "ERROR crash" in html
        assert "INFO ok" in html

    def test_full_payload_fallback_still_present(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(
            tmp_path,
            "fp1",
            {
                "diagnosis": {"severity": "LOW"},
                "llm_trace": {
                    "model": "m",
                    "system_prompt": "s",
                    "user_prompt": "u",
                    "raw_response": "r",
                    "timestamp": 1753123200,
                },
            },
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "Full payload (raw JSON)" in html

    def test_epoch_to_iso_filter_registered(self):
        import web.dashboard as dashboard

        assert "epoch_to_iso" in dashboard.templates.env.filters
        fn = dashboard.templates.env.filters["epoch_to_iso"]
        result = fn(1753123200)
        assert (
            result.startswith("2025-")
            or result.startswith("2026-")
            or len(result) == 19
        )


# ── Backup inventory dashboard tab (item 32) ─────────────────────────────────────


class TestBackupInventoryDashboard:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard
        from agents import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(dashboard, "DB_PATH", path)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_backups_route_returns_200(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/backups")
        assert resp.status_code == 200

    def test_empty_inventory_shows_no_backups_message(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/backups").text
        assert "no backups" in html.lower()

    def test_inventory_row_displays_slug_and_marks(self, db_path):
        import sqlite3
        import time
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location, offloaded_at)"
                " VALUES (?, 'deadbeef', 'ACTIVE', 52428800, 'both', ?)",
                (int(time.time()), time.time()),
            )
            conn.commit()

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/backups").text
        assert "deadbeef" in html
        assert "50.0 MB" in html

    def test_ha_only_slug_shows_pueo_cross(self, db_path):
        import sqlite3
        import time
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, 'haonly123', 'ACTIVE', 0, 'ha')",
                (int(time.time()),),
            )
            conn.commit()

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/backups").text
        assert "haonly123" in html


# ── Defer endpoint and DEFERRED status ───────────────────────────────────────────


class TestDeferEndpoint:
    def _write_request(self, watch_dir: Path, nid: str) -> None:
        import json as _json
        import time as _time

        (watch_dir / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Update available",
                    "body": "body",
                    "payload": {"component": "core"},
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )

    def test_defer_creates_deferred_and_rejected_files(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "upd1")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/defer/upd1", follow_redirects=False)
        assert (tmp_path / "upd1.deferred").exists()
        assert (tmp_path / "upd1.rejected").exists()

    def test_defer_removes_card_from_queue(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "upd2")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/defer/upd2", follow_redirects=False)
        response = client.get("/queue")
        # Deferred cards leave the queue; they appear only in the Timeline
        assert "upd2" not in response.text
        assert (tmp_path / "upd2.deferred").exists()

    def test_defer_unknown_nid_is_noop(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/defer/no-such-id", follow_redirects=False)
        assert response.status_code == 303
        assert not (tmp_path / "no-such-id.deferred").exists()

    def test_defer_already_resolved_is_noop(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "upd3")
        (tmp_path / "upd3.approved").touch()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/defer/upd3", follow_redirects=False)
        assert not (tmp_path / "upd3.deferred").exists()


class TestSuppressAndKnownIssues:
    """Tests for POST /suppress/{nid}, GET /known-issues, POST /known-issues/resolve/{key}."""

    _HITL_DDL = (
        "CREATE TABLE IF NOT EXISTS hitl_suppression ("
        "card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '', "
        "description TEXT NOT NULL DEFAULT '', first_sent_at REAL NOT NULL DEFAULT 0, "
        "last_sent_at REAL NOT NULL DEFAULT 0, send_count INTEGER NOT NULL DEFAULT 1, "
        "last_action TEXT, last_action_at REAL, rejection_count INTEGER NOT NULL DEFAULT 0, "
        "next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0, "
        "known_issue_note TEXT, resolved_at REAL)"
    )

    def _setup(self, tmp_path, monkeypatch):
        import sqlite3
        import web.dashboard as dashboard

        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as c:
            c.execute(self._HITL_DDL)
        monkeypatch.setattr(dashboard, "DB_PATH", db)
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        return db

    def _write_card(self, watch_dir, nid, suppression_key="resource:disk_critical"):
        import json as _j
        import time as _t

        (watch_dir / f"{nid}.json").write_text(
            _j.dumps(
                {
                    "notification_id": nid,
                    "subject": "s",
                    "body": "b",
                    "payload": {"suppression_key": suppression_key},
                    "sent_at": int(_t.time()) - 5,
                }
            )
        )

    def test_suppress_writes_approved_file(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        self._setup(tmp_path, monkeypatch)
        self._write_card(tmp_path, "s1")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/suppress/s1", follow_redirects=False)
        assert (tmp_path / "s1.approved").exists()

    def test_suppress_marks_known_issue_in_db(self, tmp_path, monkeypatch):
        import sqlite3
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        db = self._setup(tmp_path, monkeypatch)
        self._write_card(tmp_path, "s2", suppression_key="resource:disk_critical")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/suppress/s2", follow_redirects=False)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT known_issue FROM hitl_suppression WHERE card_key=?",
                ("resource:disk_critical",),
            ).fetchone()
        assert row is not None and row[0] == 1

    def test_suppress_without_suppression_key_is_noop_on_db(
        self, tmp_path, monkeypatch
    ):
        import sqlite3, json as _j, time as _t
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        db = self._setup(tmp_path, monkeypatch)
        # Card with no suppression_key in payload
        (tmp_path / "s3.json").write_text(
            _j.dumps(
                {
                    "notification_id": "s3",
                    "subject": "s",
                    "body": "b",
                    "payload": {},
                    "sent_at": int(_t.time()) - 5,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/suppress/s3", follow_redirects=False)
        with sqlite3.connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM hitl_suppression").fetchone()[0]
        assert count == 0

    def test_known_issues_returns_empty_json(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        self._setup(tmp_path, monkeypatch)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/known-issues")
        assert response.status_code == 200
        assert response.json() == []

    def test_known_issues_returns_active_items(self, tmp_path, monkeypatch):
        import sqlite3
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.hitl_tracker import mark_card_acknowledged, mark_card_sent

        db = self._setup(tmp_path, monkeypatch)
        with sqlite3.connect(db) as conn:
            mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
            mark_card_acknowledged(conn, "resource:disk_critical")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/known-issues")
        assert response.status_code == 200
        issues = response.json()
        assert len(issues) == 1
        assert issues[0]["card_key"] == "resource:disk_critical"

    def test_resolve_known_issue_clears_entry(self, tmp_path, monkeypatch):
        import sqlite3
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.hitl_tracker import mark_card_acknowledged, mark_card_sent

        db = self._setup(tmp_path, monkeypatch)
        with sqlite3.connect(db) as conn:
            mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
            mark_card_acknowledged(conn, "resource:disk_critical")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post(
            "/known-issues/resolve/resource:disk_critical", follow_redirects=False
        )
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression WHERE card_key=?",
                ("resource:disk_critical",),
            ).fetchone()
        assert row is not None and row[0] is not None


class TestDismissNotificationRoute:
    """Tests for the /dismiss-notification/{card_id} POST route."""

    def _write_notification_card(
        self, watch_dir: Path, card_id: str, ha_nid: str
    ) -> None:
        import json as _json
        import time as _time

        (watch_dir / f"{card_id}.json").write_text(
            _json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Failed login attempt",
                    "body": "Someone tried to log in.",
                    "payload": {
                        "ha_notification_id": ha_nid,
                        "is_notification_card": True,
                    },
                    "sent_at": int(_time.time()) - 60,
                }
            )
        )

    def test_dismiss_creates_approved_file(self, tmp_path, monkeypatch):
        import utils.ha.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            utils.ha.ha_rest_client, "HARestClient", lambda *a, **kw: FakeHARestClient()
        )
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_http_login", follow_redirects=False
        )
        assert response.status_code == 303
        assert (tmp_path / "notif_http_login.approved").exists()

    def test_dismiss_calls_ha_service(self, tmp_path, monkeypatch):
        import utils.ha.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        fake_rest = FakeHARestClient()
        monkeypatch.setattr(
            utils.ha.ha_rest_client, "HARestClient", lambda *a, **kw: fake_rest
        )
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/dismiss-notification/notif_http_login", follow_redirects=False)
        assert (
            "persistent_notification",
            "dismiss",
            {"notification_id": "http_login"},
        ) in fake_rest.service_calls

    def test_dismiss_missing_json_is_noop(self, tmp_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_none", follow_redirects=False
        )
        assert response.status_code == 303
        assert not (tmp_path / "notif_none.approved").exists()

    def test_dismiss_ha_service_exception_returns_error_redirect(
        self, tmp_path, monkeypatch
    ):
        """If the HA service call raises, no .approved file is written and DB is not updated."""
        from agents import ha_notification_manager
        import utils.ha.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_notification_card(tmp_path, "notif_exc", "http_login")

        class _FailingRest:
            async def call_service(self, *a, **kw):
                raise RuntimeError("HA unreachable")

        monkeypatch.setattr(
            utils.ha.ha_rest_client, "HARestClient", lambda *a, **kw: _FailingRest()
        )
        dismissed_calls: list[str] = []
        monkeypatch.setattr(
            ha_notification_manager,
            "mark_notification_dismissed",
            lambda nid, dismissed_by="user": dismissed_calls.append(nid),
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_exc", follow_redirects=False
        )
        assert response.status_code == 303
        assert "dismiss_error=1" in response.headers["location"]
        assert not (tmp_path / "notif_exc.approved").exists()
        assert dismissed_calls == []

    def test_dismiss_already_resolved_is_noop(self, tmp_path, monkeypatch):
        import utils.ha.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        fake_rest = FakeHARestClient()
        monkeypatch.setattr(
            utils.ha.ha_rest_client, "HARestClient", lambda *a, **kw: fake_rest
        )
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        (tmp_path / "notif_http_login.rejected").touch()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/dismiss-notification/notif_http_login", follow_redirects=False)
        assert not fake_rest.service_calls

    def test_dismiss_after_reoccurrence(self, tmp_path, monkeypatch):
        """Dismiss works on a new card occurrence even when stale .approved file exists."""
        import asyncio as _asyncio

        from agents import ha_notification_manager
        import utils.ha.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha.ha_rest_client import FakeHARestClient
        from utils.notify import FileNotifier

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        fake_rest = FakeHARestClient()
        monkeypatch.setattr(
            utils.ha.ha_rest_client, "HARestClient", lambda *a, **kw: fake_rest
        )
        dismissed_calls: list[str] = []
        monkeypatch.setattr(
            ha_notification_manager,
            "mark_notification_dismissed",
            lambda nid, dismissed_by="user": dismissed_calls.append(nid),
        )
        # Simulate a new card being written for a notification that was previously dismissed.
        # FileNotifier.send() must clear the stale .approved so dismiss can proceed.
        notifier = FileNotifier(watch_dir=str(tmp_path))
        _asyncio.run(
            notifier.send(
                "Login attempt",
                "Someone tried to log in.",
                {
                    "notification_id": "notif_http_login",
                    "ha_notification_id": "http_login",
                    "is_notification_card": True,
                },
            )
        )
        # Before the fix this stale file would block the dismiss handler.
        # After the fix, FileNotifier.send() already removed it above.
        assert not (tmp_path / "notif_http_login.approved").exists()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_http_login", follow_redirects=False
        )
        assert response.status_code == 303
        assert (
            "persistent_notification",
            "dismiss",
            {"notification_id": "http_login"},
        ) in fake_rest.service_calls
        assert (tmp_path / "notif_http_login.approved").exists()
        assert "http_login" in dismissed_calls

    def test_dismiss_empty_ha_nid_still_approves(self, tmp_path, monkeypatch):
        """Card with no ha_notification_id: no HA service call but .approved is written."""
        import json as _json
        import time as _time

        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        (tmp_path / "notif_no_nid.json").write_text(
            _json.dumps(
                {
                    "notification_id": "notif_no_nid",
                    "subject": "Something",
                    "body": "body",
                    "payload": {"is_notification_card": True},
                    "sent_at": int(_time.time()) - 60,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_no_nid", follow_redirects=False
        )
        assert response.status_code == 303
        assert (tmp_path / "notif_no_nid.approved").exists()

    def test_notification_card_excluded_from_queue(self, tmp_path, monkeypatch):
        """Notification cards must not appear in the Queue tab — they belong in Notifications."""
        import json as _json
        import time as _time

        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        card_id = "notif_http_login"
        (tmp_path / f"{card_id}.json").write_text(
            _json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Failed login",
                    "body": "Explanation here.",
                    "payload": {
                        "is_notification_card": True,
                        "ha_notification_id": "http_login",
                        "category": "security",
                        "severity": "HIGH",
                        "enriched_context": {},
                        "human_explanation": "Explanation here.",
                        "recommended_action": "Check your logs.",
                    },
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/queue")
        assert "notif_http_login" not in response.text
        assert "Failed login" not in response.text

    def test_dismiss_redirects_to_notifications(self, tmp_path, monkeypatch):
        """After dismissing, user is sent to /notifications, not /queue."""
        import utils.ha.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            utils.ha.ha_rest_client, "HARestClient", lambda *a, **kw: FakeHARestClient()
        )
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_http_login", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/notifications"

    def test_notifications_keep_touches_rejected_sentinel(self, tmp_path, monkeypatch):
        """POST /notifications/keep/{card_id} marks the card rejected."""
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/notifications/keep/notif_http_login", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/notifications"
        assert (tmp_path / "notif_http_login.rejected").exists()

    def test_notifications_keep_missing_json_is_noop(self, tmp_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/notifications/keep/notif_none", follow_redirects=False)
        assert response.status_code == 303
        assert not (tmp_path / "notif_none.rejected").exists()

    def test_repair_card_shows_approve_reject_buttons(self, tmp_path, monkeypatch):
        import json as _json
        import time as _time

        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        card_id = "repair-abc123"
        (tmp_path / f"{card_id}.json").write_text(
            _json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Repair action",
                    "body": "Apply fix?",
                    "payload": {"card_type": "repair", "severity": "MEDIUM"},
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/queue")
        assert "Approve" in response.text
        assert "Reject" in response.text
        assert "Defer" in response.text

    def test_informational_card_shows_acknowledge_button(self, tmp_path, monkeypatch):
        import json as _json
        import time as _time

        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        card_id = "info-xyz"
        (tmp_path / f"{card_id}.json").write_text(
            _json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Backup failed",
                    "body": "HA backup failed. Aborting to protect HA state.",
                    "payload": {"severity": "CRITICAL", "step": 6},
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/queue")
        assert "Acknowledge" in response.text
        assert "Reject" not in response.text
        assert "Defer" not in response.text


class TestDeferredStatusRecognition:
    def _write_request(self, tmp_path: Path, nid: str) -> None:
        import json as _json
        import time as _time

        (tmp_path / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "s",
                    "body": "b",
                    "payload": {},
                    "sent_at": int(_time.time()) - 60,
                }
            )
        )

    def test_deferred_file_yields_deferred_status(self, tmp_path):
        from web.dashboard import _status

        self._write_request(tmp_path, "ddd")
        (tmp_path / "ddd.deferred").touch()
        (tmp_path / "ddd.rejected").touch()
        assert _status("ddd", tmp_path) == "DEFERRED"

    def test_deferred_card_excluded_from_queue(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "ddd")
        (tmp_path / "ddd.deferred").touch()
        (tmp_path / "ddd.rejected").touch()
        assert _load_requests(tmp_path) == []

    def test_deferred_takes_precedence_over_rejected(self, tmp_path):
        from web.dashboard import _status

        (tmp_path / "x.deferred").touch()
        (tmp_path / "x.json").touch()
        (tmp_path / "x.rejected").touch()
        assert _status("x", tmp_path) == "DEFERRED"

    def test_deferred_is_valid_hitl_request_status(self):
        from web.dashboard import HITLRequest

        r = HITLRequest(
            notification_id="x",
            subject="s",
            body="b",
            payload={},
            sent_at=0,
            status="DEFERRED",
            elapsed_seconds=0,
        )
        assert r.status == "DEFERRED"


# ── Notifications tab ────────────────────────────────────────────────────────


class TestNotificationsDashboard:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard
        from agents import ha_agent_advanced

        path = str(tmp_path / "notif_test.db")
        monkeypatch.setattr(dashboard, "DB_PATH", path)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    @pytest.fixture
    def watch_dir(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard

        d = tmp_path / "watch"
        d.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(d))
        return d

    def _insert_notification(
        self,
        db_path: str,
        notification_id: str,
        category: str = "security",
        severity: str = "HIGH",
        first_seen_at: float = 1000.0,
        dismissed_at: float | None = None,
        hitl_sent_at: float | None = None,
    ) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO notification_history
                    (notification_id, first_seen_at, last_seen_at, category, severity,
                     hitl_sent_at, dismissed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    first_seen_at,
                    first_seen_at,
                    category,
                    severity,
                    hitl_sent_at,
                    dismissed_at,
                ),
            )

    def test_notifications_route_returns_200(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/notifications")
        assert resp.status_code == 200

    def test_empty_state_shows_no_pending_message(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "no pending notifications" in html.lower()

    def test_nav_has_notifications_link(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "/notifications" in html

    def test_pending_notification_shown_in_pending_section(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login", severity="CRITICAL")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "http_login" in html
        assert "CRITICAL" in html

    def test_dismissed_notification_not_in_pending(self, db_path, watch_dir):
        import time

        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "old_login", dismissed_at=time.time() - 100)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "No pending notifications" in html

    def test_dismissed_notification_appears_in_history(self, db_path, watch_dir):
        import time

        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "past_login", dismissed_at=time.time() - 200)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "past_login" in html

    def test_enrichment_data_from_card_json_shown(self, db_path, watch_dir):
        import json

        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login", hitl_sent_at=1000.0)
        (watch_dir / "notif_http_login.json").write_text(
            json.dumps(
                {
                    "notification_id": "notif_http_login",
                    "subject": "s",
                    "body": "b",
                    "payload": {
                        "human_explanation": "Someone tried to log in from 1.2.3.4.",
                        "recommended_action": "Check your firewall.",
                        "enriched_context": {
                            "source_ip": "1.2.3.4",
                            "is_known_device": False,
                        },
                        "original_message": "Invalid auth from 1.2.3.4",
                        "original_title": "Failed Login",
                    },
                    "sent_at": 1000,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "Someone tried to log in from 1.2.3.4" in html
        assert "Check your firewall" in html

    def test_dismiss_button_shown_when_card_file_exists(self, db_path, watch_dir):
        import json

        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login", hitl_sent_at=1000.0)
        (watch_dir / "notif_http_login.json").write_text(
            json.dumps(
                {
                    "notification_id": "notif_http_login",
                    "subject": "s",
                    "body": "b",
                    "payload": {"ha_notification_id": "http_login"},
                    "sent_at": 1000,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "Dismiss in HA" in html

    def test_dismiss_button_absent_when_no_card_file(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "Dismiss in HA" not in html

    def test_category_filter_hides_other_categories(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login", category="security")
        self._insert_notification(
            db_path, "invalid_config", category="config_error", first_seen_at=2000.0
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications?category=security").text
        assert "http_login" in html
        assert "invalid_config" not in html

    def test_severity_filter_hides_other_severities(self, db_path, watch_dir):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login", severity="CRITICAL")
        self._insert_notification(
            db_path, "low_notif", severity="LOW", first_seen_at=2000.0
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications?severity=LOW").text
        assert "low_notif" in html
        assert "http_login" not in html

    def test_history_section_shows_all_notifications(self, db_path, watch_dir):
        import time

        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_notification(db_path, "http_login")
        self._insert_notification(
            db_path,
            "invalid_config",
            category="config_error",
            first_seen_at=2000.0,
            dismissed_at=time.time(),
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/notifications").text
        assert "http_login" in html
        assert "invalid_config" in html

    def test_load_notification_dashboard_data_empty_db(self, db_path, watch_dir):
        from web.dashboard import _load_notification_dashboard_data

        pending, history = _load_notification_dashboard_data(watch_dir)
        assert pending == []
        assert history == []

    def test_load_notification_dashboard_data_enriches_from_card(
        self, db_path, watch_dir
    ):
        import json

        from web.dashboard import _load_notification_dashboard_data

        self._insert_notification(db_path, "http_login")
        (watch_dir / "notif_http_login.json").write_text(
            json.dumps(
                {
                    "payload": {
                        "human_explanation": "explanation here",
                        "original_message": "raw msg",
                    }
                }
            )
        )
        pending, history = _load_notification_dashboard_data(watch_dir)
        assert len(pending) == 1
        assert pending[0]["human_explanation"] == "explanation here"
        assert pending[0]["original_message"] == "raw msg"

    def test_sort_by_category(self, db_path, watch_dir):
        from web.dashboard import _load_notification_dashboard_data

        self._insert_notification(db_path, "zzz_notif", category="security")
        self._insert_notification(
            db_path, "aaa_notif", category="config_error", first_seen_at=2000.0
        )
        _, history = _load_notification_dashboard_data(watch_dir, sort_by="category")
        categories = [r["category"] for r in history]
        assert categories == sorted(categories)

    def test_db_exception_returns_empty(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard
        from web.dashboard import _load_notification_dashboard_data

        # Point DB_PATH at a path with no notification_history table
        empty_db = str(tmp_path / "empty.db")
        monkeypatch.setattr(dashboard, "DB_PATH", empty_db)
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        pending, history = _load_notification_dashboard_data(watch_dir)
        assert pending == []
        assert history == []

    def test_malformed_card_json_row_still_included(self, db_path, watch_dir):
        from web.dashboard import _load_notification_dashboard_data

        self._insert_notification(db_path, "http_login")
        (watch_dir / "notif_http_login.json").write_text("not valid json {{{{")
        pending, history = _load_notification_dashboard_data(watch_dir)
        # Row is still returned; enrichment fields are simply absent
        assert len(pending) == 1
        assert "human_explanation" not in pending[0]


# ── Card-type dispatch (item 56) ──────────────────────────────────────────────────


class TestCardTypeDispatch:
    """Verify approve() routes to the correct handler based on card_type field."""

    @pytest.fixture()
    def watch_dir(self, tmp_path):
        d = tmp_path / "hitl"
        d.mkdir()
        return d

    def _write_card(self, watch_dir, nid, payload):
        import json as _json
        import time

        card = {
            "notification_id": nid,
            "subject": "Test card",
            "body": "body",
            "payload": payload,
            "sent_at": int(time.time()),
        }
        (watch_dir / f"{nid}.json").write_text(_json.dumps(card))

    def test_repair_card_routes_to_execute_queued_fix(self, watch_dir, monkeypatch):
        """CARD_TYPE_REPAIR dispatches to _execute_queued_fix."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        called = []

        async def _fake_fix(nid, yaml_content, description, data, json_path, wd):
            called.append(nid)

        monkeypatch.setattr(dashboard, "_execute_queued_fix", _fake_fix)
        self._write_card(
            watch_dir,
            "r1",
            {
                "card_type": "repair",
                "pending_fix_yaml": "homeassistant:\n  name: Fixed\n",
                "pending_fix_description": "test fix",
            },
        )

        async def _run():
            await dashboard.approve("r1")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert "r1" in called

    def test_legacy_repair_card_routes_to_execute_queued_fix(
        self, watch_dir, monkeypatch
    ):
        """Card with pending_fix_yaml but no card_type still routes to _execute_queued_fix."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        called = []

        async def _fake_fix(nid, yaml_content, description, data, json_path, wd):
            called.append(nid)

        monkeypatch.setattr(dashboard, "_execute_queued_fix", _fake_fix)
        self._write_card(
            watch_dir,
            "legacy1",
            {
                "pending_fix_yaml": "homeassistant:\n  name: Fixed\n",
                "pending_fix_description": "legacy fix",
            },
        )

        async def _run():
            await dashboard.approve("legacy1")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert "legacy1" in called

    def test_update_card_routes_to_execute_queued_update(self, watch_dir, monkeypatch):
        """CARD_TYPE_UPDATE dispatches to _execute_queued_update."""
        from utils.card_types import CARD_TYPE_UPDATE
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        called = []

        async def _fake_update(nid, data, json_path, wd):
            called.append(nid)
            (wd / f"{nid}.approved").touch()

        monkeypatch.setitem(dashboard._CARD_DISPATCH, CARD_TYPE_UPDATE, _fake_update)
        self._write_card(
            watch_dir,
            "u1",
            {"card_type": "update", "component": "core", "latest_version": "2026.7.5"},
        )

        async def _run():
            await dashboard.approve("u1")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert "u1" in called
        assert (watch_dir / "u1.approved").exists()

    def test_netalertx_heal_card_routes_to_executor(self, watch_dir, monkeypatch):
        """CARD_TYPE_NETALERTX_HEAL dispatches to _execute_netalertx_heal."""
        from utils.card_types import CARD_TYPE_NETALERTX_HEAL
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        called = []

        async def _fake_heal(nid, data, json_path, wd):
            called.append(nid)
            (wd / f"{nid}.approved").touch()

        monkeypatch.setitem(
            dashboard._CARD_DISPATCH, CARD_TYPE_NETALERTX_HEAL, _fake_heal
        )
        self._write_card(
            watch_dir,
            "nh1",
            {"card_type": "netalertx_heal", "heal_action": "fix_webhook_fields"},
        )

        async def _run():
            await dashboard.approve("nh1")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert "nh1" in called
        assert (watch_dir / "nh1.approved").exists()

    def test_resource_action_card_routes_to_executor(self, watch_dir, monkeypatch):
        """CARD_TYPE_RESOURCE_ACTION dispatches to _execute_resource_action."""
        from utils.card_types import CARD_TYPE_RESOURCE_ACTION
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        called = []

        async def _fake_resource(nid, data, json_path, wd):
            called.append(nid)
            (wd / f"{nid}.approved").touch()

        monkeypatch.setitem(
            dashboard._CARD_DISPATCH, CARD_TYPE_RESOURCE_ACTION, _fake_resource
        )
        self._write_card(
            watch_dir,
            "ra1",
            {"card_type": "resource_action", "action": "offload_backups"},
        )

        async def _run():
            await dashboard.approve("ra1")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert "ra1" in called
        assert (watch_dir / "ra1.approved").exists()

    def test_unknown_card_type_falls_back_to_approved(self, watch_dir, monkeypatch):
        """Unknown card_type just touches the .approved file."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        self._write_card(watch_dir, "x1", {"card_type": "future_unknown_type"})
        asyncio.run(dashboard.approve("x1"))
        assert (watch_dir / "x1.approved").exists()

    def test_approve_sets_in_progress_synchronously(self, watch_dir, monkeypatch):
        """approve() marks in_progress before the handler coroutine runs, so the
        redirect lands on a queue page that already shows the spinner."""
        import web.dashboard as dashboard
        from utils.card_types import CARD_TYPE_UPDATE

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        in_progress_at_task_start = []

        async def _slow_handler(nid, data, json_path, wd):
            in_progress_at_task_start.append((wd / f"{nid}.in_progress").exists())
            (wd / f"{nid}.approved").touch()

        monkeypatch.setitem(dashboard._CARD_DISPATCH, CARD_TYPE_UPDATE, _slow_handler)
        self._write_card(
            watch_dir,
            "ip1",
            {"card_type": "update", "component": "core", "latest_version": "2026.7.5"},
        )

        async def _run():
            await dashboard.approve("ip1")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert in_progress_at_task_start == [
            True
        ], ".in_progress must be set before the handler task begins"

    def test_card_type_constants_exist(self):
        """Verify all four card type constants are importable."""
        from utils.card_types import (
            CARD_TYPE_NETALERTX_HEAL,
            CARD_TYPE_REPAIR,
            CARD_TYPE_RESOURCE_ACTION,
            CARD_TYPE_UPDATE,
        )

        assert CARD_TYPE_REPAIR == "repair"
        assert CARD_TYPE_UPDATE == "update"
        assert CARD_TYPE_NETALERTX_HEAL == "netalertx_heal"
        assert CARD_TYPE_RESOURCE_ACTION == "resource_action"

    def test_repair_payload_includes_card_type(self):
        """apply_fix payload includes card_type=repair."""
        from utils.card_types import CARD_TYPE_REPAIR

        assert CARD_TYPE_REPAIR == "repair"

    def test_resource_action_payload_includes_card_type(self):
        """Disk-critical alert payload includes card_type=resource_action."""
        from utils.card_types import CARD_TYPE_RESOURCE_ACTION

        assert CARD_TYPE_RESOURCE_ACTION == "resource_action"


# ── _execute_queued_update (item 57) ─────────────────────────────────────────────


class TestExecuteQueuedUpdate:
    """Tests for the real _execute_queued_update() handler."""

    def _write_update_card(self, watch_dir: Path, nid: str) -> Path:
        import json as _json
        import time as _time

        data = {
            "notification_id": nid,
            "subject": "Update available: core",
            "body": "Core 2026.7.4 → 2026.7.5",
            "payload": {
                "card_type": "update",
                "component": "core",
                "installed_version": "2026.7.4",
                "latest_version": "2026.7.5",
                "release_url": "https://example.com/release",
                "release_summary": "Bug fixes",
            },
            "sent_at": int(_time.time()) - 30,
        }
        json_path = watch_dir / f"{nid}.json"
        json_path.write_text(_json.dumps(data))
        return json_path

    def test_success_path_writes_approved_and_fix_applied(self, tmp_path, monkeypatch):
        """execute_update returns True → .approved created, fix_applied=True in JSON."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_update_manager
        import utils.ha.ssh_client as ssh_mod
        import utils.agent.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ha.ssh_client import FakeSSHClient

        nid = "upd-ok-1"
        json_path = self._write_update_card(tmp_path, nid)

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())
        monkeypatch.setattr(
            autonomy_mod, "AutonomyGate", lambda level: autonomy_mod.FakeAutonomyGate()
        )
        fake_notifier = notify_mod.FakeNotifier(approve=True)
        monkeypatch.setattr(notify_mod, "get_notifier", lambda *a, **kw: fake_notifier)
        monkeypatch.setattr(
            ha_update_manager, "execute_update", _make_async_return(True)
        )

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_queued_update(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert saved["fix_applied"] is True

    def test_failure_path_writes_rejected_and_fix_error(self, tmp_path, monkeypatch):
        """execute_update returns False → .rejected created, fix_error in JSON."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_update_manager
        import utils.ha.ssh_client as ssh_mod
        import utils.agent.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ha.ssh_client import FakeSSHClient

        nid = "upd-fail-1"
        json_path = self._write_update_card(tmp_path, nid)

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())
        monkeypatch.setattr(
            autonomy_mod, "AutonomyGate", lambda level: autonomy_mod.FakeAutonomyGate()
        )
        monkeypatch.setattr(
            notify_mod,
            "get_notifier",
            lambda *a, **kw: notify_mod.FakeNotifier(approve=False),
        )
        monkeypatch.setattr(
            ha_update_manager, "execute_update", _make_async_return(False)
        )

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_queued_update(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert "fix_error" in saved
        assert "core" in saved["fix_error"]

    def test_exception_writes_rejected_and_fix_error(self, tmp_path, monkeypatch):
        """Exception during execute_update → .rejected, fix_error set, no in_progress."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_update_manager
        import utils.ha.ssh_client as ssh_mod
        import utils.agent.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ha.ssh_client import FakeSSHClient

        nid = "upd-exc-1"
        json_path = self._write_update_card(tmp_path, nid)

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())
        monkeypatch.setattr(
            autonomy_mod, "AutonomyGate", lambda level: autonomy_mod.FakeAutonomyGate()
        )
        monkeypatch.setattr(
            notify_mod,
            "get_notifier",
            lambda *a, **kw: notify_mod.FakeNotifier(approve=False),
        )

        async def _raise(*a, **kw):
            raise RuntimeError("SSH timeout")

        monkeypatch.setattr(ha_update_manager, "execute_update", _raise)

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_queued_update(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert "SSH timeout" in saved["fix_error"]

    def test_in_progress_file_removed_on_success(self, tmp_path, monkeypatch):
        """in_progress sentinel is cleaned up even on success."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_update_manager
        import utils.ha.ssh_client as ssh_mod
        import utils.agent.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ha.ssh_client import FakeSSHClient

        nid = "upd-cleanup-1"
        json_path = self._write_update_card(tmp_path, nid)

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda: FakeSSHClient())
        monkeypatch.setattr(
            autonomy_mod, "AutonomyGate", lambda level: autonomy_mod.FakeAutonomyGate()
        )
        monkeypatch.setattr(
            notify_mod,
            "get_notifier",
            lambda *a, **kw: notify_mod.FakeNotifier(approve=True),
        )
        monkeypatch.setattr(
            ha_update_manager, "execute_update", _make_async_return(True)
        )

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_queued_update(nid, data, json_path, tmp_path))

        assert not (tmp_path / f"{nid}.in_progress").exists()

    def test_in_progress_state_shows_spinner_in_template(self, tmp_path, monkeypatch):
        """A card with .in_progress file shows spinner, not Approve/Reject buttons."""
        import json as _json
        import time as _time
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        nid = "upd-spin-1"
        (tmp_path / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Update: core",
                    "body": "Update in progress",
                    "payload": {
                        "card_type": "update",
                        "component": "core",
                        "latest_version": "2026.7.5",
                    },
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )
        (tmp_path / f"{nid}.in_progress").touch()

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "spinner" in html
        assert "Action in progress" in html
        assert "Approve" not in html

    def test_pending_card_without_in_progress_shows_buttons(
        self, tmp_path, monkeypatch
    ):
        """A PENDING card with no .in_progress file still shows Approve/Reject."""
        import json as _json
        import time as _time
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        nid = "upd-btn-1"
        (tmp_path / f"{nid}.json").write_text(
            _json.dumps(
                {
                    "notification_id": nid,
                    "subject": "Update: core",
                    "body": "Core 2026.7.5 available",
                    "payload": {
                        "card_type": "update",
                        "component": "core",
                        "latest_version": "2026.7.5",
                    },
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "Approve" in html
        assert "Action in progress" not in html


# ── _execute_netalertx_heal (item 58) ─────────────────────────────────────────


class TestExecuteNetalertxHeal:
    """Tests for the real _execute_netalertx_heal() handler."""

    def _write_heal_card(
        self, watch_dir: Path, nid: str, heal_action: str = "fix_webhook_fields"
    ) -> Path:
        import json as _json
        import time as _time

        data = {
            "notification_id": nid,
            "subject": "Pueo: NetAlertX — fix HA automation webhook fields",
            "body": "Snake_case webhook fields detected.",
            "payload": {
                "card_type": "netalertx_heal",
                "heal_action": heal_action,
                "fields": ["trigger_platform"],
            },
            "sent_at": int(_time.time()) - 30,
        }
        json_path = watch_dir / f"{nid}.json"
        json_path.write_text(_json.dumps(data))
        return json_path

    def _patch_healer_deps(self, monkeypatch, run_heal_side_effect=None):
        """Patch all dependencies of _execute_netalertx_heal."""
        import netalertx.healer as healer_mod
        import netalertx.api_client as api_mod
        import utils.ha.ssh_client as ssh_mod
        import utils.agent.autonomy as autonomy_mod
        import utils.notify as notify_mod

        class FakeHealer:
            async def run_heal(self, action: str) -> None:
                if run_heal_side_effect is not None:
                    raise run_heal_side_effect
                if action == "unknown_action":
                    raise ValueError(f"Unknown NetAlertX heal action: {action!r}")

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())
        monkeypatch.setattr(api_mod, "NetAlertXAPIClient", lambda **kw: object())
        monkeypatch.setattr(
            autonomy_mod, "AutonomyGate", lambda level: autonomy_mod.FakeAutonomyGate()
        )
        monkeypatch.setattr(
            notify_mod, "get_notifier", lambda *a, **kw: notify_mod.FakeNotifier()
        )
        monkeypatch.setattr(healer_mod, "NetAlertXHealer", lambda **kw: FakeHealer())

    def test_success_path_writes_approved_and_fix_applied(self, tmp_path, monkeypatch):
        """run_heal succeeds → .approved created, fix_applied=True in JSON."""
        import json as _json
        import web.dashboard as dashboard

        nid = "nh-ok-1"
        json_path = self._write_heal_card(tmp_path, nid)
        self._patch_healer_deps(monkeypatch)

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_netalertx_heal(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert saved["fix_applied"] is True

    def test_exception_writes_rejected_and_fix_error(self, tmp_path, monkeypatch):
        """run_heal raises → .rejected created, fix_error set, no in_progress."""
        import json as _json
        import web.dashboard as dashboard

        nid = "nh-exc-1"
        json_path = self._write_heal_card(tmp_path, nid)
        self._patch_healer_deps(
            monkeypatch, run_heal_side_effect=RuntimeError("SSH failed")
        )

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_netalertx_heal(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert "SSH failed" in saved["fix_error"]

    def test_unknown_heal_action_writes_rejected(self, tmp_path, monkeypatch):
        """Unknown heal_action propagates as ValueError → .rejected."""
        import json as _json
        import web.dashboard as dashboard

        nid = "nh-unk-1"
        json_path = self._write_heal_card(tmp_path, nid, heal_action="unknown_action")
        self._patch_healer_deps(
            monkeypatch,
            run_heal_side_effect=ValueError(
                "Unknown NetAlertX heal action: 'unknown_action'"
            ),
        )

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_netalertx_heal(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()

    def test_in_progress_sentinel_removed_on_success(self, tmp_path, monkeypatch):
        """in_progress file is cleaned up after successful heal."""
        import json as _json
        import web.dashboard as dashboard

        nid = "nh-cleanup-1"
        json_path = self._write_heal_card(tmp_path, nid)
        self._patch_healer_deps(monkeypatch)

        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_netalertx_heal(nid, data, json_path, tmp_path))

        assert not (tmp_path / f"{nid}.in_progress").exists()


# ── _execute_resource_action (item 58) ────────────────────────────────────────


class TestExecuteResourceAction:
    """Tests for the real _execute_resource_action() handler."""

    def _write_resource_card(self, watch_dir: Path, nid: str, action: str) -> Path:
        import json as _json
        import time as _time

        data = {
            "notification_id": nid,
            "subject": "Pueo CRITICAL: HA disk almost full",
            "body": "Disk free: 1.9 GB — below critical threshold 2.0 GB.",
            "payload": {
                "card_type": "resource_action",
                "action": action,
                "severity": "CRITICAL",
                "disk_free_gb": 1.9,
            },
            "sent_at": int(_time.time()) - 30,
        }
        json_path = watch_dir / f"{nid}.json"
        json_path.write_text(_json.dumps(data))
        return json_path

    def _make_db(self, db_path: Path, slugs: list[str]) -> None:
        """Create a minimal backup_registry with the given slugs (location='ha')."""
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS backup_registry"
                " (id INTEGER PRIMARY KEY, backup_slug TEXT, location TEXT,"
                " deleted_from_ha_at REAL, timestamp REAL, status TEXT, size_bytes INTEGER,"
                " offloaded_at REAL)"
            )
            for slug in slugs:
                conn.execute(
                    "INSERT INTO backup_registry (backup_slug, location, timestamp, status, size_bytes)"
                    " VALUES (?, 'ha', ?, 'completed', 1024)",
                    (slug, __import__("time").time()),
                )
            conn.commit()

    def test_offload_backups_calls_offload_and_retention(self, tmp_path, monkeypatch):
        """offload_backups action: offloads pending slugs then enforces retention."""
        import json as _json
        import sqlite3 as _sqlite3
        import web.dashboard as dashboard
        from agents import ha_agent_advanced
        import utils.ha.ssh_client as ssh_mod

        db_path = tmp_path / "test.db"
        self._make_db(db_path, ["slug-a", "slug-b"])

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())
        monkeypatch.setattr(dashboard, "DB_PATH", str(db_path))

        offloaded = []
        retention_called = []
        purge_called = []

        async def _fake_offload(slug, ssh_client=None):
            offloaded.append(slug)
            return True

        async def _fake_retention(ssh_client=None, force_critical=False):
            retention_called.append(True)

        def _fake_purge():
            purge_called.append(True)

        monkeypatch.setattr(ha_agent_advanced, "offload_backup_to_local", _fake_offload)
        monkeypatch.setattr(ha_agent_advanced, "enforce_ha_retention", _fake_retention)
        monkeypatch.setattr(ha_agent_advanced, "purge_local_backups", _fake_purge)

        nid = "ra-offload-1"
        json_path = self._write_resource_card(tmp_path, nid, "offload_backups")
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_resource_action(nid, data, json_path, tmp_path))

        assert set(offloaded) == {"slug-a", "slug-b"}
        assert retention_called
        assert purge_called
        assert (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert saved["fix_applied"] is True

    def test_enforce_retention_calls_retention_and_purge(self, tmp_path, monkeypatch):
        """enforce_retention action: calls enforce_ha_retention + purge_local_backups."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_agent_advanced
        import utils.ha.ssh_client as ssh_mod

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        retention_called = []
        purge_called = []

        async def _fake_retention(ssh_client=None, force_critical=False):
            retention_called.append(True)

        def _fake_purge():
            purge_called.append(True)

        monkeypatch.setattr(ha_agent_advanced, "enforce_ha_retention", _fake_retention)
        monkeypatch.setattr(ha_agent_advanced, "purge_local_backups", _fake_purge)

        nid = "ra-ret-1"
        json_path = self._write_resource_card(tmp_path, nid, "enforce_retention")
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_resource_action(nid, data, json_path, tmp_path))

        assert retention_called
        assert purge_called
        assert (tmp_path / f"{nid}.approved").exists()
        saved = _json.loads(json_path.read_text())
        assert saved["fix_applied"] is True

    def test_unknown_action_writes_rejected(self, tmp_path, monkeypatch):
        """Unknown action raises ValueError → .rejected, fix_error set."""
        import json as _json
        import web.dashboard as dashboard
        import utils.ha.ssh_client as ssh_mod

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        nid = "ra-unk-1"
        json_path = self._write_resource_card(tmp_path, nid, "wipe_everything")
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_resource_action(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert "fix_error" in saved

    def test_offload_backups_sftp_failure_writes_warn(self, tmp_path, monkeypatch):
        """offload_backup_to_local returning False → WARN timeline event; action still succeeds."""
        import json as _json
        import sqlite3 as _sqlite3
        import web.dashboard as dashboard
        from agents import ha_agent_advanced
        import utils.ha.ssh_client as ssh_mod
        import utils.core.timeline

        db_path = tmp_path / "test.db"
        self._make_db(db_path, ["slug-fail"])

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())
        monkeypatch.setattr(dashboard, "DB_PATH", str(db_path))

        async def _fake_offload_fail(slug, ssh_client=None):
            return False

        monkeypatch.setattr(
            ha_agent_advanced, "offload_backup_to_local", _fake_offload_fail
        )
        monkeypatch.setattr(
            ha_agent_advanced, "enforce_ha_retention", _make_async_return(None)
        )
        monkeypatch.setattr(ha_agent_advanced, "purge_local_backups", lambda: None)

        nid = "ra-sftp-fail-1"
        json_path = self._write_resource_card(tmp_path, nid, "offload_backups")
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_resource_action(nid, data, json_path, tmp_path))

        # Action completes successfully despite SFTP failure
        assert (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.rejected").exists()
        saved = _json.loads(json_path.read_text())
        assert saved["fix_applied"] is True

        # A WARN event must be recorded in the (patched) timeline DB
        rows = (
            _sqlite3.connect(utils.core.timeline.DB_PATH)
            .execute(
                "SELECT level, source, message, detail_json FROM timeline_events"
                " WHERE source = 'resource_action' AND level = 'WARN'"
            )
            .fetchall()
        )
        assert rows, "expected a WARN timeline row for failed SFTP transfers"
        detail = _json.loads(rows[0][3])
        assert detail["fail_count"] == 1
        assert detail["total"] == 1

    def test_exception_writes_rejected_and_fix_error(self, tmp_path, monkeypatch):
        """Exception in offload → .rejected, fix_error set, no in_progress."""
        import json as _json
        import sqlite3 as _sqlite3
        import web.dashboard as dashboard
        from agents import ha_agent_advanced
        import utils.ha.ssh_client as ssh_mod

        db_path = tmp_path / "test.db"
        self._make_db(db_path, ["slug-x"])

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())
        monkeypatch.setattr(dashboard, "DB_PATH", str(db_path))

        async def _boom(*a, **kw):
            raise OSError("SFTP failed")

        monkeypatch.setattr(ha_agent_advanced, "offload_backup_to_local", _boom)

        nid = "ra-exc-1"
        json_path = self._write_resource_card(tmp_path, nid, "offload_backups")
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_resource_action(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        assert not (tmp_path / f"{nid}.in_progress").exists()
        saved = _json.loads(json_path.read_text())
        assert "SFTP failed" in saved["fix_error"]

    def test_in_progress_sentinel_removed_on_success(self, tmp_path, monkeypatch):
        """in_progress file cleaned up after successful resource action."""
        import json as _json
        import web.dashboard as dashboard
        from agents import ha_agent_advanced
        import utils.ha.ssh_client as ssh_mod

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        monkeypatch.setattr(
            ha_agent_advanced, "enforce_ha_retention", _make_async_return(None)
        )
        monkeypatch.setattr(ha_agent_advanced, "purge_local_backups", lambda: None)

        nid = "ra-cleanup-1"
        json_path = self._write_resource_card(tmp_path, nid, "enforce_retention")
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_resource_action(nid, data, json_path, tmp_path))

        assert not (tmp_path / f"{nid}.in_progress").exists()


# ── _execute_disk_recovery (CARD_TYPE_DISK_RECOVERY) ─────────────────────────


class TestExecuteDiskRecovery:
    """Tests for the _execute_disk_recovery() HITL handler."""

    def _write_disk_recovery_card(self, watch_dir, nid: str, action_keys: list[str]):
        import json as _json
        import time as _time

        hitl_opts = [{"action_key": k} for k in action_keys]
        data = {
            "notification_id": nid,
            "subject": "Pueo CRITICAL: HA disk space recovery required",
            "body": "Auto-safe steps taken. HITL options below.",
            "payload": {
                "card_type": "disk_recovery",
                "ranked_hitl_options": hitl_opts,
            },
            "sent_at": int(_time.time()) - 10,
        }
        json_path = watch_dir / f"{nid}.json"
        json_path.write_text(_json.dumps(data))
        return json_path

    def test_repack_recorder_calls_purge_recorder(self, tmp_path, monkeypatch):
        import json as _json
        import web.dashboard as dashboard
        import utils.disk_recovery as dr
        import utils.ha.ssh_client as ssh_mod
        import config as _config

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())
        monkeypatch.setattr(_config, "HA_API_TOKEN", "tok")

        purge_calls = []

        async def _fake_purge(rest, keep_days, repack):
            purge_calls.append({"keep_days": keep_days, "repack": repack})
            from utils.disk_recovery import RecoveryAction

            return RecoveryAction("purge_recorder", 0, "ok", True)

        monkeypatch.setattr(dr, "purge_recorder", _fake_purge)

        class _FakeRest:
            pass

        monkeypatch.setattr(
            dashboard,
            "HARestClient",
            lambda *a, **kw: _FakeRest(),
            raising=False,
        )

        nid = "dr-repack-1"
        json_path = self._write_disk_recovery_card(tmp_path, nid, ["repack_recorder"])
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_disk_recovery(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.approved").exists()
        assert len(purge_calls) == 1
        assert purge_calls[0]["repack"] is True

    def test_clean_tmp_calls_audit_and_rm(self, tmp_path, monkeypatch):
        import json as _json
        import web.dashboard as dashboard
        import utils.disk_recovery as dr
        import utils.ha.ssh_client as ssh_mod
        import config as _config

        monkeypatch.setattr(_config, "HA_API_TOKEN", "")

        rm_calls = []

        class _FakeSSH:
            async def run(self, cmd, check=False):
                rm_calls.append(cmd)
                return (0, "", "")

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: _FakeSSH())

        from utils.disk_recovery import TmpAuditItem

        async def _fake_audit(ssh):
            return [TmpAuditItem("/mnt/data/supervisor/tmp/foo", "500M", 500 * 1024**2)]

        monkeypatch.setattr(dr, "audit_supervisor_tmp", _fake_audit)

        nid = "dr-tmp-1"
        json_path = self._write_disk_recovery_card(tmp_path, nid, ["clean_tmp"])
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_disk_recovery(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.approved").exists()
        assert any("rm -rf" in c for c in rm_calls)

    def test_unknown_action_keys_succeed_silently(self, tmp_path, monkeypatch):
        import json as _json
        import web.dashboard as dashboard
        import utils.ha.ssh_client as ssh_mod
        import config as _config

        monkeypatch.setattr(_config, "HA_API_TOKEN", "")
        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        nid = "dr-noop-1"
        json_path = self._write_disk_recovery_card(tmp_path, nid, [])
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_disk_recovery(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.approved").exists()
        assert not (tmp_path / f"{nid}.rejected").exists()

    def test_exception_writes_rejected(self, tmp_path, monkeypatch):
        import json as _json
        import web.dashboard as dashboard
        import utils.ha.ssh_client as ssh_mod
        import config as _config

        monkeypatch.setattr(_config, "HA_API_TOKEN", "tok")
        monkeypatch.setattr(
            ssh_mod,
            "AsyncSSHClient",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        nid = "dr-err-1"
        json_path = self._write_disk_recovery_card(tmp_path, nid, ["repack_recorder"])
        data = _json.loads(json_path.read_text())
        asyncio.run(dashboard._execute_disk_recovery(nid, data, json_path, tmp_path))

        assert (tmp_path / f"{nid}.rejected").exists()
        saved = _json.loads(json_path.read_text())
        assert "fix_error" in saved

    def test_cleanup_orphaned_addon_dirs_deletes_listed_paths(
        self, tmp_path, monkeypatch
    ):
        """cleanup_orphaned_addon_dirs runs rm -rf on each listed path."""
        import json as _json
        import web.dashboard as dashboard
        import utils.ha.ssh_client as ssh_mod
        import config as _config

        monkeypatch.setattr(_config, "HA_API_TOKEN", "")

        deleted_paths: list = []

        class _FakeSSH:
            async def run(self, cmd, check=False):
                if cmd.startswith("rm -rf"):
                    deleted_paths.append(cmd.split("rm -rf ", 1)[1].strip())
                return (0, "", "")

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: _FakeSSH())

        orphaned_slugs = [
            {
                "slug": "db21ed7f_netalertx",
                "paths": [
                    "/mnt/data/supervisor/addons/db21ed7f_netalertx",
                    "/addon_configs/db21ed7f_netalertx",
                ],
                "size_display": "60M",
            }
        ]
        hitl_opts = [
            {
                "action_key": "cleanup_orphaned_addon_dirs",
                "orphaned_slugs": orphaned_slugs,
            }
        ]
        data = {
            "notification_id": "dr-orphan-1",
            "subject": "Disk recovery",
            "body": "b",
            "payload": {
                "card_type": "disk_recovery",
                "ranked_hitl_options": hitl_opts,
            },
            "sent_at": 0,
        }
        json_path = tmp_path / "dr-orphan-1.json"
        json_path.write_text(_json.dumps(data))

        asyncio.run(
            dashboard._execute_disk_recovery("dr-orphan-1", data, json_path, tmp_path)
        )

        assert (tmp_path / "dr-orphan-1.approved").exists()
        assert "/mnt/data/supervisor/addons/db21ed7f_netalertx" in deleted_paths
        assert "/addon_configs/db21ed7f_netalertx" in deleted_paths

    def test_cleanup_orphaned_addon_dirs_skips_installed_slugs_not_in_list(
        self, tmp_path, monkeypatch
    ):
        """cleanup_orphaned_addon_dirs only removes paths explicitly listed in orphaned_slugs."""
        import json as _json
        import web.dashboard as dashboard
        import utils.ha.ssh_client as ssh_mod
        import config as _config

        monkeypatch.setattr(_config, "HA_API_TOKEN", "")

        deleted_paths: list = []

        class _FakeSSH:
            async def run(self, cmd, check=False):
                if cmd.startswith("rm -rf"):
                    deleted_paths.append(cmd.split("rm -rf ", 1)[1].strip())
                return (0, "", "")

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: _FakeSSH())

        # Only the orphan slug is listed — installed slug must not be deleted
        orphaned_slugs = [
            {
                "slug": "db21ed7f_netalertx",
                "paths": ["/addon_configs/db21ed7f_netalertx"],
                "size_display": "10M",
            }
        ]
        hitl_opts = [
            {
                "action_key": "cleanup_orphaned_addon_dirs",
                "orphaned_slugs": orphaned_slugs,
            }
        ]
        data = {
            "notification_id": "dr-orphan-2",
            "subject": "s",
            "body": "b",
            "payload": {"card_type": "disk_recovery", "ranked_hitl_options": hitl_opts},
            "sent_at": 0,
        }
        json_path = tmp_path / "dr-orphan-2.json"
        json_path.write_text(_json.dumps(data))

        asyncio.run(
            dashboard._execute_disk_recovery("dr-orphan-2", data, json_path, tmp_path)
        )

        assert "/addon_configs/db21ed7f_netalertx" in deleted_paths
        assert not any("netalertx_fa" in p for p in deleted_paths)


class TestDiskQueueOrphanCleanup:
    """Tests for the POST /disk/queue-orphan-cleanup endpoint."""

    def test_returns_ok_with_card_queued(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        import utils.disk_recovery as dr_mod
        import utils.ha.ssh_client as ssh_mod
        import utils.notify as notify_mod
        import web.dashboard as dashboard
        import config as _config

        monkeypatch.setattr(_config, "NOTIFY_WATCH_DIR", str(tmp_path))

        from utils.disk_recovery import OrphanedAddonDir

        async def _fake_scan(ssh):
            return [
                OrphanedAddonDir(
                    slug="old_addon",
                    paths=["/mnt/data/supervisor/addons/old_addon"],
                    size_display="50M",
                    size_bytes=50 * 1024**2,
                )
            ]

        monkeypatch.setattr(dr_mod, "scan_orphaned_addon_dirs", _fake_scan)
        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        send_calls: list = []

        class _FakeNotifier:
            async def send(self, **kw):
                send_calls.append(kw)

        monkeypatch.setattr(
            notify_mod, "get_notifier", lambda *a, **kw: _FakeNotifier()
        )

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/disk/queue-orphan-cleanup")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert len(send_calls) == 1
        payload = send_calls[0]["payload"]
        assert payload["card_type"] == "disk_recovery"
        opts = payload["ranked_hitl_options"]
        assert any(o["action_key"] == "cleanup_orphaned_addon_dirs" for o in opts)

    def test_returns_ok_no_card_when_no_orphans(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        import utils.disk_recovery as dr_mod
        import utils.ha.ssh_client as ssh_mod
        import web.dashboard as dashboard

        async def _empty_scan(ssh):
            return []

        monkeypatch.setattr(dr_mod, "scan_orphaned_addon_dirs", _empty_scan)
        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/disk/queue-orphan-cleanup")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "No orphaned" in response.json()["message"]


# ── Overview tab + SSE /events (item 59) ─────────────────────────────────────


class TestOverviewRoute:
    """Tests for the new / overview page and /queue approval queue."""

    def test_overview_returns_200(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "nonexistent.db"))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/")
        assert response.status_code == 200
        assert "Pueo" in response.text

    def test_overview_shows_overview_tab_text(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "nonexistent.db"))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert "Monitoring Loops" in html
        assert "Overview" in html

    def test_overview_shows_pending_count(self, tmp_path, monkeypatch):
        import json as _json, time as _time
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "nonexistent.db"))
        (tmp_path / "card1.json").write_text(
            _json.dumps(
                {
                    "notification_id": "card1",
                    "subject": "s",
                    "body": "b",
                    "payload": {},
                    "sent_at": int(_time.time()),
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        # pending_count=1 should appear somewhere in the page
        assert "1" in html

    def test_queue_route_returns_200(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/queue")
        assert response.status_code == 200

    def test_queue_shows_pending_cards(self, tmp_path, monkeypatch):
        import json as _json, time as _time
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        (tmp_path / "qcard1.json").write_text(
            _json.dumps(
                {
                    "notification_id": "qcard1",
                    "subject": "Repair needed",
                    "body": "b",
                    "payload": {},
                    "sent_at": int(_time.time()),
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/queue").text
        assert "Repair needed" in html
        assert "PENDING" in html

    def test_nav_includes_queue_link(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "nonexistent.db"))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert 'href="/queue"' in html

    def test_overview_no_supervisor_shows_no_loops_message(self, tmp_path, monkeypatch):
        """When no supervisor is running, overview shows the no-loops placeholder."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "nonexistent.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", None)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert "supervisor mode" in html or "No loops running" in html

    def test_overview_with_supervisor_shows_loop_rows(self, tmp_path, monkeypatch):
        """When a supervisor with loops is registered, loop rows appear in the table."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "nonexistent.db"))

        fake_sv = type(
            "FakeSV",
            (),
            {
                "get_statuses": lambda self: [
                    sup_mod.LoopStatus(
                        name="ha_log_monitor", status="running", error_count=0
                    )
                ]
            },
        )()
        monkeypatch.setattr(sup_mod, "_supervisor_instance", fake_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert "ha_log_monitor" in html
        assert "running" in html


class TestLoopControlEndpoints:
    """Tests for POST /loops/{name}/pause|resume|run-now endpoints."""

    def _make_fake_sv(self, loop_names=("ha_log_monitor",)):
        """Return a minimal fake supervisor that records control calls."""
        import utils.agent.supervisor as sup_mod

        calls: list[tuple] = []

        def _ctrl(action):
            def method(self_inner, name):
                if name not in loop_names:
                    raise KeyError(name)
                calls.append((action, name))

            return method

        FakeSV = type(
            "FakeSV",
            (),
            {
                "get_statuses": lambda self: [
                    sup_mod.LoopStatus(name=n) for n in loop_names
                ],
                "pause": _ctrl("pause"),
                "resume": _ctrl("resume"),
                "run_now": _ctrl("run_now"),
            },
        )
        return FakeSV(), calls

    def test_pause_loop_returns_ok(self, tmp_path, monkeypatch):
        """POST /loops/{name}/pause returns 200 and calls sv.pause()."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        fake_sv, calls = self._make_fake_sv()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", fake_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        r = client.post("/loops/ha_log_monitor/pause")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert ("pause", "ha_log_monitor") in calls

    def test_resume_loop_returns_ok(self, tmp_path, monkeypatch):
        """POST /loops/{name}/resume returns 200 and calls sv.resume()."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        fake_sv, calls = self._make_fake_sv()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", fake_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        r = client.post("/loops/ha_log_monitor/resume")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert ("resume", "ha_log_monitor") in calls

    def test_run_now_loop_returns_ok(self, tmp_path, monkeypatch):
        """POST /loops/{name}/run-now returns 200 and calls sv.run_now()."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        fake_sv, calls = self._make_fake_sv()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", fake_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        r = client.post("/loops/ha_log_monitor/run-now")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert ("run_now", "ha_log_monitor") in calls

    def test_unknown_loop_returns_404(self, tmp_path, monkeypatch):
        """Unknown loop name returns 404 for all three control actions."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        fake_sv, _ = self._make_fake_sv()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", fake_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        assert client.post("/loops/nonexistent/pause").status_code == 404
        assert client.post("/loops/nonexistent/resume").status_code == 404
        assert client.post("/loops/nonexistent/run-now").status_code == 404

    def test_no_supervisor_returns_503(self, tmp_path, monkeypatch):
        """When no supervisor is registered, all loop control endpoints return 503."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", None)
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        assert client.post("/loops/ha_log_monitor/pause").status_code == 503
        assert client.post("/loops/ha_log_monitor/resume").status_code == 503
        assert client.post("/loops/ha_log_monitor/run-now").status_code == 503

    def test_overview_shows_controls_column(self, tmp_path, monkeypatch):
        """Overview page renders the Controls column with Pause/Run now buttons."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        fake_sv, _ = self._make_fake_sv(loop_names=("ha_log_monitor",))
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", fake_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert "Controls" in html
        assert "Pause" in html
        assert "Run now" in html

    def test_paused_loop_shows_resume_button(self, tmp_path, monkeypatch):
        """A paused loop renders a Resume button instead of Pause."""
        import utils.agent.supervisor as sup_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        paused_sv = type(
            "FakeSV",
            (),
            {
                "get_statuses": lambda self: [
                    sup_mod.LoopStatus(
                        name="ha_log_monitor", status="paused", paused=True
                    )
                ],
                "pause": lambda self, n: None,
                "resume": lambda self, n: None,
                "run_now": lambda self, n: None,
            },
        )()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(dashboard, "DB_PATH", str(tmp_path / "x.db"))
        monkeypatch.setattr(sup_mod, "_supervisor_instance", paused_sv)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert "Resume" in html


class TestSSEEventBus:
    """Tests for the SSE fan-out publish/subscribe mechanism."""

    def test_publish_event_reaches_subscriber(self):
        """An event published to the bus is delivered to a subscribed queue."""
        import asyncio
        from utils.agent.supervisor import publish_event, subscribe, unsubscribe

        async def run():
            q = subscribe()
            try:
                publish_event({"event_type": "loop_status", "loop": "test"})
                event = q.get_nowait()
                assert event["event_type"] == "loop_status"
                assert event["loop"] == "test"
            finally:
                unsubscribe(q)

        asyncio.run(run())

    def test_publish_event_reaches_multiple_subscribers(self):
        """All subscribed queues receive a copy of each published event."""
        import asyncio
        from utils.agent.supervisor import publish_event, subscribe, unsubscribe

        async def run():
            q1, q2 = subscribe(), subscribe()
            try:
                publish_event({"event_type": "resource", "disk_free_gb": 5.0})
                e1 = q1.get_nowait()
                e2 = q2.get_nowait()
                assert e1["event_type"] == "resource"
                assert e2["event_type"] == "resource"
            finally:
                unsubscribe(q1)
                unsubscribe(q2)

        asyncio.run(run())

    def test_unsubscribe_stops_delivery(self):
        """After unsubscribe(), the queue receives no further events."""
        import asyncio
        from utils.agent.supervisor import publish_event, subscribe, unsubscribe

        async def run():
            q = subscribe()
            unsubscribe(q)
            publish_event({"event_type": "loop_status", "loop": "gone"})
            assert q.empty()

        asyncio.run(run())

    def test_loop_supervisor_emits_to_subscribers(self):
        """LoopSupervisor._emit() publishes to SSE subscribers via publish_event."""
        import asyncio
        import utils.agent.supervisor as sup_mod
        from utils.agent.supervisor import LoopSupervisor, subscribe, unsubscribe

        async def run():
            q = subscribe()
            try:
                sv = LoopSupervisor()
                sv._handles["my_loop"] = sup_mod.LoopStatus(
                    name="my_loop", status="running"
                )
                sv._emit("my_loop")
                event = q.get_nowait()
                assert event["event_type"] == "loop_status"
                assert event["loop"] == "my_loop"
                assert event["status"] == "running"
            finally:
                unsubscribe(q)

        asyncio.run(run())

    def test_sse_events_endpoint_registered(self):
        """The /events route is registered in the FastAPI app."""
        import web.dashboard as dashboard

        route_paths = {getattr(r, "path", "") for r in dashboard.app.routes}
        assert "/events" in route_paths

    def test_sse_events_returns_streaming_response(self):
        """sse_events() returns a StreamingResponse with text/event-stream media type."""
        import asyncio
        from fastapi.responses import StreamingResponse
        from web.dashboard import sse_events

        async def run():
            resp = await sse_events()
            # The generator is lazy — just check the response object metadata
            assert isinstance(resp, StreamingResponse)
            assert resp.media_type == "text/event-stream"

        asyncio.run(run())


class TestLoadLastBackup:
    """Tests for the _load_last_backup helper."""

    def test_returns_never_when_db_missing(self, monkeypatch):
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "DB_PATH", "/nonexistent/path/test.db")
        result = dashboard._load_last_backup()
        assert result["age"] == "never"
        assert result["slug"] is None

    def test_returns_age_string_for_recent_backup(self, monkeypatch, tmp_path):
        import sqlite3, time
        from agents import ha_agent_advanced
        import web.dashboard as dashboard

        db = str(tmp_path / "test.db")
        monkeypatch.setattr(dashboard, "DB_PATH", db)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        ha_agent_advanced.init_local_database()

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, 'abc123', 'ACTIVE', 0, 'both')",
                (int(time.time()) - 120,),
            )
        result = dashboard._load_last_backup()
        assert result["slug"] == "abc123"
        assert "m ago" in result["age"] or "h ago" in result["age"]


class TestTimelineRoutes:
    """Tests for GET /timeline (list) and GET /timeline/{id} (detail)."""

    @pytest.fixture()
    def tl_setup(self, tmp_path, monkeypatch):
        """Migrated DB + patched DB_PATH in dashboard and timeline modules."""
        from agents import ha_agent_advanced
        import utils.core.timeline as tl_mod
        import web.dashboard as dashboard

        db = str(tmp_path / "tl_dash_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        monkeypatch.setattr(dashboard, "DB_PATH", db)
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        ha_agent_advanced.init_local_database()
        return db, tmp_path

    def test_timeline_empty_returns_200(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/timeline")
        assert response.status_code == 200
        assert "Timeline" in response.text

    def test_timeline_shows_events(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.core.timeline import write_timeline_event

        write_timeline_event("ERROR", "ha_log_monitor", "pycync mesh error")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/timeline").text
        assert "pycync mesh error" in html
        assert "ha_log_monitor" in html

    def test_timeline_level_filter(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.core.timeline import write_timeline_event

        write_timeline_event("INFO", "update_check", "update found")
        write_timeline_event("ERROR", "resource", "disk critical")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/timeline?level=ERROR").text
        assert "disk critical" in html
        assert "update found" not in html

    def test_timeline_pagination_headers(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.core.timeline import write_timeline_event

        for i in range(30):
            write_timeline_event("INFO", "src", f"event {i}")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/timeline").text
        assert "Page 1" in html
        assert "Next" in html

    def test_timeline_detail_returns_200(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.core.timeline import write_timeline_event

        eid = write_timeline_event(
            "WARN", "resource", "disk warn", {"disk_free_gb": 1.8}
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get(f"/timeline/{eid}")
        assert response.status_code == 200
        assert "disk warn" in response.text
        assert "resource" in response.text

    def test_timeline_detail_shows_detail_fields(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.core.timeline import write_timeline_event

        eid = write_timeline_event(
            "INFO",
            "update_check",
            "update available",
            {"component": "core", "latest_version": "2026.8.0"},
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get(f"/timeline/{eid}").text
        assert "component" in html
        assert "core" in html

    def test_timeline_detail_missing_id_returns_404(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/timeline/99999")
        assert response.status_code == 404

    def test_timeline_nav_link_present(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert 'href="/timeline"' in html

    def test_overview_recent_events_section_present(self, tl_setup):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        from utils.core.timeline import write_timeline_event

        write_timeline_event("ERROR", "ha_log_monitor", "recent error event")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert "recent error event" in html

    def test_resource_disk_critical_emits_timeline_event(self, tl_setup):
        """disk_critical alert writes a CRITICAL timeline event."""
        import asyncio
        import sqlite3
        import utils.core.timeline as tl_mod

        db, _ = tl_setup

        async def run():
            from utils.resource import (
                ResourcePoller,
                ResourceStatus,
                update_resource_status,
            )

            captured = []

            class FakeNotifier:
                async def send(self, subject, body, payload=None):
                    captured.append(subject)

            poller = ResourcePoller(
                ssh_client=None,  # type: ignore[arg-type]
                notifier=FakeNotifier(),
                interval_seconds=9999,
                disk_warn_gb=2.0,
                disk_critical_gb=3.0,
                mem_warn_mb=200,
                db_path=db,
            )
            status = ResourceStatus(
                disk_free_gb=1.0,
                disk_total_gb=32.0,
                disk_used_gb=31.0,
                mem_available_mb=500,
                mem_total_mb=8000,
                disk_warn=True,
                disk_critical=True,
                mem_warn=False,
            )
            await poller._check_and_alert(status)

        asyncio.run(run())

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT level, source FROM timeline_events WHERE level='CRITICAL'"
            ).fetchall()
        assert any(r[1] == "resource" for r in rows)


# ── Configuration editor (item 61) ───────────────────────────────────────────


class TestConfigEditor:
    """Tests for GET /settings and POST /config endpoints."""

    @pytest.fixture()
    def cfg_path(self, tmp_path, monkeypatch):
        """Point config._config_path at a fresh temp file for each test."""
        import config as _config

        p = tmp_path / "config.yaml"
        monkeypatch.setattr(_config, "_config_path", p)
        return p

    def test_settings_route_returns_200(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/settings")
        assert response.status_code == 200

    def test_settings_renders_all_groups(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/settings").text
        for group_name in (
            "Autonomy",
            "Monitoring intervals",
            "Thresholds",
            "Notifications",
            "HA Connection",
            "LLM Provider",
        ):
            assert (
                group_name in html
            ), f"group '{group_name}' missing from settings page"

    def test_settings_llm_provider_params_present(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/settings").text
        assert "llm_provider" in html
        assert "cloud_model" in html
        assert "cloud_max_cost_per_incident_usd" in html
        assert "cloud_max_daily_spend_usd" in html

    def test_settings_api_key_badge_not_set(self, tmp_path, monkeypatch):
        import config as _config
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(_config, "ANTHROPIC_API_KEY", "")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/settings").text
        assert "not set" in html

    def test_settings_api_key_badge_set(self, tmp_path, monkeypatch):
        import config as _config
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(_config, "ANTHROPIC_API_KEY", "sk-ant-fake")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/settings").text
        assert "set ✓" in html

    def test_config_update_llm_provider(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        import config as _config
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        # Writing "local" is always safe — no API key guard fires
        monkeypatch.setattr(_config, "ANTHROPIC_API_KEY", "")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post("/config", json={"key": "llm_provider", "value": "local"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["restart_required"] is True
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["llm"]["provider"] == "local"

    def test_config_update_cloud_model(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post(
            "/config", json={"key": "cloud_model", "value": "claude-opus-5"}
        )
        assert resp.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["cloud"]["model"] == "claude-opus-5"

    def test_config_update_billing_caps(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post(
            "/config",
            json={"key": "cloud_max_cost_per_incident_usd", "value": "1.5"},
        )
        assert resp.status_code == 200
        resp2 = client.post(
            "/config",
            json={"key": "cloud_max_daily_spend_usd", "value": "20.0"},
        )
        assert resp2.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert abs(cfg["cloud"]["max_cost_per_incident_usd"] - 1.5) < 1e-9
        assert abs(cfg["cloud"]["max_daily_spend_usd"] - 20.0) < 1e-9

    def test_settings_shows_autonomy_level_param(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/settings").text
        assert "autonomy_level" in html
        assert "ha_host" in html

    def test_settings_nav_link_present(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
        assert 'href="/settings"' in html

    def test_valid_int_update_writes_config_yaml(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/config", json={"key": "autonomy_level", "value": "3"})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["restart_required"] is False
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["agent"]["autonomy_level"] == 3

    def test_valid_int_update_sets_module_attr(self, cfg_path, tmp_path, monkeypatch):
        import config as _config
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/config", json={"key": "autonomy_level", "value": "4"})
        assert _config.AUTONOMY_LEVEL == 4

    def test_valid_float_update(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "log_confidence_threshold", "value": "0.85"}
        )
        assert response.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert abs(cfg["agent"]["log_confidence_threshold"] - 0.85) < 1e-9

    def test_valid_bool_update_true(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "self_healing_enabled", "value": "true"}
        )
        assert response.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["agent"]["self_healing_enabled"] is True

    def test_valid_bool_update_false(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "self_healing_enabled", "value": "false"}
        )
        assert response.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["agent"]["self_healing_enabled"] is False

    def test_valid_string_update(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "notify_url", "value": "https://ntfy.sh/my-topic"}
        )
        assert response.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["agent"]["notify_url"] == "https://ntfy.sh/my-topic"

    def test_connection_param_returns_restart_required(
        self, cfg_path, tmp_path, monkeypatch
    ):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "ha_host", "value": "192.168.1.10"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["restart_required"] is True

    def test_runtime_param_returns_no_restart(self, cfg_path, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "max_repairs_per_hour", "value": "5"}
        )
        assert response.status_code == 200
        assert response.json()["restart_required"] is False

    def test_unknown_key_returns_400(self, cfg_path, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/config", json={"key": "does_not_exist", "value": "1"})
        assert response.status_code == 400

    def test_below_min_returns_400(self, cfg_path, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/config", json={"key": "autonomy_level", "value": "0"})
        assert response.status_code == 400

    def test_above_max_returns_400(self, cfg_path, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/config", json={"key": "autonomy_level", "value": "5"})
        assert response.status_code == 400

    def test_invalid_int_value_returns_400(self, cfg_path, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/config", json={"key": "autonomy_level", "value": "notanumber"}
        )
        assert response.status_code == 400

    def test_invalid_option_returns_400(self, cfg_path, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/config", json={"key": "notifier", "value": "telegram"})
        assert response.status_code == 400

    def test_valid_option_accepted(self, cfg_path, tmp_path, monkeypatch):
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/config", json={"key": "notifier", "value": "ntfy"})
        assert response.status_code == 200
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["agent"]["notifier"] == "ntfy"

    def test_existing_yaml_keys_preserved(self, cfg_path, tmp_path, monkeypatch):
        """Writing one key must not erase sibling keys in config.yaml."""
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        cfg_path.write_text(
            yaml.dump({"agent": {"autonomy_level": 2, "log_level": "DEBUG"}})
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/config", json={"key": "autonomy_level", "value": "3"})
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["agent"]["autonomy_level"] == 3
        assert cfg["agent"]["log_level"] == "DEBUG"

    def test_ha_connection_section_written_correctly(
        self, cfg_path, tmp_path, monkeypatch
    ):
        """ha_host writes into home_assistant section, not agent section."""
        import yaml
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/config", json={"key": "ha_host", "value": "10.0.0.5"})
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["home_assistant"]["host"] == "10.0.0.5"
        assert "agent" not in cfg  # no contamination

    def test_config_update_model_valid(self):
        from web.dashboard import ConfigUpdateRequest

        req = ConfigUpdateRequest(key="autonomy_level", value="3")
        assert req.key == "autonomy_level"
        assert req.value == "3"

    def test_config_update_model_missing_field_raises(self):
        from pydantic import ValidationError
        from web.dashboard import ConfigUpdateRequest

        with pytest.raises(ValidationError):
            ConfigUpdateRequest(key="autonomy_level")  # type: ignore[call-arg]

    def test_editable_params_all_have_required_keys(self):
        from web.dashboard import _EDITABLE_PARAMS

        required = {
            "yaml_section",
            "yaml_key",
            "config_attr",
            "val_type",
            "description",
            "group",
        }
        for name, spec in _EDITABLE_PARAMS.items():
            missing = required - spec.keys()
            assert not missing, f"{name} missing keys: {missing}"


# ── launchd service helpers ───────────────────────────────────────────────────────


class TestLaunchdPlistTemplate:
    def test_render_plist_substitutes_all_placeholders(self, tmp_path):
        from utils.service import render_plist

        rendered = render_plist("/opt/pueo", "/opt/pueo/.venv/bin/python")
        assert "{{ PUEO_DIR }}" not in rendered
        assert "{{ PYTHON_PATH }}" not in rendered

    def test_render_plist_inserts_correct_paths(self, tmp_path):
        from utils.service import render_plist

        rendered = render_plist("/opt/pueo", "/opt/pueo/.venv/bin/python3")
        assert "/opt/pueo" in rendered
        assert "/opt/pueo/.venv/bin/python3" in rendered

    def test_render_plist_produces_valid_plist(self):
        import plistlib

        from utils.service import render_plist

        rendered = render_plist("/opt/pueo", "/usr/bin/python3")
        # plistlib.loads handles the DOCTYPE declaration that ElementTree cannot
        parsed = plistlib.loads(rendered.encode())
        assert isinstance(parsed, dict)

    def test_render_plist_has_correct_label(self):
        import plistlib

        from utils.service import render_plist

        rendered = render_plist("/opt/pueo", "/usr/bin/python3")
        plist = plistlib.loads(rendered.encode())
        assert plist["Label"] == "com.pueo.agent"

    def test_render_plist_has_keep_alive(self):
        import plistlib

        from utils.service import render_plist

        rendered = render_plist("/opt/pueo", "/usr/bin/python3")
        plist = plistlib.loads(rendered.encode())
        assert plist.get("KeepAlive") is True

    def test_render_plist_working_directory_matches_pueo_dir(self):
        import plistlib

        from utils.service import render_plist

        rendered = render_plist("/my/pueo/dir", "/usr/bin/python3")
        plist = plistlib.loads(rendered.encode())
        assert plist["WorkingDirectory"] == "/my/pueo/dir"

    def test_render_plist_program_arguments_include_main_py(self):
        import plistlib

        from utils.service import render_plist

        rendered = render_plist("/opt/pueo", "/opt/pueo/.venv/bin/python")
        plist = plistlib.loads(rendered.encode())
        args = plist["ProgramArguments"]
        assert args[0] == "/opt/pueo/.venv/bin/python"
        assert args[1] == "/opt/pueo/main.py"


class TestServiceStatus:
    def test_service_status_not_loaded_returns_false(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        def fake_run(*args, **kwargs):
            r = subprocess.CompletedProcess(args, 1)
            r.stdout = ""
            r.stderr = "Could not find service"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        status = svc.service_status()
        assert status["loaded"] is False
        assert status["running"] is False
        assert status["pid"] is None

    def test_service_status_loaded_running_with_pid(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        def fake_run(*args, **kwargs):
            r = subprocess.CompletedProcess(args, 0)
            r.stdout = '{\n\t"PID" = 9876;\n\t"Label" = "com.pueo.agent";\n};\n'
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        status = svc.service_status()
        assert status["loaded"] is True
        assert status["running"] is True
        assert status["pid"] == 9876

    def test_service_status_loaded_stopped_no_pid(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        def fake_run(*args, **kwargs):
            r = subprocess.CompletedProcess(args, 0)
            r.stdout = '{\n\t"Label" = "com.pueo.agent";\n\t"LastExitStatus" = 0;\n};\n'
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        status = svc.service_status()
        assert status["loaded"] is True
        assert status["running"] is False
        assert status["pid"] is None

    def test_service_status_non_darwin_returns_not_loaded(self, monkeypatch):
        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "linux"})())
        status = svc.service_status()
        assert status["loaded"] is False
        assert "error" in status

    def test_service_status_launchctl_not_found(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("launchctl not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        status = svc.service_status()
        assert status["loaded"] is False
        assert "error" in status

    def test_service_status_invalid_pid_value_handled(self, monkeypatch):
        """ValueError when PID field is non-numeric must be silently caught."""
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        def fake_run(*args, **kwargs):
            r = subprocess.CompletedProcess(args, 0)
            r.stdout = '{\n\t"PID" = not-a-number;\n\t"Label" = "com.pueo.agent";\n};\n'
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        status = svc.service_status()
        assert status["loaded"] is True
        assert status["running"] is False
        assert status["pid"] is None


class TestServiceActions:
    def test_install_service_writes_plist_and_loads(self, tmp_path, monkeypatch):
        import subprocess

        from utils import service as svc

        fake_target = tmp_path / "com.pueo.agent.plist"
        monkeypatch.setattr(svc, "PLIST_TARGET", fake_target)
        monkeypatch.setattr(
            svc,
            "sys",
            type(
                "_Sys", (), {"platform": "darwin", "executable": "/usr/bin/python3"}
            )(),
        )

        runs = []

        def fake_run(cmd, **kwargs):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        svc.install_service(
            pueo_dir="/opt/pueo", python_path="/opt/pueo/.venv/bin/python"
        )

        assert fake_target.exists()
        assert "com.pueo.agent" in fake_target.read_text()
        assert any("load" in str(cmd) for cmd in runs)

    def test_install_service_uses_defaults_when_args_omitted(
        self, tmp_path, monkeypatch
    ):
        """install_service() with no args resolves pueo_dir from template path."""
        import subprocess

        from utils import service as svc

        fake_target = tmp_path / "com.pueo.agent.plist"
        monkeypatch.setattr(svc, "PLIST_TARGET", fake_target)
        monkeypatch.setattr(
            svc,
            "sys",
            type(
                "_Sys", (), {"platform": "darwin", "executable": "/usr/bin/python3"}
            )(),
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        svc.install_service()

        assert fake_target.exists()

    def test_restart_service_calls_launchctl_stop(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())
        runs = []

        def fake_run(cmd, **kwargs):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        svc.restart_service()
        assert runs and "stop" in runs[0]

    def test_uninstall_service_unloads_and_removes_plist(self, tmp_path, monkeypatch):
        import subprocess

        from utils import service as svc

        fake_target = tmp_path / "com.pueo.agent.plist"
        fake_target.write_text("plist content")
        monkeypatch.setattr(svc, "PLIST_TARGET", fake_target)
        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        runs = []

        def fake_run(cmd, **kwargs):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        svc.uninstall_service()

        assert not fake_target.exists()
        assert any("unload" in str(cmd) for cmd in runs)

    def test_uninstall_service_no_op_when_plist_missing(self, tmp_path, monkeypatch):
        """uninstall_service does nothing if plist was never installed."""
        import subprocess

        from utils import service as svc

        fake_target = tmp_path / "com.pueo.agent.plist"
        monkeypatch.setattr(svc, "PLIST_TARGET", fake_target)
        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())

        runs = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: runs.append(cmd))
        svc.uninstall_service()

        assert len(runs) == 0

    def test_stop_service_calls_launchctl_unload(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())
        runs = []

        def fake_run(cmd, **kwargs):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        svc.stop_service()
        assert runs and "unload" in runs[0]

    def test_start_service_calls_launchctl_load(self, monkeypatch):
        import subprocess

        from utils import service as svc

        monkeypatch.setattr(svc, "sys", type("_Sys", (), {"platform": "darwin"})())
        runs = []

        def fake_run(cmd, **kwargs):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        svc.start_service()
        assert runs and "load" in runs[0]


class TestDashboardServiceEndpoints:
    def test_get_service_status_returns_dict(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )
        client = TestClient(dashboard.app)
        resp = client.get("/service/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "loaded" in data
        assert "running" in data

    def test_settings_tab_includes_service_key(self, tmp_path, monkeypatch):
        """GET /settings renders without error and passes service context."""
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": True, "running": True, "pid": 1234},
        )
        client = TestClient(dashboard.app)
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"Service" in resp.content

    def test_service_install_calls_install_service(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        called = []
        monkeypatch.setattr(svc, "install_service", lambda **kw: called.append(kw))
        client = TestClient(dashboard.app)
        resp = client.post("/service/install")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(called) == 1

    def test_service_install_error_returns_500(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "install_service",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("macOS only")),
        )
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post("/service/install")
        assert resp.status_code == 500

    def test_service_restart_calls_restart_service(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        called = []
        monkeypatch.setattr(svc, "restart_service", lambda: called.append(1))
        client = TestClient(dashboard.app)
        resp = client.post("/service/restart")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(called) == 1

    def test_service_uninstall_calls_uninstall_service(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        called = []
        monkeypatch.setattr(svc, "uninstall_service", lambda: called.append(1))
        client = TestClient(dashboard.app)
        resp = client.post("/service/uninstall")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(called) == 1

    def test_service_stop_calls_stop_service(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        called = []
        monkeypatch.setattr(svc, "stop_service", lambda: called.append(1))
        client = TestClient(dashboard.app)
        resp = client.post("/service/stop")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(called) == 1

    def test_service_start_calls_start_service(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        called = []
        monkeypatch.setattr(svc, "start_service", lambda: called.append(1))
        client = TestClient(dashboard.app)
        resp = client.post("/service/start")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(called) == 1

    def test_service_stop_error_returns_500(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "stop_service",
            lambda: (_ for _ in ()).throw(RuntimeError("macOS only")),
        )
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post("/service/stop")
        assert resp.status_code == 500


class TestControlTab:
    def test_control_page_renders(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": True, "running": True, "pid": 1234},
        )
        monkeypatch.setattr(svc, "PLIST_TARGET", tmp_path / "com.pueo.agent.plist")
        client = TestClient(dashboard.app)
        resp = client.get("/control")
        assert resp.status_code == 200
        assert b"Pueo Service" in resp.content
        assert b"Run a Mode" in resp.content

    def test_control_page_shows_install_button_when_no_plist(
        self, tmp_path, monkeypatch
    ):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )
        monkeypatch.setattr(svc, "PLIST_TARGET", tmp_path / "missing.plist")
        client = TestClient(dashboard.app)
        resp = client.get("/control")
        assert resp.status_code == 200
        assert b"Install service" in resp.content

    def test_control_run_valid_mode_returns_ok(self, tmp_path, monkeypatch):
        import asyncio

        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )

        launched: list[str] = []

        async def fake_subprocess(*args, **kwargs):
            launched.append(args[1] if len(args) > 1 else "")

            class _Proc:
                async def wait(self):
                    pass

            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        client = TestClient(dashboard.app)
        resp = client.post("/control/run?mode=audit")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["mode"] == "audit"

    def test_control_run_invalid_mode_returns_400(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post("/control/run?mode=rm_rf")
        assert resp.status_code == 400

    def test_control_nav_link_active_on_control_page(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.dashboard as dashboard
        from utils import service as svc

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )
        monkeypatch.setattr(svc, "PLIST_TARGET", tmp_path / "missing.plist")
        client = TestClient(dashboard.app)
        resp = client.get("/control")
        assert resp.status_code == 200
        assert b"nav-link active" in resp.content


# ── Phase 12.5 — Update Ordering Guard ────────────────────────────────────────


class TestUpdateOrderingGuard:
    """The approve() endpoint must block approving a lower-priority update when
    a higher-priority update card is still PENDING in the watch directory."""

    def _write_update_card(self, watch_dir: Path, nid: str, component: str) -> None:
        import json

        from utils.card_types import CARD_TYPE_UPDATE

        card = {
            "subject": f"Update: {component}",
            "body": "",
            "payload": {
                "card_type": CARD_TYPE_UPDATE,
                "component": component,
                "entity_id": f"update.{component}",
                "installed_version": "1.0",
                "latest_version": "2.0",
            },
        }
        (watch_dir / f"{nid}.json").write_text(json.dumps(card))

    def test_order_error_redirects_when_higher_priority_pending(
        self, tmp_path, monkeypatch
    ):
        import web.dashboard as dashboard
        from starlette.testclient import TestClient

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        os_nid = "os-card-1111"
        core_nid = "core-card-2222"
        self._write_update_card(watch_dir, os_nid, "os")
        self._write_update_card(watch_dir, core_nid, "core")

        client = TestClient(dashboard.app, follow_redirects=False)
        resp = client.post(f"/approve/{core_nid}")
        assert resp.status_code == 303
        assert "order_error=os" in resp.headers["location"]

    def test_approve_proceeds_when_no_higher_priority_pending(
        self, tmp_path, monkeypatch
    ):
        import json
        from unittest.mock import AsyncMock, patch

        import web.dashboard as dashboard
        from starlette.testclient import TestClient

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        os_nid = "os-card-1111"
        self._write_update_card(watch_dir, os_nid, "os")

        with patch(
            "web.dashboard._execute_queued_update", new_callable=AsyncMock
        ) as mock_exec:
            client = TestClient(dashboard.app, follow_redirects=False)
            resp = client.post(f"/approve/{os_nid}")

        assert resp.status_code == 303
        # Should NOT have an order_error param
        assert "order_error" not in resp.headers.get("location", "")

    def test_order_error_banner_rendered_in_queue(self, tmp_path, monkeypatch):
        import web.dashboard as dashboard
        from starlette.testclient import TestClient

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        client = TestClient(dashboard.app)
        resp = client.get("/queue?order_error=os")
        assert resp.status_code == 200
        assert b"order-banner" in resp.content
        assert b"os" in resp.content


class TestExecuteQueuedHARepair:
    def _write_repair_card(
        self, watch_dir: Path, nid: str, action: str = "dismiss"
    ) -> None:
        import json

        from utils.card_types import CARD_TYPE_HA_REPAIR

        card = {
            "subject": "HA repair: homeassistant/reboot_required",
            "body": "Reboot required",
            "payload": {
                "card_type": CARD_TYPE_HA_REPAIR,
                "action": action,
                "domain": "homeassistant",
                "issue_id": "reboot_required",
                "issue_key": "homeassistant/reboot_required",
                "severity": "CRITICAL",
            },
        }
        (watch_dir / f"{nid}.json").write_text(json.dumps(card))

    def test_dismiss_action_calls_dismiss_and_marks_resolved(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from agents import ha_agent_advanced
        import utils.ha.ha_rest_client as hrc
        from unittest.mock import AsyncMock, MagicMock

        import web.dashboard as dashboard

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        nid = "repair-test-1"
        self._write_repair_card(watch_dir, nid, action="dismiss")
        json_path = watch_dir / f"{nid}.json"
        data = __import__("json").loads(json_path.read_text())

        mock_dismiss = AsyncMock()
        mock_resolve = MagicMock()
        fake_rest = hrc.FakeHARestClient()

        monkeypatch.setattr(hrc, "HARestClient", lambda *a, **kw: fake_rest)
        monkeypatch.setattr(hrc, "dismiss_ha_repair_issue", mock_dismiss)
        monkeypatch.setattr(ha_agent_advanced, "mark_repair_resolved", mock_resolve)

        asyncio.run(
            dashboard._execute_queued_ha_repair(nid, data, json_path, watch_dir)
        )

        mock_dismiss.assert_called_once()
        mock_resolve.assert_called_once_with("homeassistant/reboot_required")
        assert (watch_dir / f"{nid}.approved").exists()
        assert not (watch_dir / f"{nid}.in_progress").exists()

    def test_repair_card_approved_via_queue(self, tmp_path, monkeypatch):
        """Approve endpoint dispatches ha_repair cards to _execute_queued_ha_repair."""
        import asyncio
        import json

        import web.dashboard as dashboard
        from starlette.testclient import TestClient
        from utils.card_types import CARD_TYPE_HA_REPAIR

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        nid = "repair-queue-1"
        self._write_repair_card(watch_dir, nid, action="dismiss")

        dispatched = []

        async def fake_handler(nid_, data_, jpath_, wdir_):
            dispatched.append(nid_)

        monkeypatch.setitem(dashboard._CARD_DISPATCH, CARD_TYPE_HA_REPAIR, fake_handler)

        client = TestClient(dashboard.app, follow_redirects=False)
        resp = client.post(f"/approve/{nid}")

        assert resp.status_code == 303
        assert nid in dispatched

    def test_reboot_action_triggers_post_reboot_scan(self, tmp_path, monkeypatch):
        """After a successful reboot, _post_reboot_repair_scan is scheduled as a task."""
        import asyncio
        from agents import ha_agent_advanced
        from unittest.mock import AsyncMock, MagicMock, patch

        import web.dashboard as dashboard
        from utils.ha.ssh_client import AsyncSSHClient
        from utils.notify import get_notifier

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        nid = "repair-reboot-scan-1"
        self._write_repair_card(watch_dir, nid, action="reboot")
        json_path = watch_dir / f"{nid}.json"
        data = __import__("json").loads(json_path.read_text())

        tasks_created = []

        def fake_create_task(coro, **kwargs):
            tasks_created.append(
                coro.__name__ if hasattr(coro, "__name__") else str(coro)
            )
            # Close the coroutine to avoid ResourceWarning
            coro.close()
            return MagicMock()

        mock_resolve = MagicMock()
        monkeypatch.setattr(ha_agent_advanced, "mark_repair_resolved", mock_resolve)
        monkeypatch.setattr(dashboard.asyncio, "create_task", fake_create_task)

        with patch(
            "agents.ha_update_manager.execute_ha_reboot",
            new=AsyncMock(return_value=True),
        ):
            asyncio.run(
                dashboard._execute_queued_ha_repair(nid, data, json_path, watch_dir)
            )

        assert (watch_dir / f"{nid}.approved").exists()
        # A post-reboot scan task must have been scheduled.
        assert len(tasks_created) == 1

    def test_reboot_failure_does_not_trigger_post_reboot_scan(
        self, tmp_path, monkeypatch
    ):
        """If the reboot fails, no post-reboot scan task is created."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        import web.dashboard as dashboard

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        nid = "repair-reboot-fail-1"
        self._write_repair_card(watch_dir, nid, action="reboot")
        json_path = watch_dir / f"{nid}.json"
        data = __import__("json").loads(json_path.read_text())

        tasks_created = []

        def fake_create_task(coro, **kwargs):
            tasks_created.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(dashboard.asyncio, "create_task", fake_create_task)

        with patch(
            "agents.ha_update_manager.execute_ha_reboot",
            new=AsyncMock(return_value=False),
        ):
            asyncio.run(
                dashboard._execute_queued_ha_repair(nid, data, json_path, watch_dir)
            )

        assert (watch_dir / f"{nid}.rejected").exists()
        assert len(tasks_created) == 0

    def test_restart_action_calls_core_restart_and_marks_resolved(
        self, tmp_path, monkeypatch
    ):
        """action='restart' SSHes ha core restart, polls for HA API ready, writes .approved."""
        import asyncio
        from agents import ha_agent_advanced
        import utils.ha.ssh_client as ssh_mod
        from unittest.mock import AsyncMock, MagicMock, patch

        import web.dashboard as dashboard

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        nid = "repair-restart-ok-1"
        self._write_repair_card(watch_dir, nid, action="restart")
        json_path = watch_dir / f"{nid}.json"
        data = __import__("json").loads(json_path.read_text())

        tasks_created = []

        def fake_create_task(coro, **kwargs):
            tasks_created.append(
                coro.__name__ if hasattr(coro, "__name__") else str(coro)
            )
            coro.close()
            return MagicMock()

        mock_resolve = MagicMock()
        fake_ssh = AsyncMock()
        fake_ssh.run = AsyncMock(return_value=(0, "", ""))

        monkeypatch.setattr(ha_agent_advanced, "mark_repair_resolved", mock_resolve)
        monkeypatch.setattr(dashboard.asyncio, "create_task", fake_create_task)
        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: fake_ssh)

        with patch(
            "agents.ha_update_manager._poll_ha_api_ready",
            new=AsyncMock(return_value=True),
        ):
            asyncio.run(
                dashboard._execute_queued_ha_repair(nid, data, json_path, watch_dir)
            )

        fake_ssh.run.assert_called_once_with("ha core restart")
        mock_resolve.assert_called_once_with("homeassistant/reboot_required")
        assert (watch_dir / f"{nid}.approved").exists()
        assert len(tasks_created) == 1

    def test_restart_action_failure_writes_rejected(self, tmp_path, monkeypatch):
        """If HA does not come back online after restart, .rejected is written."""
        import asyncio
        import utils.ha.ssh_client as ssh_mod
        from unittest.mock import AsyncMock, MagicMock, patch

        import web.dashboard as dashboard

        watch_dir = tmp_path / "hitl"
        watch_dir.mkdir()
        nid = "repair-restart-fail-1"
        self._write_repair_card(watch_dir, nid, action="restart")
        json_path = watch_dir / f"{nid}.json"
        data = __import__("json").loads(json_path.read_text())

        fake_ssh = AsyncMock()
        fake_ssh.run = AsyncMock(return_value=(0, "", ""))
        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: fake_ssh)

        with patch(
            "agents.ha_update_manager._poll_ha_api_ready",
            new=AsyncMock(return_value=False),
        ):
            asyncio.run(
                dashboard._execute_queued_ha_repair(nid, data, json_path, watch_dir)
            )

        assert (watch_dir / f"{nid}.rejected").exists()
        assert not (watch_dir / f"{nid}.approved").exists()


class TestPostRebootRepairScan:
    def test_calls_supervisor_run_now(self, monkeypatch):
        """_post_reboot_repair_scan triggers run_now('repair_poll') on the supervisor."""
        import asyncio
        from unittest.mock import MagicMock

        import web.dashboard as dashboard

        mock_sv = MagicMock()
        import utils.agent.supervisor as _sup

        monkeypatch.setattr(_sup, "get_supervisor_instance", lambda: mock_sv)

        asyncio.run(dashboard._post_reboot_repair_scan(settle_seconds=0))

        mock_sv.run_now.assert_called_once_with("repair_poll")

    def test_handles_supervisor_unavailable(self, monkeypatch):
        """_post_reboot_repair_scan does not raise when the supervisor is None."""
        import asyncio

        import web.dashboard as dashboard
        import utils.agent.supervisor as _sup

        monkeypatch.setattr(_sup, "get_supervisor_instance", lambda: None)

        # Should not raise.
        asyncio.run(dashboard._post_reboot_repair_scan(settle_seconds=0))


# ── Disk usage routes ─────────────────────────────────────────────────────────────


class TestDiskRoutes:
    def test_disk_get_no_data_returns_200(self, monkeypatch):
        import utils.disk_usage as du_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/disk")
        assert response.status_code == 200
        assert "No disk usage data" in response.text

    def test_disk_get_with_data_shows_sections(self, monkeypatch):
        import utils.disk_usage as du_mod
        from utils.disk_usage import DiskBreakdown, DiskSection
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        import time

        section = DiskSection(
            title="HA Config & Database",
            items=[],
            total_bytes=0,
            total_human="0 B",
            is_empty=True,
        )
        bd = DiskBreakdown(
            sections=[section],
            disk_used_gb=10.8,
            disk_total_gb=13.6,
            disk_free_gb=2.8,
            disk_used_pct=79.4,
            fetched_at=time.time(),
        )
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", bd)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/disk")
        assert response.status_code == 200
        assert (
            "HA Config &amp; Database" in response.text
            or "HA Config & Database" in response.text
        )
        assert "79.4" in response.text

    def test_disk_get_shows_age_seconds(self, monkeypatch):
        import utils.disk_usage as du_mod
        from utils.disk_usage import DiskBreakdown
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard
        import time

        bd = DiskBreakdown(fetched_at=time.time() - 30)
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", bd)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/disk")
        assert response.status_code == 200
        assert "ago" in response.text

    def test_disk_refresh_calls_fetch(self, monkeypatch):
        import utils.disk_usage as du_mod
        from utils.disk_usage import DiskBreakdown
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        fetch_calls = []

        async def _fake_fetch(ssh):
            fetch_calls.append(True)
            return DiskBreakdown(fetched_at=1.0)

        monkeypatch.setattr(du_mod, "fetch_disk_breakdown", _fake_fetch)
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)

        # Patch AsyncSSHClient to avoid real SSH
        import utils.ha.ssh_client as _sc
        from utils.ha.ssh_client import FakeSSHClient

        monkeypatch.setattr(_sc, "AsyncSSHClient", lambda *a, **kw: FakeSSHClient())

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post("/disk/refresh")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert len(fetch_calls) == 1

    def test_disk_refresh_error_returns_not_ok(self, monkeypatch):
        import utils.disk_usage as du_mod
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        async def _failing_fetch(ssh):
            raise OSError("SSH connection refused")

        monkeypatch.setattr(du_mod, "fetch_disk_breakdown", _failing_fetch)

        import utils.ha.ssh_client as _sc
        from utils.ha.ssh_client import FakeSSHClient

        monkeypatch.setattr(_sc, "AsyncSSHClient", lambda *a, **kw: FakeSSHClient())

        client = TestClient(dashboard.app, raise_server_exceptions=False)
        response = client.post("/disk/refresh")
        assert response.status_code == 500
        assert response.json()["ok"] is False
        assert "SSH connection refused" in response.json()["error"]


# ── Episodes tab ─────────────────────────────────────────────────────────────────


class TestEpisodesTab:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard
        from agents import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(dashboard, "DB_PATH", path)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _insert_episode(self, db_path, **overrides):
        import json
        import time
        import sqlite3

        defaults = dict(
            id="ep-test-1",
            timestamp=time.time(),
            trigger="ha_log",
            symptoms=json.dumps(["Error in sensor"]),
            tool_sequence=json.dumps([{"name": "read_config", "arguments": {}}]),
            hypothesis_chain=json.dumps(["Config may be wrong"]),
            fix_applied=None,
            verification_result=1,
            model_used="qwen2.5-coder:7b",
            escalated=0,
            duration_seconds=12.5,
        )
        defaults.update(overrides)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO repair_episodes"
                " (id, timestamp, trigger, symptoms, tool_sequence, hypothesis_chain,"
                "  fix_applied, verification_result, model_used, escalated, duration_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    defaults["id"],
                    defaults["timestamp"],
                    defaults["trigger"],
                    defaults["symptoms"],
                    defaults["tool_sequence"],
                    defaults["hypothesis_chain"],
                    defaults["fix_applied"],
                    defaults["verification_result"],
                    defaults["model_used"],
                    defaults["escalated"],
                    defaults["duration_seconds"],
                ),
            )
            conn.commit()

    def test_episodes_route_returns_200(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/episodes")
        assert resp.status_code == 200

    def test_empty_episodes_shows_message(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes").text
        assert "no repair episodes" in html.lower()

    def test_episode_row_renders_trigger_and_model(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_episode(db_path)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes").text
        assert "ha_log" in html
        assert "qwen2.5-coder:7b" in html

    def test_trigger_filter_limits_results(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_episode(db_path, id="ep1", trigger="ha_log")
        self._insert_episode(db_path, id="ep2", trigger="netalertx")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes?trigger=netalertx").text
        assert "netalertx" in html
        assert html.count("ha_log") <= 2  # may appear in select options

    def test_export_endpoint_returns_yaml(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard
        import yaml

        self._insert_episode(db_path)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/episodes/export")
        assert resp.status_code == 200
        assert "application/x-yaml" in resp.headers["content-type"]
        parsed = yaml.safe_load(resp.content)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_export_endpoint_no_episodes_returns_comment(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/episodes/export")
        assert resp.status_code == 200
        assert b"No episodes" in resp.content

    def test_export_endpoint_since_filter(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard
        import yaml

        self._insert_episode(db_path, id="old", timestamp=1000.0)
        self._insert_episode(db_path, id="new", timestamp=9999999999.0)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/episodes/export?since=2030-01-01")
        parsed = yaml.safe_load(resp.content)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "new"

    def test_export_endpoint_invalid_date_returns_400(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/episodes/export?since=not-a-date")
        assert resp.status_code == 400


class TestEpisodePrepareAndSubmit:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard
        from agents import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(dashboard, "DB_PATH", path)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _insert_episode(self, db_path, episode_id="ep-sub-1"):
        import json
        import sqlite3
        import time

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO repair_episodes"
                " (id, timestamp, trigger, symptoms, tool_sequence, hypothesis_chain,"
                "  fix_applied, verification_result, model_used, escalated, duration_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    time.time(),
                    "ha_log",
                    json.dumps(["sensor offline"]),
                    json.dumps([{"name": "read_logs", "arguments": {}}]),
                    json.dumps(["maybe timeout"]),
                    None,
                    1,
                    "qwen2.5-coder:7b",
                    0,
                    7.3,
                ),
            )
            conn.commit()

    def test_prepare_404_for_unknown_episode(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.get("/episodes/nonexistent-id/prepare")
        assert resp.status_code == 404

    def test_prepare_renders_yaml_preview(self, db_path, monkeypatch):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "owner/pueo-cases")
        self._insert_episode(db_path)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes/ep-sub-1/prepare").text
        assert "trigger" in html
        assert "yaml" in html.lower() or "YAML" in html

    def test_prepare_shows_repo_not_configured_warning(self, db_path, monkeypatch):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "")
        self._insert_episode(db_path)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes/ep-sub-1/prepare").text
        assert "not configured" in html.lower()

    def test_submit_returns_400_when_repo_not_configured(self, db_path, monkeypatch):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "")
        self._insert_episode(db_path)
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post(
            "/episodes/ep-sub-1/submit",
            json={"description": ""},
        )
        assert resp.status_code == 400

    def test_submit_404_for_unknown_episode(self, db_path, monkeypatch):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "owner/pueo-cases")
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post(
            "/episodes/nonexistent-id/submit",
            json={"description": ""},
        )
        assert resp.status_code == 404

    def test_submit_returns_already_submitted_for_duplicate(self, db_path, monkeypatch):
        import sqlite3
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "owner/pueo-cases")
        self._insert_episode(db_path)
        # Pre-mark as submitted
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE repair_episodes SET submitted_at = 1.0, pr_url = 'https://github.com/x/y/pull/1' WHERE id = 'ep-sub-1'"
            )
            conn.commit()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post(
            "/episodes/ep-sub-1/submit",
            json={"description": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["already_submitted"] is True
        assert data["pr_url"] == "https://github.com/x/y/pull/1"

    def test_submit_calls_case_submitter_and_marks_submitted(
        self, db_path, monkeypatch
    ):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "owner/pueo-cases")
        self._insert_episode(db_path)

        async def _fake_submit(episode_id, yaml_content, cases_repo, pr_title, pr_body):
            return "https://github.com/owner/pueo-cases/pull/99"

        # Dashboard imports submit_episode at call time via `from utils.case_submitter import ...`
        monkeypatch.setattr("utils.case_submitter.submit_episode", _fake_submit)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post(
            "/episodes/ep-sub-1/submit",
            json={"description": "context note"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "pr_url" in data
        assert data["pr_url"] == "https://github.com/owner/pueo-cases/pull/99"

        # Verify the episode is now marked submitted in DB
        from utils.repair.repair_episode import load_episode

        ep = load_episode(db_path, "ep-sub-1")
        assert ep is not None
        assert ep.submitted_at is not None
        assert ep.pr_url == "https://github.com/owner/pueo-cases/pull/99"

    def test_episodes_list_shows_prepare_button_for_unsubmitted(self, db_path):
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_episode(db_path)
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes").text
        assert "Prepare" in html

    def test_episodes_list_shows_submitted_link_for_submitted(self, db_path):
        import sqlite3
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        self._insert_episode(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE repair_episodes SET submitted_at = 1.0, pr_url = 'https://github.com/x/y/pull/3' WHERE id = 'ep-sub-1'"
            )
            conn.commit()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/episodes").text
        assert "submitted" in html.lower()
        assert "https://github.com/x/y/pull/3" in html


# ---------------------------------------------------------------------------
# TestSanitizeArgsPreview — transparency Chunk 1 (issue #264)
# ---------------------------------------------------------------------------


class TestSanitizeArgsPreview:
    """_sanitize_args_preview redacts sensitive keys and truncates long values."""

    def _fn(self, args):
        from web.dashboard import _sanitize_args_preview

        return _sanitize_args_preview(args)

    def test_normal_args_rendered(self):
        out = self._fn({"command": "df -h", "host": "ha.local"})
        assert "command=df -h" in out
        assert "host=ha.local" in out

    def test_sensitive_key_redacted(self):
        out = self._fn({"api_key": "s3cr3t", "token": "abc"})
        assert "s3cr3t" not in out
        assert "abc" not in out
        assert "***" in out

    def test_long_value_truncated_to_40(self):
        out = self._fn({"q": "x" * 60})
        assert len(out) <= 130  # 40 chars value + key + prefix + some slack

    def test_output_truncated_to_120(self):
        args = {f"k{i}": f"val{i}" for i in range(30)}
        out = self._fn(args)
        assert len(out) <= 120

    def test_empty_args(self):
        assert self._fn({}) == ""
