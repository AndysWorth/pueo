"""Tests for utils/investigation_loop.py — run_investigation gate defaulting."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@dataclass
class _FakeLoopResult:
    outcome: str = "success"
    steps: list = field(default_factory=list)


class TestRunInvestigationGateDefault:
    """run_investigation() should use AUTONOMY_LEVEL gate when gate=None."""

    def _run(self, autonomy_level: int, explicit_gate=None):
        """Run run_investigation with a captured gate; return the captured gate."""
        fake_executor_class = MagicMock()
        fake_executor_instance = MagicMock()
        fake_executor_class.return_value = fake_executor_instance

        fake_loop_instance = MagicMock()
        fake_loop_instance.run = AsyncMock(return_value=_FakeLoopResult())
        fake_loop_class = MagicMock(return_value=fake_loop_instance)

        with (
            patch("utils.agent.tool_executor.ToolExecutor", fake_executor_class),
            patch("utils.agent.agent_loop.AgentLoop", fake_loop_class),
            patch("config.AUTONOMY_LEVEL", autonomy_level),
            patch("utils.agent.supervisor.increment_active_agent", return_value=None),
            patch("utils.agent.supervisor.decrement_active_agent", return_value=None),
            patch("utils.agent.supervisor.publish_activity_done", return_value=None),
            patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
        ):
            from utils.agent.investigation_loop import run_investigation

            asyncio.run(
                run_investigation(
                    topic="disk full",
                    investigation_goal="find cause",
                    context="2 GB free",
                    llm_client=MagicMock(),
                    ha_ssh_client=MagicMock(),
                    gate=explicit_gate,
                )
            )

        _, kwargs = fake_executor_class.call_args
        return kwargs.get("gate")

    def test_gate_uses_autonomy_level_when_none(self):
        from utils.agent.autonomy import AutonomyGate

        gate = self._run(autonomy_level=1)
        assert isinstance(gate, AutonomyGate)
        assert gate.level == 1

    def test_gate_level_reflects_config(self):
        from utils.agent.autonomy import AutonomyGate

        gate = self._run(autonomy_level=2)
        assert isinstance(gate, AutonomyGate)
        assert gate.level == 2

    def test_explicit_gate_passed_through_unchanged(self):
        from utils.agent.autonomy import AutonomyGate

        explicit = AutonomyGate(level=0)
        gate = self._run(autonomy_level=3, explicit_gate=explicit)
        assert gate is explicit

    def test_outcome_mirrored_from_result(self):
        """report.outcome should reflect the AgentLoop result.outcome."""
        fake_loop_instance = MagicMock()
        fake_loop_instance.run = AsyncMock(
            return_value=_FakeLoopResult(outcome="stuck")
        )
        fake_loop_class = MagicMock(return_value=fake_loop_instance)

        with (
            patch("utils.agent.tool_executor.ToolExecutor", MagicMock()),
            patch("utils.agent.agent_loop.AgentLoop", fake_loop_class),
            patch("config.AUTONOMY_LEVEL", 3),
            patch("utils.agent.supervisor.increment_active_agent", return_value=None),
            patch("utils.agent.supervisor.decrement_active_agent", return_value=None),
            patch("utils.agent.supervisor.publish_activity_done", return_value=None),
            patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
        ):
            from utils.agent.investigation_loop import run_investigation

            report = asyncio.run(
                run_investigation(
                    topic="disk",
                    investigation_goal="find",
                    context="",
                    llm_client=MagicMock(),
                    ha_ssh_client=MagicMock(),
                )
            )

        assert report.outcome == "stuck"


class TestSerializeAnalysis:
    """Tests for _serialize_analysis() module-level helper in resource.py."""

    def _make_report(self, manual_only=None, confidence=0.9):
        from utils.agent.investigation_loop import (
            InvestigationAction,
            InvestigationReport,
        )

        return InvestigationReport(
            topic="disk",
            summary="NetAlertX is the culprit",
            root_causes=["NetAlertX data dir is 8 GB"],
            manual_only=manual_only or [],
            hitl_actions=[
                InvestigationAction(
                    name="Remove NetAlertX",
                    description="Uninstall the add-on",
                    estimated_impact="frees 8 GB",
                    reversible=False,
                    risk_level="HIGH",
                    action_key="remove_addon",
                )
            ],
            knowledge_sources=["ha_release_notes/2026.8"],
            confidence=confidence,
        )

    def _heuristic(self):
        return {
            "text": "heuristic text",
            "hitl_sufficient": True,
            "needed_mb": 500.0,
            "top_consumers": [{"name": "NetAlertX", "size_human": "8.0 GB"}],
            "addon_persistent": [],
        }

    def test_fallback_returns_heuristic_with_source_key(self):
        from utils.disk.resource import _serialize_analysis

        result = _serialize_analysis(None, self._heuristic(), is_fallback=True)
        assert result["source"] == "heuristic"
        assert result["text"] == "heuristic text"

    def test_investigation_result_sets_source_investigation(self):
        from utils.disk.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(), self._heuristic(), is_fallback=False
        )
        assert result["source"] == "investigation"
        assert result["text"] == "NetAlertX is the culprit"

    def test_investigation_preserves_top_consumers_from_heuristic(self):
        from utils.disk.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(), self._heuristic(), is_fallback=False
        )
        assert result["top_consumers"] == [
            {"name": "NetAlertX", "size_human": "8.0 GB"}
        ]

    def test_hitl_sufficient_false_when_manual_only_nonempty(self):
        from utils.disk.resource import _serialize_analysis

        report = self._make_report(manual_only=["Expand physical storage"])
        result = _serialize_analysis(report, self._heuristic(), is_fallback=False)
        assert result["hitl_sufficient"] is False

    def test_hitl_sufficient_true_when_manual_only_empty(self):
        from utils.disk.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(manual_only=[]), self._heuristic(), is_fallback=False
        )
        assert result["hitl_sufficient"] is True

    def test_confidence_included_in_investigation_result(self):
        from utils.disk.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(confidence=0.75), self._heuristic(), is_fallback=False
        )
        assert result["confidence"] == pytest.approx(0.75)

    def test_hitl_actions_serialized(self):
        from utils.disk.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(), self._heuristic(), is_fallback=False
        )
        assert len(result["hitl_actions"]) == 1
        assert result["hitl_actions"][0]["name"] == "Remove NetAlertX"


class TestBuildDiskInvestigationContext:
    """Tests for _build_disk_investigation_context() in resource.py."""

    def _make_status(self, disk_free=1.9, disk_total=13.6):
        from utils.disk.resource import ResourceStatus

        return ResourceStatus(
            disk_free_gb=disk_free,
            disk_total_gb=disk_total,
            disk_used_gb=disk_total - disk_free,
            mem_available_mb=1000.0,
            mem_total_mb=2000.0,
            disk_warn=True,
            disk_critical=True,
            mem_warn=False,
        )

    def test_includes_disk_free_and_total(self):
        from utils.disk.resource import _build_disk_investigation_context

        ctx = _build_disk_investigation_context(
            self._make_status(disk_free=1.9, disk_total=13.6),
            None,
            None,
            3.0,
            {},
        )
        assert "1.90 GB" in ctx
        assert "13.60 GB" in ctx

    def test_includes_post_recovery_info_when_available(self):
        from utils.disk.resource import _build_disk_investigation_context

        ctx = _build_disk_investigation_context(
            self._make_status(),
            2.5,  # disk_free_after_gb
            None,
            3.0,
            {},
        )
        assert "2.50 GB" in ctx
        assert "still CRITICAL" in ctx

    def test_includes_top_consumers_when_heuristic_has_them(self):
        from utils.disk.resource import _build_disk_investigation_context

        heuristic = {
            "top_consumers": [{"name": "NetAlertX", "size_human": "8.0 GB"}],
            "needed_mb": 1100.0,
        }
        ctx = _build_disk_investigation_context(
            self._make_status(), None, None, 3.0, heuristic
        )
        assert "NetAlertX" in ctx
        assert "1100" in ctx


class TestResourcePollerInvestigationIntegration:
    """ResourcePoller._check_and_alert() wires investigation when llm_client is set."""

    _HITL_DDL = (
        "CREATE TABLE IF NOT EXISTS hitl_suppression ("
        "card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '', "
        "description TEXT NOT NULL DEFAULT '', "
        "first_sent_at REAL NOT NULL DEFAULT 0, "
        "last_sent_at REAL NOT NULL DEFAULT 0, "
        "send_count INTEGER NOT NULL DEFAULT 1, "
        "last_action TEXT, last_action_at REAL, "
        "rejection_count INTEGER NOT NULL DEFAULT 0, "
        "next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0, "
        "known_issue_note TEXT, resolved_at REAL)"
    )

    @pytest.fixture(autouse=True)
    def _patch_hitl_db(self, monkeypatch):
        """Route sqlite3.connect to an in-memory DB so real suppression logic can run."""
        import sqlite3 as _sq3

        _mem = _sq3.connect(":memory:")
        _mem.execute(self._HITL_DDL)
        _mem.commit()
        monkeypatch.setattr(_sq3, "connect", lambda *a, **kw: _mem)

    def _make_status(self, disk_free=1.5):
        from utils.disk.resource import ResourceStatus

        return ResourceStatus(
            disk_free_gb=disk_free,
            disk_total_gb=13.6,
            disk_used_gb=13.6 - disk_free,
            mem_available_mb=1000.0,
            mem_total_mb=2000.0,
            disk_warn=True,
            disk_critical=True,
            mem_warn=False,
        )

    def _make_report(self):
        from utils.agent.investigation_loop import InvestigationReport

        return InvestigationReport(
            topic="HA disk CRITICAL",
            summary="NetAlertX is consuming 8 GB",
            root_causes=["NetAlertX data dir too large"],
            manual_only=[],
            confidence=0.88,
        )

    def test_disk_analysis_source_is_investigation_when_llm_available(self):
        from utils.hitl.notify import FakeNotifier
        from utils.disk.resource import ResourcePoller
        from utils.ha.ssh_client import FakeSSHClient

        notifier = FakeNotifier()
        report = self._make_report()
        poller = ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=3.0,
            mem_warn_mb=256.0,
            llm_client=object(),  # non-None triggers investigation path
        )

        with patch(
            "utils.agent.investigation_loop.run_investigation",
            new=AsyncMock(return_value=report),
        ) as mock_inv:
            with patch(
                "utils.disk.resource._build_disk_investigation_context",
                return_value="ctx",
            ):
                asyncio.run(poller._check_and_alert(self._make_status()))

        # run_investigation should have been called
        mock_inv.assert_called_once()
        assert len(notifier.sent) == 1
        da = notifier.sent[0]["payload"]["disk_analysis"]
        assert da["source"] == "investigation"
        assert da["text"] == "NetAlertX is consuming 8 GB"

    def test_disk_analysis_source_is_heuristic_on_fallback(self):
        from utils.hitl.notify import FakeNotifier
        from utils.disk.resource import ResourcePoller
        from utils.ha.ssh_client import FakeSSHClient

        notifier = FakeNotifier()
        poller = ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=3.0,
            mem_warn_mb=256.0,
            llm_client=object(),
        )

        with patch(
            "utils.agent.investigation_loop.run_investigation",
            new=AsyncMock(side_effect=RuntimeError("ollama down")),
        ):
            with patch(
                "utils.disk.resource._build_disk_investigation_context",
                return_value="ctx",
            ):
                asyncio.run(poller._check_and_alert(self._make_status()))

        assert len(notifier.sent) == 1
        da = notifier.sent[0]["payload"]["disk_analysis"]
        assert da["source"] == "heuristic"

    def test_investigation_skipped_when_no_llm_client(self):
        from utils.hitl.notify import FakeNotifier
        from utils.disk.resource import ResourcePoller
        from utils.ha.ssh_client import FakeSSHClient

        notifier = FakeNotifier()
        poller = ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=3.0,
            mem_warn_mb=256.0,
            llm_client=None,  # no LLM
        )

        with patch(
            "utils.agent.investigation_loop.run_investigation", new=AsyncMock()
        ) as mock_inv:
            asyncio.run(poller._check_and_alert(self._make_status()))

        mock_inv.assert_not_called()
        assert len(notifier.sent) == 1
        da = notifier.sent[0]["payload"]["disk_analysis"]
        assert da.get("source") == "heuristic"

    def test_critical_recovery_passes_notifier_and_db_path(self, tmp_path):
        """ResourcePoller passes notifier and db_path to run_safe_disk_recovery."""
        import sqlite3

        from utils.hitl.notify import FakeNotifier
        from utils.disk.resource import ResourcePoller
        from utils.ha.ssh_client import FakeSSHClient

        db = str(tmp_path / "rp_test.db")
        with sqlite3.connect(db) as conn:
            conn.executescript(self._HITL_DDL)

        notifier = FakeNotifier()
        llm = object()
        poller = ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=3.0,
            mem_warn_mb=256.0,
            llm_client=llm,
            db_path=db,
        )

        captured_kwargs = {}

        async def _fake_recovery(**kwargs):
            captured_kwargs.update(kwargs)
            from utils.disk.disk_recovery import RecoverySummary

            return RecoverySummary()

        with (
            patch(
                "utils.disk.disk_recovery.run_safe_disk_recovery",
                side_effect=_fake_recovery,
            ),
            patch(
                "utils.agent.work_queue.get_work_queue_or_none",
                return_value=None,
            ),
            patch(
                "utils.disk.resource.poll_host_resources",
                new=AsyncMock(return_value=self._make_status()),
            ),
            patch("utils.disk.resource.update_resource_status"),
            patch("utils.core.timeline.write_timeline_event"),
        ):
            asyncio.run(poller._check_and_alert(self._make_status()))

        assert captured_kwargs.get("notifier") is notifier
        assert captured_kwargs.get("db_path") == db


class TestInvestigationPromptFile:
    """Confirms prompts/investigation.md loads and formats correctly."""

    def test_prompt_loads_without_error(self):
        from utils.core.prompts import load_prompt

        text = load_prompt(
            "investigation",
            topic="disk full",
            investigation_goal="find cause",
            context="2 GB free",
        )
        assert "disk full" in text
        assert "find cause" in text
        assert "2 GB free" in text
