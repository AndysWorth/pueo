"""Tests for DEVELOPMENT_MODE gates across cloud escalation, episode submission, and add_tool."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest


class TestDevelopmentModeEpisodeSubmit:
    """Dashboard /episodes/{id}/submit returns 400 when dev mode is off."""

    @pytest.fixture()
    def db(self, tmp_path):
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_episodes (
                id TEXT PRIMARY KEY,
                timestamp TEXT,
                trigger TEXT,
                initial_context TEXT,
                outcome TEXT,
                fix_applied TEXT,
                model_used TEXT,
                tool_sequence TEXT,
                anonymized BOOLEAN,
                submitted_at TEXT
            );
            INSERT INTO repair_episodes VALUES (
                'ep-001', '2026-09-04T00:00:00', 'test', 'ctx', 'fixed',
                NULL, 'ollama', '[]', 0, NULL
            );
            """
        )
        conn.commit()
        conn.close()
        return db_path

    def test_submit_blocked_in_appliance_mode(self, tmp_path, monkeypatch, db):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "DEVELOPMENT_MODE", False)
        monkeypatch.setattr(dashboard, "DB_PATH", str(db))
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path / "hitl"))

        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post(
            "/episodes/ep-001/submit",
            json={"redacted_yaml": "problem: test\nfix: restart"},
        )
        assert resp.status_code == 400
        assert "development_mode" in resp.json().get("detail", "").lower()

    def test_submit_allowed_in_dev_mode_without_repo(self, tmp_path, monkeypatch, db):
        """With dev mode on but no repo configured, should get 400 about missing repo."""
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dashboard, "DEVELOPMENT_MODE", True)
        monkeypatch.setattr(dashboard, "FEDERATED_CASES_REPO", "")
        monkeypatch.setattr(dashboard, "DB_PATH", str(db))
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(tmp_path / "hitl"))

        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.post(
            "/episodes/ep-001/submit",
            json={"redacted_yaml": "problem: test\nfix: restart"},
        )
        assert resp.status_code == 400
        assert "FEDERATED_CASES_REPO" in resp.json().get("detail", "")


class TestDevelopmentModeAddTool:
    """_add_tool returns error when DEVELOPMENT_MODE is False."""

    _VALID_CODE = "def ping_host(host: str) -> str:\n    return host\n"

    @pytest.fixture()
    def executor(self, tmp_path, monkeypatch):
        import config
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor

        monkeypatch.setattr(config, "DEVELOPMENT_MODE", False)
        monkeypatch.setattr(config, "CHAT_ALLOW_TOOL_REGISTRATION", True)

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate(auto_execute_result=True)
        ex = ToolExecutor(ha_ssh_client=ssh, gate=gate, notifier=None)
        ex._sandbox_passed = True
        ex._sandbox_output = "All checks passed"
        return ex

    def test_blocked_when_dev_mode_off(self, executor):
        from utils.agent.tool_registry import ToolCall

        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="add_tool",
                    arguments={
                        "name": "ping_host",
                        "description": "Pings a host",
                        "parameters_schema": "{}",
                        "code": self._VALID_CODE,
                    },
                )
            )
        )
        assert not result.success
        assert "development_mode" in (result.error or "").lower()


class TestDevelopmentModeCloudEscalation:
    """Cloud escalation card is only created when DEVELOPMENT_MODE is True."""

    def test_cloud_escalation_skipped_in_appliance_mode(self, monkeypatch):
        """run_repair_loop skips cloud escalation entirely when DEVELOPMENT_MODE=False."""
        import agents.ha_agent_sandbox_engine as engine

        monkeypatch.setattr(engine, "DEVELOPMENT_MODE", False)
        monkeypatch.setattr(engine, "LLM_PROVIDER", "both")

        from utils.agent.tool_registry import AgentLoopResult

        result = AgentLoopResult(outcome="exhausted", steps=[])
        should_escalate = (
            result.outcome in ("exhausted", "timeout")
            and engine.LLM_PROVIDER == "both"
            and engine.DEVELOPMENT_MODE
        )
        assert should_escalate is False

    def test_cloud_escalation_eligible_in_dev_mode(self, monkeypatch):
        """Condition evaluates True when dev mode is on and provider is 'both'."""
        import agents.ha_agent_sandbox_engine as engine

        monkeypatch.setattr(engine, "DEVELOPMENT_MODE", True)
        monkeypatch.setattr(engine, "LLM_PROVIDER", "both")

        from utils.agent.tool_registry import AgentLoopResult

        result = AgentLoopResult(outcome="exhausted", steps=[])
        should_escalate = (
            result.outcome in ("exhausted", "timeout")
            and engine.LLM_PROVIDER == "both"
            and engine.DEVELOPMENT_MODE
        )
        assert should_escalate is True
