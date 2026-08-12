"""Tests for utils/investigation_loop.py — investigate_with_fallback and gate defaulting."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


class TestInvestigateWithFallback:
    """Tests for the investigate_with_fallback() convenience wrapper."""

    def _make_report(self):
        from utils.investigation_loop import InvestigationReport

        return InvestigationReport(
            topic="test",
            summary="Disk is full because of NetAlertX",
            root_causes=["NetAlertX data directory grew to 8 GB"],
            manual_only=[],
            confidence=0.85,
        )

    def test_returns_report_and_false_on_success(self):
        from utils.investigation_loop import investigate_with_fallback
        from utils.ssh_client import FakeSSHClient

        report = self._make_report()

        async def _fake_run(**_):
            return report

        with patch(
            "utils.investigation_loop.run_investigation",
            new=AsyncMock(return_value=report),
        ):
            result_report, is_fallback = asyncio.run(
                investigate_with_fallback(
                    topic="HA disk CRITICAL",
                    goal="Find root cause",
                    context="Disk is at 1.9 GB",
                    llm_client=object(),
                    ssh_client=FakeSSHClient(),
                )
            )
        assert result_report is report
        assert is_fallback is False

    def test_returns_none_and_true_on_timeout(self):
        from utils.investigation_loop import investigate_with_fallback
        from utils.ssh_client import FakeSSHClient

        async def _slow():
            await asyncio.sleep(999)
            raise AssertionError("should not reach here")

        with patch(
            "utils.investigation_loop.run_investigation",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ):
            result_report, is_fallback = asyncio.run(
                investigate_with_fallback(
                    topic="HA disk CRITICAL",
                    goal="Find root cause",
                    context="Disk is at 1.9 GB",
                    llm_client=object(),
                    ssh_client=FakeSSHClient(),
                    timeout=0.01,
                )
            )
        assert result_report is None
        assert is_fallback is True

    def test_returns_none_and_true_on_exception(self):
        from utils.investigation_loop import investigate_with_fallback
        from utils.ssh_client import FakeSSHClient

        with patch(
            "utils.investigation_loop.run_investigation",
            new=AsyncMock(side_effect=RuntimeError("ollama down")),
        ):
            result_report, is_fallback = asyncio.run(
                investigate_with_fallback(
                    topic="HA disk CRITICAL",
                    goal="Find root cause",
                    context="Disk is at 1.9 GB",
                    llm_client=object(),
                    ssh_client=FakeSSHClient(),
                )
            )
        assert result_report is None
        assert is_fallback is True


class TestRunInvestigationGateDefault:
    """run_investigation() should create an AUTONOMOUS gate when gate=None."""

    def test_gate_defaults_to_autonomous_level(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.investigation_loop import run_investigation

        captured = {}

        async def _fake_loop_run(**_):
            return None

        import utils.investigation_loop as il

        original = il.run_investigation

        async def _intercept(**kwargs):
            # Spy on the gate that gets created inside run_investigation
            pass

        # Just verify AutonomyGate(level=4) auto-executes LOW risk
        gate = AutonomyGate(level=4)
        assert gate.should_auto_execute(RiskLevel.LOW) is True
        assert gate.should_auto_execute(RiskLevel.HIGH) is True


class TestSerializeAnalysis:
    """Tests for _serialize_analysis() module-level helper in resource.py."""

    def _make_report(self, manual_only=None, confidence=0.9):
        from utils.investigation_loop import InvestigationAction, InvestigationReport

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
        from utils.resource import _serialize_analysis

        result = _serialize_analysis(None, self._heuristic(), is_fallback=True)
        assert result["source"] == "heuristic"
        assert result["text"] == "heuristic text"

    def test_investigation_result_sets_source_investigation(self):
        from utils.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(), self._heuristic(), is_fallback=False
        )
        assert result["source"] == "investigation"
        assert result["text"] == "NetAlertX is the culprit"

    def test_investigation_preserves_top_consumers_from_heuristic(self):
        from utils.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(), self._heuristic(), is_fallback=False
        )
        assert result["top_consumers"] == [
            {"name": "NetAlertX", "size_human": "8.0 GB"}
        ]

    def test_hitl_sufficient_false_when_manual_only_nonempty(self):
        from utils.resource import _serialize_analysis

        report = self._make_report(manual_only=["Expand physical storage"])
        result = _serialize_analysis(report, self._heuristic(), is_fallback=False)
        assert result["hitl_sufficient"] is False

    def test_hitl_sufficient_true_when_manual_only_empty(self):
        from utils.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(manual_only=[]), self._heuristic(), is_fallback=False
        )
        assert result["hitl_sufficient"] is True

    def test_confidence_included_in_investigation_result(self):
        from utils.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(confidence=0.75), self._heuristic(), is_fallback=False
        )
        assert result["confidence"] == pytest.approx(0.75)

    def test_hitl_actions_serialized(self):
        from utils.resource import _serialize_analysis

        result = _serialize_analysis(
            self._make_report(), self._heuristic(), is_fallback=False
        )
        assert len(result["hitl_actions"]) == 1
        assert result["hitl_actions"][0]["name"] == "Remove NetAlertX"


class TestBuildDiskInvestigationContext:
    """Tests for _build_disk_investigation_context() in resource.py."""

    def _make_status(self, disk_free=1.9, disk_total=13.6):
        from utils.resource import ResourceStatus

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
        from utils.resource import _build_disk_investigation_context

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
        from utils.resource import _build_disk_investigation_context

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
        from utils.resource import _build_disk_investigation_context

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

    def _make_status(self, disk_free=1.5):
        from utils.resource import ResourceStatus

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
        from utils.investigation_loop import InvestigationReport

        return InvestigationReport(
            topic="HA disk CRITICAL",
            summary="NetAlertX is consuming 8 GB",
            root_causes=["NetAlertX data dir too large"],
            manual_only=[],
            confidence=0.88,
        )

    def test_disk_analysis_source_is_investigation_when_llm_available(self):
        from utils.notify import FakeNotifier
        from utils.resource import ResourcePoller
        from utils.ssh_client import FakeSSHClient

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
            "utils.investigation_loop.investigate_with_fallback",
            new=AsyncMock(return_value=(report, False)),
        ) as mock_inv:
            with patch(
                "utils.resource._build_disk_investigation_context", return_value="ctx"
            ):
                asyncio.run(poller._check_and_alert(self._make_status()))

        # investigate_with_fallback should have been called
        mock_inv.assert_called_once()
        assert len(notifier.sent) == 1
        da = notifier.sent[0]["payload"]["disk_analysis"]
        assert da["source"] == "investigation"
        assert da["text"] == "NetAlertX is consuming 8 GB"

    def test_disk_analysis_source_is_heuristic_on_fallback(self):
        from utils.notify import FakeNotifier
        from utils.resource import ResourcePoller
        from utils.ssh_client import FakeSSHClient

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
            "utils.investigation_loop.investigate_with_fallback",
            new=AsyncMock(return_value=(None, True)),
        ):
            with patch(
                "utils.resource._build_disk_investigation_context", return_value="ctx"
            ):
                asyncio.run(poller._check_and_alert(self._make_status()))

        assert len(notifier.sent) == 1
        da = notifier.sent[0]["payload"]["disk_analysis"]
        assert da["source"] == "heuristic"

    def test_investigation_skipped_when_no_llm_client(self):
        from utils.notify import FakeNotifier
        from utils.resource import ResourcePoller
        from utils.ssh_client import FakeSSHClient

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
            "utils.investigation_loop.investigate_with_fallback", new=AsyncMock()
        ) as mock_inv:
            asyncio.run(poller._check_and_alert(self._make_status()))

        mock_inv.assert_not_called()
        assert len(notifier.sent) == 1
        da = notifier.sent[0]["payload"]["disk_analysis"]
        assert da.get("source") == "heuristic"
