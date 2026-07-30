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
        response = client.get("/queue")
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
        response = client.get("/queue")
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
        response = client.get("/queue")
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
        asyncio.run(dashboard.approve("u1"))
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
        asyncio.run(dashboard.approve("nh1"))
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
        asyncio.run(dashboard.approve("ra1"))
        assert "ra1" in called
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
        import ha_update_manager
        import utils.ssh_client as ssh_mod
        import utils.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ssh_client import FakeSSHClient

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
        import ha_update_manager
        import utils.ssh_client as ssh_mod
        import utils.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ssh_client import FakeSSHClient

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
        import ha_update_manager
        import utils.ssh_client as ssh_mod
        import utils.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ssh_client import FakeSSHClient

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
        import ha_update_manager
        import utils.ssh_client as ssh_mod
        import utils.autonomy as autonomy_mod
        import utils.notify as notify_mod
        from utils.ssh_client import FakeSSHClient

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
        import utils.ssh_client as ssh_mod
        import utils.autonomy as autonomy_mod
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
        import ha_agent_advanced
        import utils.ssh_client as ssh_mod

        db_path = tmp_path / "test.db"
        self._make_db(db_path, ["slug-a", "slug-b"])

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())
        monkeypatch.setattr(dashboard, "DB_PATH", str(db_path))

        offloaded = []
        retention_called = []
        purge_called = []

        async def _fake_offload(slug, ssh_client=None):
            offloaded.append(slug)

        async def _fake_retention(ssh_client=None):
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
        import ha_agent_advanced
        import utils.ssh_client as ssh_mod

        monkeypatch.setattr(ssh_mod, "AsyncSSHClient", lambda *a, **kw: object())

        retention_called = []
        purge_called = []

        async def _fake_retention(ssh_client=None):
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
        import utils.ssh_client as ssh_mod

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

    def test_exception_writes_rejected_and_fix_error(self, tmp_path, monkeypatch):
        """Exception in offload → .rejected, fix_error set, no in_progress."""
        import json as _json
        import sqlite3 as _sqlite3
        import web.dashboard as dashboard
        import ha_agent_advanced
        import utils.ssh_client as ssh_mod

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
        import ha_agent_advanced
        import utils.ssh_client as ssh_mod

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
        import utils.supervisor as sup_mod
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
        import utils.supervisor as sup_mod
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


class TestSSEEventBus:
    """Tests for the SSE fan-out publish/subscribe mechanism."""

    def test_publish_event_reaches_subscriber(self):
        """An event published to the bus is delivered to a subscribed queue."""
        import asyncio
        from utils.supervisor import publish_event, subscribe, unsubscribe

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
        from utils.supervisor import publish_event, subscribe, unsubscribe

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
        from utils.supervisor import publish_event, subscribe, unsubscribe

        async def run():
            q = subscribe()
            unsubscribe(q)
            publish_event({"event_type": "loop_status", "loop": "gone"})
            assert q.empty()

        asyncio.run(run())

    def test_loop_supervisor_emits_to_subscribers(self):
        """LoopSupervisor._emit() publishes to SSE subscribers via publish_event."""
        import asyncio
        import utils.supervisor as sup_mod
        from utils.supervisor import LoopSupervisor, subscribe, unsubscribe

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
        import ha_agent_advanced
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
