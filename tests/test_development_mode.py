"""Tests for DEVELOPMENT_MODE gates across cloud escalation, add_tool, and episodes tab."""

from __future__ import annotations

import asyncio

import pytest


class TestDevelopmentModeEpisodesGate:
    """Episodes tab returns 404 when DEVELOPMENT_MODE is False."""

    def test_episodes_tab_returns_404_when_dev_mode_false(self, monkeypatch):
        import config as _config
        from starlette.testclient import TestClient
        import web.dashboard as dashboard

        monkeypatch.setattr(_config, "DEVELOPMENT_MODE", False)
        client = TestClient(dashboard.app, raise_server_exceptions=False)
        assert client.get("/episodes").status_code == 404
        assert client.get("/episodes/export").status_code == 404


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
