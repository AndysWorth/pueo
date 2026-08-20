"""Tests for utils/cloud_escalation.py — run_cloud_escalation()."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestRunCloudEscalation:
    def _make_registry(self):
        from utils.agent.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        for name in ("read_config", "finish_repair"):
            reg.register(
                ToolDefinition(
                    name=name,
                    description=f"{name}",
                    parameters={"type": "object", "properties": {}, "required": []},
                )
            )
        return reg

    def test_billing_cap_blocks_escalation(self):
        """run_cloud_escalation raises BillingCapError if spending caps are exceeded."""
        from utils.repair.billing import BillingCapError
        from utils.repair.cloud_escalation import run_cloud_escalation
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        import pytest

        registry = self._make_registry()

        # Patch the source module since billing is imported locally inside the function
        with patch("utils.repair.billing.check_billing_caps") as mock_check:
            mock_check.side_effect = BillingCapError("daily cap exceeded")
            with pytest.raises(BillingCapError):
                asyncio.run(
                    run_cloud_escalation(
                        initial_context="diagnose HA",
                        tool_registry=registry,
                        gate=FakeAutonomyGate(auto_execute_result=True),
                        ha_ssh_client=FakeSSHClient(),
                        notifier=FakeNotifier(),
                        incident_id="test-001",
                    )
                )

    def test_cloud_loop_runs_on_success(self):
        """run_cloud_escalation runs an AgentLoop with ClaudeAPIClient and returns result."""
        from utils.agent.agent_loop import AgentLoopResult
        from utils.repair.cloud_escalation import run_cloud_escalation
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient

        fake_result = AgentLoopResult(outcome="success", steps=[])

        with (
            patch("utils.repair.billing.check_billing_caps"),
            patch("utils.llm.cloud_client.ClaudeAPIClient") as MockClient,
            patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            patch("utils.agent.tool_executor.ToolExecutor"),
        ):
            mock_loop_instance = MagicMock()
            mock_loop_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_loop_instance

            result = asyncio.run(
                run_cloud_escalation(
                    initial_context="diagnose HA",
                    tool_registry=self._make_registry(),
                    gate=FakeAutonomyGate(auto_execute_result=True),
                    ha_ssh_client=FakeSSHClient(),
                    notifier=FakeNotifier(),
                    incident_id="test-002",
                )
            )

        assert result.outcome == "success"

    def test_default_context_inserted_when_empty(self):
        """When initial_context is empty a fallback prompt is used."""
        from utils.agent.agent_loop import AgentLoopResult
        from utils.repair.cloud_escalation import run_cloud_escalation
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient

        fake_result = AgentLoopResult(outcome="exhausted", steps=[])
        captured: list[str] = []

        with (
            patch("utils.repair.billing.check_billing_caps"),
            patch("utils.llm.cloud_client.ClaudeAPIClient"),
            patch("utils.agent.tool_executor.ToolExecutor"),
            patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
        ):
            mock_loop_instance = MagicMock()

            async def _capture_run(ctx):
                captured.append(ctx)
                return fake_result

            mock_loop_instance.run = _capture_run
            MockLoop.return_value = mock_loop_instance

            asyncio.run(
                run_cloud_escalation(
                    initial_context="",
                    tool_registry=self._make_registry(),
                    gate=FakeAutonomyGate(auto_execute_result=True),
                    ha_ssh_client=FakeSSHClient(),
                    notifier=FakeNotifier(),
                    incident_id="test-003",
                )
            )

        assert len(captured) == 1
        assert "configuration.yaml" in captured[0]

    def test_billing_cap_error_propagates(self):
        """BillingCapError raised during loop execution propagates to the caller."""
        from utils.repair.billing import BillingCapError
        from utils.repair.cloud_escalation import run_cloud_escalation
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        import pytest

        with (
            patch("utils.repair.billing.check_billing_caps"),
            patch("utils.llm.cloud_client.ClaudeAPIClient"),
            patch("utils.agent.tool_executor.ToolExecutor"),
            patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
        ):
            mock_loop_instance = MagicMock()
            mock_loop_instance.run = AsyncMock(
                side_effect=BillingCapError("per-incident cap")
            )
            MockLoop.return_value = mock_loop_instance

            with pytest.raises(BillingCapError):
                asyncio.run(
                    run_cloud_escalation(
                        initial_context="diagnose",
                        tool_registry=self._make_registry(),
                        gate=FakeAutonomyGate(auto_execute_result=True),
                        ha_ssh_client=FakeSSHClient(),
                        notifier=FakeNotifier(),
                        incident_id="test-004",
                    )
                )
