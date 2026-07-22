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

    def test_hitl_payload_contains_llm_trace(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from ha_agent_core import DiagnosticsReport
        import ha_agent_sandbox_engine

        _orig = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
        _fix = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"
        report = DiagnosticsReport(
            is_valid=False,
            severity="HIGH",
            identified_issues=["server_port should be 8124"],
            recommended_fix_yaml=_fix,
        )
        llm = FakeLLMClient(report.model_dump_json())
        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _orig},
            command_results={
                "ha core check": (0, "OK", ""),
                "ha backup new": (0, '{"slug": "abc123"}', ""),
                "ha core restart": (0, "", ""),
            },
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
        assert "llm_trace" in payload
        assert "model" in payload["llm_trace"]
        assert "raw_response" in payload["llm_trace"]

    def test_hitl_payload_contains_diagnosis(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from ha_agent_core import DiagnosticsReport
        import ha_agent_sandbox_engine

        _orig = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
        _fix = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"
        report = DiagnosticsReport(
            is_valid=False,
            severity="HIGH",
            identified_issues=["server_port should be 8124"],
            recommended_fix_yaml=_fix,
        )
        llm = FakeLLMClient(report.model_dump_json())
        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _orig},
            command_results={
                "ha core check": (0, "OK", ""),
                "ha backup new": (0, '{"slug": "abc123"}', ""),
                "ha core restart": (0, "", ""),
            },
        )
        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, notifier=notifier, gate=gate
            )
        )
        payload = notifier.sent[0]["payload"]
        assert "diagnosis" in payload
        assert payload["diagnosis"]["severity"] == "HIGH"

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
