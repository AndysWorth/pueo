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
    import ha_agent_advanced
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
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "bbb")
        (tmp_path / "bbb.approved").touch()
        results = _load_requests(tmp_path)
        assert results[0].status == "APPROVED"

    def test_rejected_signal_yields_rejected_status(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "ccc")
        (tmp_path / "ccc.rejected").touch()
        results = _load_requests(tmp_path)
        assert results[0].status == "REJECTED"

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

    def test_pending_sorted_before_resolved(self, tmp_path):
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "p1")
        self._write_request(tmp_path, "r1")
        (tmp_path / "r1.approved").touch()
        results = _load_requests(tmp_path)
        statuses = [r.status for r in results]
        assert statuses[0] == "PENDING"
        assert statuses[1] == "APPROVED"


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
        response = client.get("/")
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
        import ha_agent_sandbox_engine
        import ha_agent_advanced

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
        import utils.ssh_client as ssh_mod
        from utils.ssh_client import FakeSSHClient

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
        import ha_agent_sandbox_engine
        import ha_agent_advanced

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
        import utils.ssh_client as ssh_mod
        from utils.ssh_client import FakeSSHClient

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
        import ha_agent_sandbox_engine

        nid = "fix-internal-3"
        json_path = tmp_path / f"{nid}.json"
        data: dict = {}
        json_path.write_text(_json.dumps(data))

        async def _fail(*args, **kwargs):
            raise RuntimeError("backup exploded")

        monkeypatch.setattr(ha_agent_sandbox_engine, "execute_remote_backup", _fail)
        import utils.ssh_client as ssh_mod
        from utils.ssh_client import FakeSSHClient

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
        from utils.ssh_client import FakeSSHClient
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
        from utils.ssh_client import FakeSSHClient
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
        from utils.ssh_client import FakeSSHClient
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
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
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
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
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
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from utils.autonomy import FakeAutonomyGate
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
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from utils.autonomy import FakeAutonomyGate
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
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from utils.autonomy import FakeAutonomyGate
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

        from utils.ollama_client import FakeLLMClient
        from ha_agent_core import DiagnosticsReport, analyze_config_locally

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

        from utils.ollama_client import FakeLLMClient
        from ha_log_monitor import LogEvaluation, analyze_log_line_with_ai

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

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient
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

        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        import ha_agent_sandbox_engine

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

        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        import ha_agent_sandbox_engine

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

        from utils.ollama_client import FakeLLMClient
        from ha_log_monitor import analyze_log_line_with_ai

        broken_llm = FakeLLMClient("{not valid json}")
        _result, trace = asyncio.run(
            analyze_log_line_with_ai(["ERROR crash"], broken_llm)
        )
        assert trace.raw_response == ""


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
        html = client.get("/").text
        assert "Evidence" in html
        assert "addon_info" in html
        assert "state: stopped" in html

    def test_evidence_section_absent_when_missing(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "ev2", {"severity": "HIGH"})
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        html = client.get("/").text
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
        html = client.get("/").text
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
        html = client.get("/").text
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
        html = client.get("/").text
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
        html = client.get("/").text
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
        import ha_agent_advanced

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

    def test_defer_shows_deferred_status(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_request(tmp_path, "upd2")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/defer/upd2", follow_redirects=False)
        response = client.get("/")
        assert "DEFERRED" in response.text

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
        import utils.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(
            utils.ha_rest_client, "HARestClient", lambda *a, **kw: FakeHARestClient()
        )
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.post(
            "/dismiss-notification/notif_http_login", follow_redirects=False
        )
        assert response.status_code == 303
        assert (tmp_path / "notif_http_login.approved").exists()

    def test_dismiss_calls_ha_service(self, tmp_path, monkeypatch):
        import utils.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        fake_rest = FakeHARestClient()
        monkeypatch.setattr(
            utils.ha_rest_client, "HARestClient", lambda *a, **kw: fake_rest
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

    def test_dismiss_ha_service_exception_is_swallowed(self, tmp_path, monkeypatch):
        """If the HA service call raises, the notification is still marked approved."""
        import utils.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        self._write_notification_card(tmp_path, "notif_exc", "http_login")

        class _FailingRest:
            async def call_service(self, *a, **kw):
                raise RuntimeError("HA unreachable")

        monkeypatch.setattr(
            utils.ha_rest_client, "HARestClient", lambda *a, **kw: _FailingRest()
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/dismiss-notification/notif_exc", follow_redirects=False)
        assert (tmp_path / "notif_exc.approved").exists()

    def test_dismiss_already_resolved_is_noop(self, tmp_path, monkeypatch):
        import utils.ha_rest_client
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient
        from utils.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path))
        fake_rest = FakeHARestClient()
        monkeypatch.setattr(
            utils.ha_rest_client, "HARestClient", lambda *a, **kw: fake_rest
        )
        self._write_notification_card(tmp_path, "notif_http_login", "http_login")
        (tmp_path / "notif_http_login.rejected").touch()
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        client.post("/dismiss-notification/notif_http_login", follow_redirects=False)
        assert not fake_rest.service_calls

    def test_notification_card_shows_dismiss_button(self, tmp_path, monkeypatch):
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
        response = client.get("/")
        assert "Dismiss in HA" in response.text
        assert "Keep" in response.text

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
                    "payload": {"severity": "MEDIUM"},
                    "sent_at": int(_time.time()) - 10,
                }
            )
        )
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        response = client.get("/")
        assert "Approve" in response.text
        assert "Reject" in response.text
        assert "Defer" in response.text


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
        from web.dashboard import _load_requests

        self._write_request(tmp_path, "ddd")
        (tmp_path / "ddd.deferred").touch()
        (tmp_path / "ddd.rejected").touch()
        results = _load_requests(tmp_path)
        assert results[0].status == "DEFERRED"

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
        import ha_agent_advanced

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
        asyncio.run(dashboard.approve("r1"))
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
        asyncio.run(dashboard.approve("legacy1"))
        assert "legacy1" in called

    def test_update_card_routes_to_stub_and_creates_approved(
        self, watch_dir, monkeypatch
    ):
        """CARD_TYPE_UPDATE dispatches to the update stub which creates .approved."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        self._write_card(
            watch_dir,
            "u1",
            {"card_type": "update", "component": "core", "latest_version": "2026.7.5"},
        )
        asyncio.run(dashboard.approve("u1"))
        assert (watch_dir / "u1.approved").exists()

    def test_netalertx_heal_card_routes_to_stub_and_creates_approved(
        self, watch_dir, monkeypatch
    ):
        """CARD_TYPE_NETALERTX_HEAL dispatches to the netalertx heal stub."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        self._write_card(
            watch_dir,
            "nh1",
            {"card_type": "netalertx_heal", "heal_action": "fix_webhook_fields"},
        )
        asyncio.run(dashboard.approve("nh1"))
        assert (watch_dir / "nh1.approved").exists()

    def test_resource_action_card_routes_to_stub_and_creates_approved(
        self, watch_dir, monkeypatch
    ):
        """CARD_TYPE_RESOURCE_ACTION dispatches to the resource action stub."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        self._write_card(
            watch_dir,
            "ra1",
            {"card_type": "resource_action", "action": "offload_backups"},
        )
        asyncio.run(dashboard.approve("ra1"))
        assert (watch_dir / "ra1.approved").exists()

    def test_unknown_card_type_falls_back_to_approved(self, watch_dir, monkeypatch):
        """Unknown card_type just touches the .approved file."""
        import web.dashboard as dashboard

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))
        self._write_card(watch_dir, "x1", {"card_type": "future_unknown_type"})
        asyncio.run(dashboard.approve("x1"))
        assert (watch_dir / "x1.approved").exists()

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
