"""Tests for ha_update_manager stuck-loop backoff behavior."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_HITL_DDL = """
CREATE TABLE IF NOT EXISTS hitl_suppression (
    card_key TEXT PRIMARY KEY,
    card_type TEXT DEFAULT '',
    description TEXT DEFAULT '',
    first_sent_at REAL DEFAULT 0,
    last_sent_at REAL DEFAULT 0,
    send_count INTEGER DEFAULT 1,
    known_issue INTEGER DEFAULT 0,
    known_issue_note TEXT DEFAULT '',
    last_action TEXT,
    last_action_at REAL,
    rejection_count INTEGER DEFAULT 0,
    next_allowed_at REAL,
    resolved_at REAL
)
"""


def _make_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "update_test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(_HITL_DDL)
    return db_path


# ---------------------------------------------------------------------------
# Stuck-loop backoff — _run_update_analysis
# ---------------------------------------------------------------------------


class TestUpdateAnalysisStuckBackoff:
    def test_stuck_outcome_writes_backoff(self, tmp_path, pueo_dirs):
        """outcome='stuck' writes a deferred backoff row for the entity_id."""
        import asyncio
        import time
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)
        fake_result = AgentLoopResult(outcome="stuck")

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "Home Assistant Core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_update_manager import _run_update_analysis
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_update_analysis(
                    update=_FakeUpdate(),
                    notifier=FakeNotifier(),
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_action, next_allowed_at FROM hitl_suppression"
                " WHERE card_key = "
                "'update_analysis_backoff:update.home_assistant_core_update'",
            ).fetchone()
        assert row is not None, "backoff row must exist after stuck outcome"
        assert row[0] == "deferred"
        assert row[1] > time.time()

    def test_success_outcome_no_backoff(self, tmp_path, pueo_dirs):
        """outcome='success' must not write a backoff row."""
        import asyncio
        import sqlite3
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "Home Assistant Core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_update_manager import _run_update_analysis
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_update_analysis(
                    update=_FakeUpdate(),
                    notifier=FakeNotifier(),
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_action FROM hitl_suppression"
                " WHERE card_key = "
                "'update_analysis_backoff:update.home_assistant_core_update'",
            ).fetchone()
        assert row is None, "success outcome must not write a backoff row"

    def test_publishes_done_on_success(self, tmp_path, pueo_dirs):
        """_run_update_analysis publishes repair_done SSE event when outcome='success'."""
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "Home Assistant Core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        published = []

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
            mock.patch(
                "utils.agent.supervisor.publish_event",
                side_effect=lambda evt: published.append(evt),
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_update_manager import _run_update_analysis
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_update_analysis(update=_FakeUpdate(), notifier=FakeNotifier())
            )

        done_events = [e for e in published if e.get("event_type") == "repair_done"]
        assert done_events, "success outcome must publish repair_done"
        assert done_events[0].get("activity") == "update_analysis"

    def test_publishes_failed_on_stuck(self, tmp_path, pueo_dirs):
        """_run_update_analysis publishes repair_failed SSE event when outcome='stuck'."""
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)
        fake_result = AgentLoopResult(outcome="stuck")

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "Home Assistant Core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        published = []

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
            mock.patch(
                "utils.agent.supervisor.publish_event",
                side_effect=lambda evt: published.append(evt),
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_update_manager import _run_update_analysis
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_update_analysis(update=_FakeUpdate(), notifier=FakeNotifier())
            )

        failed_events = [e for e in published if e.get("event_type") == "repair_failed"]
        assert failed_events, "stuck outcome must publish repair_failed"
        assert failed_events[0].get("activity") == "update_analysis"


# ---------------------------------------------------------------------------
# fetch_release_notes_cached — non-core add-ons without release_url (Fix 4)
# ---------------------------------------------------------------------------


class TestFetchReleaseNotesCached:
    """fetch_release_notes_cached returns a helpful message for add-ons without release_url."""

    def test_addon_no_release_url_returns_message_not_404(self, tmp_path):
        """Non-HA-core version with no release_url must NOT hit GitHub API."""
        import asyncio
        from unittest import mock

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            result = await fetch_release_notes_cached(
                "10.5.0",  # non-YYYY version → not HA core
                cache_dir=str(tmp_path / "cache"),
                release_url=None,
            )
            return result

        # Patch the GitHub fetcher to ensure it is never called
        with mock.patch(
            "agents.ha_update_manager._fetch_github_release_notes"
        ) as mock_fetcher:
            result = asyncio.run(_run())

        mock_fetcher.assert_not_called()
        assert "unavailable" in result.lower()
        assert "release URL" in result or "release_url" in result.lower()

    def test_addon_with_release_url_still_fetches(self, tmp_path):
        """Non-HA-core version WITH release_url must use it (no regression)."""
        import asyncio
        from unittest import mock

        async def _fake_fetch_url(url):
            return "CHANGELOG v10.5.0: Fixed things."

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            return await fetch_release_notes_cached(
                "10.5.0",
                cache_dir=str(tmp_path / "cache2"),
                release_url="https://github.com/example/addon/releases/tag/10.5.0",
                _fetcher=None,
            )

        with mock.patch(
            "agents.ha_update_manager._fetch_release_notes_from_url",
            side_effect=_fake_fetch_url,
        ):
            result = asyncio.run(_run())

        assert "CHANGELOG" in result

    def test_ha_core_version_uses_github_fetcher(self, tmp_path):
        """YYYY.M.P versions always use the GitHub tags fetcher (no change)."""
        import asyncio

        async def _fake_fetcher(version):
            return f"Release notes for {version}"

        result = asyncio.run(
            __import__(
                "agents.ha_update_manager", fromlist=["fetch_release_notes_cached"]
            ).fetch_release_notes_cached(
                "2026.9.2",
                cache_dir=str(tmp_path / "cache3"),
                release_url=None,
                _fetcher=_fake_fetcher,
            )
        )

        assert "2026.9.2" in result


# ---------------------------------------------------------------------------
# Autonomy gate enforcement in _finish_update_analysis
# ---------------------------------------------------------------------------


class TestFinishUpdateAnalysisGate:
    """Gate override in _finish_update_analysis (Issues 1 & 2)."""

    def _make_executor(self, gate):
        """Build a minimal ToolExecutor with the given gate."""
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_executor import ToolExecutor

        notifier = MagicMock()
        notifier.send = AsyncMock()
        executor = ToolExecutor(
            ha_ssh_client=MagicMock(),
            gate=gate,
            notifier=notifier,
            db_path=":memory:",
        )
        return executor, notifier

    def _make_update(self, component="noaa_it_all"):
        """Build a minimal UpdateStatus-like object."""
        from types import SimpleNamespace

        return SimpleNamespace(
            entity_id=f"update.{component}",
            component=component,
            installed_version="0.7.0",
            latest_version="0.7.1",
            release_url=None,
            release_summary=None,
        )

    def test_guided_gate_overrides_llm_no_card_to_card(self, pueo_dirs):
        """At GUIDED level, gate overrides create_hitl_card=False to True."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate
        from utils.hitl.card_types import CARD_TYPE_UPDATE

        # autonomy_level=2 (GUIDED) → only LOW risk auto-executes;
        # updates are MEDIUM so gate blocks auto-execute → card required
        gate = AutonomyGate(level=2)
        executor, notifier = self._make_executor(gate)
        update = self._make_update("noaa_it_all")
        executor.set_update_status(update)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "agents.ha_log_monitor._update_mark_card_sent"
        ):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="No breaking changes found.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=False,  # LLM says no card
                )
            )

        # Gate must have overridden → notifier.send called (card created)
        assert notifier.send.called, "Gate override must cause a HITL card to be sent"
        assert result.success

    def test_autonomous_gate_sets_pending_auto_apply(self, pueo_dirs):
        """At AUTONOMOUS level, gate allows auto-apply → _pending_auto_apply=True."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        # autonomy_level=3 (AUTONOMOUS) → MEDIUM risk auto-executes
        gate = AutonomyGate(level=3)
        executor, notifier = self._make_executor(gate)
        update = self._make_update("noaa_it_all")
        executor.set_update_status(update)
        # Simulate that release notes were successfully obtained (happy path).
        executor._release_notes_obtained = True

        result = asyncio.run(
            executor._finish_update_analysis(
                safe_to_update=True,
                breaking_changes=[],
                affected_config_keys=[],
                pueo_command_risks=[],
                recommendation="Safe patch update.",
                instance_impact="none",
                proposed_config_fixes=[],
                create_hitl_card=False,
            )
        )

        assert result.success
        assert (
            executor._pending_auto_apply
        ), "_pending_auto_apply must be set for gate-approved auto-apply"
        # No card should be sent
        assert not notifier.send.called

    def test_autonomous_gate_no_auto_apply_when_not_safe(self, pueo_dirs, tmp_path):
        """When safe_to_update=False, auto-apply is not set even at AUTONOMOUS level."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(level=3)
        executor, notifier = self._make_executor(gate)
        update = self._make_update("noaa_it_all")
        executor.set_update_status(update)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "agents.ha_log_monitor._update_mark_card_sent"
        ):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=False,  # not safe
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Config changes required first.",
                    instance_impact="high",
                    proposed_config_fixes=[],
                    create_hitl_card=False,
                )
            )

        assert result.success
        assert (
            not executor._pending_auto_apply
        ), "unsafe update must not trigger auto-apply"


# ---------------------------------------------------------------------------
# Level 4 FULL_AUTONOMOUS: confidence-gated bypass for core/os components
# ---------------------------------------------------------------------------


class TestFullAutonomousBypass:
    """FULL_AUTONOMOUS (level 4) bypasses the core/os block when LLM confidence is high."""

    def _make_executor(self, gate):
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_executor import ToolExecutor

        notifier = MagicMock()
        notifier.send = AsyncMock()
        return (
            ToolExecutor(
                ha_ssh_client=MagicMock(),
                gate=gate,
                notifier=notifier,
                db_path=":memory:",
            ),
            notifier,
        )

    def _make_core_update(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            entity_id="update.core",
            component="core",
            installed_version="2026.9.0",
            latest_version="2026.10.0",
            release_url=None,
            release_summary=None,
        )

    def test_level3_critical_still_sends_card(self, pueo_dirs):
        """Level 3 AUTONOMOUS: core update still requires a card even at high confidence."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(level=3)
        executor, notifier = self._make_executor(gate)
        executor.set_update_status(self._make_core_update())

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "agents.ha_log_monitor._update_mark_card_sent"
        ):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Safe.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=False,
                    confidence=0.95,
                )
            )

        assert result.success
        assert not executor._pending_auto_apply, "level 3 must not auto-apply core"

    def test_level4_high_confidence_safe_bypasses_block(self, pueo_dirs):
        """Level 4 + safe_to_update=True + confidence ≥ threshold → auto-apply (no card)."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(level=4)
        executor, notifier = self._make_executor(gate)
        executor.set_update_status(self._make_core_update())
        # Simulate release notes obtained — required for the auto-apply path.
        executor._release_notes_obtained = True

        result = asyncio.run(
            executor._finish_update_analysis(
                safe_to_update=True,
                breaking_changes=[],
                affected_config_keys=[],
                pueo_command_risks=[],
                recommendation="Safe.",
                instance_impact="none",
                proposed_config_fixes=[],
                create_hitl_card=False,
                confidence=0.95,
            )
        )

        assert result.success
        assert executor._pending_auto_apply, "level 4 + high confidence must auto-apply"
        assert not notifier.send.called, "no card should be sent"

    def test_level4_low_confidence_sends_card(self, pueo_dirs):
        """Level 4 + confidence below threshold → sends card as fallback."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(level=4)
        executor, notifier = self._make_executor(gate)
        executor.set_update_status(self._make_core_update())

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "agents.ha_log_monitor._update_mark_card_sent"
        ):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Safe.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=False,
                    confidence=0.70,  # below 0.85 threshold
                )
            )

        assert result.success
        assert not executor._pending_auto_apply, "low confidence must not auto-apply"

    def test_level4_not_safe_sends_card(self, pueo_dirs):
        """Level 4 + safe_to_update=False → sends card regardless of confidence."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(level=4)
        executor, notifier = self._make_executor(gate)
        executor.set_update_status(self._make_core_update())

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "agents.ha_log_monitor._update_mark_card_sent"
        ):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=False,
                    breaking_changes=["Removed deprecated API"],
                    affected_config_keys=["some_key"],
                    pueo_command_risks=[],
                    recommendation="Review required.",
                    instance_impact="high",
                    proposed_config_fixes=[],
                    create_hitl_card=False,
                    confidence=0.95,
                )
            )

        assert result.success
        assert not executor._pending_auto_apply, "unsafe update must not auto-apply"

    def test_level4_none_confidence_sends_card(self, pueo_dirs):
        """Level 4 + confidence=None → sends card (no confidence reported)."""
        import asyncio

        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(level=4)
        executor, notifier = self._make_executor(gate)
        executor.set_update_status(self._make_core_update())

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "agents.ha_log_monitor._update_mark_card_sent"
        ):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Safe.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=False,
                    confidence=None,
                )
            )

        assert result.success
        assert not executor._pending_auto_apply, "no confidence → must not auto-apply"


# ---------------------------------------------------------------------------
# Auto-apply block for core/OS components
# ---------------------------------------------------------------------------


class TestAutoApplyBlock:
    """Auto-apply is always blocked for core/os/supervisor components."""

    def test_core_update_auto_apply_blocked(self, tmp_path, pueo_dirs):
        """When _pending_auto_apply=True for 'core', a notification is sent instead."""
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        notifier_calls = []

        class _CapturingNotifier:
            async def send(self, subject="", body="", payload=None, **kw):
                notifier_calls.append({"subject": subject, "body": body})

        # Simulate loop returning success + executor has _pending_auto_apply=True
        def _make_mock_loop(executor_ref):
            async def _fake_run(initial_context):
                executor_ref._pending_auto_apply = True
                return AgentLoopResult(outcome="success")

            m = MagicMock()
            m.run = _fake_run
            return m

        captured_executor = []

        original_ToolExecutor = None

        import utils.agent.tool_executor as _te_mod

        original_ToolExecutor = _te_mod.ToolExecutor

        class _SpyToolExecutor(original_ToolExecutor):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                captured_executor.append(self)

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
            mock.patch("utils.agent.tool_executor.ToolExecutor", _SpyToolExecutor),
        ):

            def _make_loop_side_effect(*a, **kw):
                if captured_executor:
                    return _make_mock_loop(captured_executor[-1])
                return MagicMock()

            MockLoop.side_effect = _make_loop_side_effect

            from agents.ha_update_manager import _run_update_analysis

            asyncio.run(
                _run_update_analysis(
                    update=_FakeUpdate(),
                    notifier=_CapturingNotifier(),
                )
            )

        # A warning notification must have been sent instead of auto-apply
        block_msgs = [
            c for c in notifier_calls if "blocked" in c.get("body", "").lower()
        ]
        assert block_msgs, "Core update auto-apply must be blocked with a notification"


# ---------------------------------------------------------------------------
# Phase 2: sanitize + device_context_summary + upgrade_advisor wiring
# ---------------------------------------------------------------------------


def _base_patches(db_path):
    """Common mock.patch context managers for _run_update_analysis tests."""
    from unittest import mock

    return [
        mock.patch("utils.agent.agent_loop.AgentLoop"),
        mock.patch("utils.agent.supervisor.increment_active_agent"),
        mock.patch("utils.agent.supervisor.decrement_active_agent"),
        mock.patch(
            "utils.agent.supervisor.make_activity_timeline_callback",
            return_value=None,
        ),
        mock.patch("utils.llm.llm_factory.make_llm_client"),
        mock.patch("agents.ha_update_manager.DB_PATH", db_path),
        mock.patch("utils.agent.work_queue.get_work_queue_or_none", return_value=None),
    ]


class _FakeUpdateBasic:
    entity_id = "update.home_assistant_core_update"
    component = "Home Assistant Core"
    installed_version = "2026.9.0"
    latest_version = "2026.9.1"
    release_url = None
    release_summary = None


class TestUpdateAnalysisContextBuilding:
    """Verify sanitize, device_context_summary, and upgrade_advisor wiring."""

    def _run(self, tmp_path, **kwargs):
        """Run _run_update_analysis and return the context string passed to loop.run()."""
        import asyncio
        from contextlib import ExitStack
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult
        from utils.hitl.notify import FakeNotifier

        db_path = _make_db(tmp_path)
        captured_context = []

        with ExitStack() as stack:
            patches = _base_patches(db_path)
            mocks = [stack.enter_context(p) for p in patches]
            MockLoop = mocks[0]

            mock_instance = MagicMock()

            async def _capture_run(ctx):
                captured_context.append(ctx)
                return AgentLoopResult(outcome="success")

            mock_instance.run = _capture_run
            MockLoop.return_value = mock_instance
            mocks[4].return_value = MagicMock()  # make_llm_client

            from agents.ha_update_manager import _run_update_analysis

            asyncio.run(
                _run_update_analysis(
                    update=_FakeUpdateBasic(),
                    notifier=FakeNotifier(),
                    **kwargs,
                )
            )

        return captured_context[0] if captured_context else ""

    def test_sanitize_strips_bearer_token(self, tmp_path, pueo_dirs):
        from unittest import mock

        with mock.patch.object(
            _FakeUpdateBasic,
            "release_summary",
            new="Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.longtoken",
        ):
            update = _FakeUpdateBasic()
            update.release_summary = (
                "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.longtoken"
            )

        import asyncio
        from contextlib import ExitStack
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult
        from utils.hitl.notify import FakeNotifier

        db_path = _make_db(tmp_path)
        captured_context = []

        class _SensitiveUpdate(_FakeUpdateBasic):
            release_summary = (
                "update summary Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.longtoken"
            )

        with ExitStack() as stack:
            patches = _base_patches(db_path)
            mocks = [stack.enter_context(p) for p in patches]
            MockLoop = mocks[0]
            mock_instance = MagicMock()

            async def _capture_run(ctx):
                captured_context.append(ctx)
                return AgentLoopResult(outcome="success")

            mock_instance.run = _capture_run
            MockLoop.return_value = mock_instance
            mocks[4].return_value = MagicMock()

            from agents.ha_update_manager import _run_update_analysis

            asyncio.run(
                _run_update_analysis(
                    update=_SensitiveUpdate(),
                    notifier=FakeNotifier(),
                )
            )

        ctx = captured_context[0]
        assert "Bearer <REDACTED>" in ctx
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in ctx

    def test_profile_summary_appended(self, tmp_path, pueo_dirs):
        from utils.ha.ha_environment import HAEnvironmentProfile

        profile = HAEnvironmentProfile(
            ha_version="2026.9.0",
            installed_integrations=["mqtt", "zha"],
        )
        ctx = self._run(tmp_path, ha_profile=profile)
        assert "Environment summary" in ctx
        assert "2026.9.0" in ctx

    def test_no_profile_no_summary(self, tmp_path, pueo_dirs):
        ctx = self._run(tmp_path)
        assert "Environment summary" not in ctx

    def test_upgrade_advisor_sensor_prepended(self, tmp_path, pueo_dirs):
        from utils.ha.ha_rest_client import FakeHARestClient

        # The new advisor integration uses sensor.upgrade_advisor_status and _risk.
        # available_version must match the update's latest_version ("2026.9.1").
        states = [
            {
                "entity_id": "sensor.upgrade_advisor_status",
                "state": "report_ready",
                "attributes": {
                    "available_version": "2026.9.1",
                    "breaking_change_count": 1,
                    "report": "One breaking change found.",
                },
            },
            {
                "entity_id": "sensor.upgrade_advisor_risk",
                "state": "medium",
                "attributes": {},
            },
        ]
        rest = FakeHARestClient(states=states)
        ctx = self._run(tmp_path, ha_rest_client=rest)
        assert "upgrade-advisor" in ctx.lower()
        assert "medium" in ctx
        # Must appear before the update line
        assert ctx.lower().index("upgrade-advisor") < ctx.index("Available update")

    def test_upgrade_advisor_not_installed_no_error(self, tmp_path, pueo_dirs):
        from utils.ha.ha_rest_client import FakeHARestClient

        rest = FakeHARestClient(states=[])
        ctx = self._run(tmp_path, ha_rest_client=rest)
        assert "Available update" in ctx  # still runs fine


# ---------------------------------------------------------------------------
# execute_core_update — post-upgrade advisor verification
# ---------------------------------------------------------------------------


class _FakeCoreUpdate:
    component = "core"
    entity_id = "update.home_assistant_core_update"
    installed_version = "2026.9.0"
    latest_version = "2026.10.0"
    release_url = None
    release_summary = None


class TestExecuteCoreUpdateAdvisor:
    """Verify post-upgrade advisor integration in execute_core_update."""

    def _run_core_update(self, ha_rest_client=None):
        """Run execute_core_update with a success-always poll and return notifier."""
        import asyncio
        from contextlib import ExitStack
        from unittest.mock import AsyncMock, MagicMock, patch

        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        ssh = FakeSSHClient(
            command_results={
                "ha core update": (0, "", ""),
                "ha core check": (0, "", ""),
            }
        )
        notifier = FakeNotifier()
        gate = MagicMock()

        async def _always_success(*_a, **_kw):
            return True

        from agents.ha_update_manager import execute_core_update

        with ExitStack() as stack:
            # Patched at source modules because they are imported locally inside
            # execute_core_update's body.
            stack.enter_context(
                patch(
                    "agents.ha_agent_advanced.execute_remote_backup",
                    new=AsyncMock(return_value="slug123"),
                )
            )
            stack.enter_context(patch("agents.ha_agent_advanced.record_backup_slug"))
            stack.enter_context(
                patch(
                    "agents.ha_agent_advanced.offload_backup_to_local",
                    new=AsyncMock(),
                )
            )
            stack.enter_context(
                patch(
                    "agents.ha_update_manager.run_pueo_self_check",
                    new=AsyncMock(return_value=None),
                )
            )

            class _FakeEval:
                root_cause_summary = ""
                is_actionable = False
                confidence_score = 0.0

            stack.enter_context(
                patch(
                    "agents.ha_log_monitor.analyze_log_line_with_ai",
                    new=AsyncMock(return_value=(_FakeEval(), None)),
                )
            )

            asyncio.run(
                execute_core_update(
                    update=_FakeCoreUpdate(),
                    ssh_client=ssh,
                    notifier=notifier,
                    gate=gate,
                    _poll=_always_success,
                    ha_rest_client=ha_rest_client,
                )
            )

        return notifier

    def test_advisor_regressions_in_card_payload(self):
        from utils.ha.ha_rest_client import FakeHARestClient

        states = [
            {
                "entity_id": "sensor.upgrade_advisor_status",
                "state": "report_ready",
                "attributes": {
                    "post_upgrade_status": "regressions",
                    "post_upgrade_regressions": ["automation.foo broke"],
                },
            }
        ]
        rest = FakeHARestClient(states=states)
        notifier = self._run_core_update(ha_rest_client=rest)

        assert notifier.sent, "no notification sent"
        payload = notifier.sent[0]["payload"]
        assert "advisor_regressions" in payload
        assert payload["advisor_regressions"] == ["automation.foo broke"]

    def test_advisor_regressions_in_card_body(self):
        from utils.ha.ha_rest_client import FakeHARestClient

        states = [
            {
                "entity_id": "sensor.upgrade_advisor_status",
                "state": "report_ready",
                "attributes": {
                    "post_upgrade_status": "regressions",
                    "post_upgrade_regressions": ["light.bar missing"],
                },
            }
        ]
        rest = FakeHARestClient(states=states)
        notifier = self._run_core_update(ha_rest_client=rest)

        body = notifier.sent[0]["body"]
        assert "light.bar missing" in body

    def test_no_rest_client_no_advisor_check(self):
        """Without ha_rest_client the card still sends with no advisor_regressions."""
        notifier = self._run_core_update(ha_rest_client=None)
        assert notifier.sent
        payload = notifier.sent[0]["payload"]
        assert "advisor_regressions" not in payload

    def test_advisor_not_installed_no_error(self):
        """Empty advisor sensor raises → regression check is silently skipped."""
        from utils.ha.ha_rest_client import FakeHARestClient

        rest = FakeHARestClient(states=[])
        notifier = self._run_core_update(ha_rest_client=rest)
        assert notifier.sent
        payload = notifier.sent[0]["payload"]
        assert "advisor_regressions" not in payload

    def test_advisor_no_regressions_payload_empty_list(self):
        """post_upgrade_status present but no regressions → empty list in payload."""
        from utils.ha.ha_rest_client import FakeHARestClient

        states = [
            {
                "entity_id": "sensor.upgrade_advisor_status",
                "state": "report_ready",
                "attributes": {
                    "post_upgrade_status": "ok",
                    "post_upgrade_regressions": [],
                },
            }
        ]
        rest = FakeHARestClient(states=states)
        notifier = self._run_core_update(ha_rest_client=rest)
        payload = notifier.sent[0]["payload"]
        assert "advisor_regressions" in payload
        assert payload["advisor_regressions"] == []


# ---------------------------------------------------------------------------
# Race guard — _finish_update_analysis skips card when already approved
# ---------------------------------------------------------------------------


class TestFinishUpdateAnalysisRaceGuard:
    """Fix 1: _finish_update_analysis does not send a duplicate card when
    hitl_suppression already shows last_action='approved' for the key."""

    def _make_executor(self, db_path):
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_executor import ToolExecutor

        notifier = MagicMock()
        notifier.send = AsyncMock()
        executor = ToolExecutor(
            ha_ssh_client=MagicMock(),
            gate=None,
            notifier=notifier,
            db_path=db_path,
        )
        return executor, notifier

    def _make_update(self, component="noaa_it_all"):
        from types import SimpleNamespace

        return SimpleNamespace(
            entity_id=f"update.{component}",
            component=component,
            installed_version="0.7.1",
            latest_version="0.7.3",
            release_url=None,
            release_summary=None,
        )

    def test_skips_card_when_suppression_already_approved(self, tmp_path, pueo_dirs):
        """Race guard skips card only when BOTH base row approved AND analyzed_key present."""
        import asyncio

        db_path = _make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            # base approval row — resolved_at IS NULL (active approval)
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, last_action)"
                " VALUES (?, ?, ?)",
                ("update:update.noaa_it_all", "update", "approved"),
            )
            # analyzed key for this exact version — proves approval is for 0.7.3
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, last_action)"
                " VALUES (?, ?, ?)",
                ("update_analyzed:update.noaa_it_all:0.7.3", "update", "approved"),
            )

        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update("noaa_it_all"))

        result = asyncio.run(
            executor._finish_update_analysis(
                safe_to_update=True,
                breaking_changes=[],
                affected_config_keys=[],
                pueo_command_risks=[],
                recommendation="Patch update.",
                instance_impact="none",
                proposed_config_fixes=[],
                create_hitl_card=True,
            )
        )

        assert result.success
        assert "skipping duplicate" in result.output
        assert (
            not notifier.send.called
        ), "No HITL card should be sent when already approved"

    def test_sends_card_when_not_yet_approved(self, tmp_path, pueo_dirs):
        """_finish_update_analysis sends a card when no approved row exists."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update("noaa_it_all"))

        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Patch update.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=True,
                )
            )

        assert result.success
        assert (
            notifier.send.called
        ), "HITL card should be sent when not already approved"

    def test_sends_card_when_approval_already_resolved(self, tmp_path, pueo_dirs):
        """Stale approval (resolved_at set) must NOT suppress a new card.

        Regression for the OTBR 3.2.0→3.2.1 bug: old approval row had
        resolved_at set but the guard still matched it.
        """
        import asyncio
        import time
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            # Old approval row with resolved_at set — the approval is done.
            conn.execute(
                "INSERT INTO hitl_suppression"
                " (card_key, card_type, last_action, resolved_at)"
                " VALUES (?, ?, ?, ?)",
                ("update:update.noaa_it_all", "update", "approved", time.time()),
            )

        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update("noaa_it_all"))

        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Patch update.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=True,
                )
            )

        assert result.success
        assert notifier.send.called, "Card must be sent when old approval is resolved"

    def test_sends_card_when_analyzed_key_is_different_version(
        self, tmp_path, pueo_dirs
    ):
        """Approval for a previous version must not suppress the next version's card."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            # Base row approved, unresolved.
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, last_action)"
                " VALUES (?, ?, ?)",
                ("update:update.noaa_it_all", "update", "approved"),
            )
            # analyzed_key exists but for version 0.7.1, not 0.7.3.
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, last_action)"
                " VALUES (?, ?, ?)",
                ("update_analyzed:update.noaa_it_all:0.7.1", "update", "approved"),
            )

        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(
            self._make_update("noaa_it_all")
        )  # latest_version=0.7.3

        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Patch update.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=True,
                )
            )

        assert result.success
        assert (
            notifier.send.called
        ), "Card must be sent when analyzed_key is a different version"

    def test_timeline_event_written_on_card_sent(self, tmp_path, pueo_dirs):
        """A timeline event is written when an approval card is sent."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        executor, _notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update("noaa_it_all"))

        timeline_calls = []
        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            with patch(
                "utils.core.timeline.write_timeline_event",
                side_effect=lambda *a, **kw: timeline_calls.append(a),
            ):
                asyncio.run(
                    executor._finish_update_analysis(
                        safe_to_update=True,
                        breaking_changes=[],
                        affected_config_keys=[],
                        pueo_command_risks=[],
                        recommendation="Patch update.",
                        instance_impact="none",
                        proposed_config_fixes=[],
                        create_hitl_card=True,
                    )
                )

        assert any(
            "approval card sent" in str(args) for args in timeline_calls
        ), "Expected timeline event mentioning 'approval card sent'"

    def test_timeline_event_written_on_skipped(self, tmp_path, pueo_dirs):
        """A timeline event is written when the race guard skips the card."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, last_action)"
                " VALUES (?, ?, ?)",
                ("update:update.noaa_it_all", "update", "approved"),
            )
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, last_action)"
                " VALUES (?, ?, ?)",
                ("update_analyzed:update.noaa_it_all:0.7.3", "update", "approved"),
            )

        executor, _notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update("noaa_it_all"))

        timeline_calls = []
        with patch(
            "utils.core.timeline.write_timeline_event",
            side_effect=lambda *a, **kw: timeline_calls.append(a),
        ):
            asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Patch update.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=True,
                )
            )

        assert any(
            "skipped" in str(args) for args in timeline_calls
        ), "Expected timeline event mentioning 'skipped'"


# ---------------------------------------------------------------------------
# _is_update_wake_worthy  (wake-storm filter)
# ---------------------------------------------------------------------------


class TestIsUpdateWakeWorthy:
    """_is_update_wake_worthy filters attribute-only update.* state changes."""

    def _make_ev(
        self,
        entity_id="update.openthread_border_router_update",
        old_state=None,
        new_state=None,
    ):
        data = {"entity_id": entity_id}
        if old_state is not None:
            data["old_state"] = old_state
        if new_state is not None:
            data["new_state"] = new_state
        return {
            "event_type": "state_changed",
            "data": data,
        }

    def test_state_value_change_is_worthy(self):
        from main import _is_update_wake_worthy

        ev = self._make_ev(
            old_state={"state": "off", "attributes": {"latest_version": "3.2.0"}},
            new_state={"state": "on", "attributes": {"latest_version": "3.2.1"}},
        )
        assert _is_update_wake_worthy(ev) is True

    def test_latest_version_change_is_worthy(self):
        from main import _is_update_wake_worthy

        ev = self._make_ev(
            old_state={"state": "on", "attributes": {"latest_version": "3.2.0"}},
            new_state={"state": "on", "attributes": {"latest_version": "3.2.1"}},
        )
        assert _is_update_wake_worthy(ev) is True

    def test_attribute_only_in_progress_not_worthy(self):
        """in_progress / update_percentage changes must NOT wake the loop."""
        from main import _is_update_wake_worthy

        ev = self._make_ev(
            old_state={
                "state": "on",
                "attributes": {"latest_version": "3.2.1", "in_progress": False},
            },
            new_state={
                "state": "on",
                "attributes": {"latest_version": "3.2.1", "in_progress": True},
            },
        )
        assert _is_update_wake_worthy(ev) is False

    def test_update_percentage_change_not_worthy(self):
        from main import _is_update_wake_worthy

        ev = self._make_ev(
            old_state={
                "state": "on",
                "attributes": {"latest_version": "3.2.1", "update_percentage": 10},
            },
            new_state={
                "state": "on",
                "attributes": {"latest_version": "3.2.1", "update_percentage": 80},
            },
        )
        assert _is_update_wake_worthy(ev) is False

    def test_missing_old_state_is_worthy(self):
        """First observation of an entity (no old_state) should wake."""
        from main import _is_update_wake_worthy

        ev = self._make_ev(
            new_state={"state": "on", "attributes": {"latest_version": "3.2.1"}}
        )
        assert _is_update_wake_worthy(ev) is True

    def test_non_update_entity_state_value_change_is_worthy(self):
        """Non-update entities pass through the function (callers already filter)."""
        from main import _is_update_wake_worthy

        ev = self._make_ev(
            entity_id="light.living_room",
            old_state={"state": "off", "attributes": {}},
            new_state={"state": "on", "attributes": {}},
        )
        assert _is_update_wake_worthy(ev) is True


# ---------------------------------------------------------------------------
# fetch_release_notes_cached — WS-first, per-entity cache key
# ---------------------------------------------------------------------------


class TestFetchReleaseNotesCachedWS:
    """WS-first fetch order and per-entity cache key (non-core add-ons)."""

    def test_ws_fetcher_used_first_for_non_core(self, tmp_path):
        """For a non-core add-on, WS notes take priority over release_url."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        ws_notes = "## 3.2.1\nKeep retrying unavailable network RCPs."

        async def _fake_ws(entity_id):
            return ws_notes

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            return await fetch_release_notes_cached(
                "3.2.1",
                cache_dir=str(tmp_path / "cache"),
                entity_id="update.openthread_border_router_update",
                release_url="https://example.com/release",
                ws_fetcher=_fake_ws,
            )

        with patch(
            "agents.ha_update_manager._fetch_release_notes_from_url"
        ) as mock_url:
            result = asyncio.run(_run())

        mock_url.assert_not_called()
        assert "RCPs" in result

    def test_release_url_used_when_ws_returns_none(self, tmp_path):
        """Falls back to release_url when WS returns None."""
        import asyncio
        from unittest.mock import patch

        async def _fake_ws(entity_id):
            return None

        async def _fake_url(url):
            return "CHANGELOG from release_url"

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            return await fetch_release_notes_cached(
                "3.2.1",
                cache_dir=str(tmp_path / "cache"),
                entity_id="update.otbr_update",
                release_url="https://example.com/releases/3.2.1",
                ws_fetcher=_fake_ws,
            )

        with patch(
            "agents.ha_update_manager._fetch_release_notes_from_url",
            side_effect=_fake_url,
        ):
            result = asyncio.run(_run())

        assert "CHANGELOG from release_url" in result

    def test_concrete_url_fallback_when_ws_and_url_absent(self, tmp_path):
        """With no WS notes and no release_url, returns a concrete CHANGELOG URL."""
        import asyncio

        async def _fake_ws(entity_id):
            return None

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            return await fetch_release_notes_cached(
                "3.2.1",
                cache_dir=str(tmp_path / "cache"),
                entity_id="update.openthread_border_router_update",
                release_url=None,
                ws_fetcher=_fake_ws,
            )

        result = asyncio.run(_run())
        assert "openthread_border_router" in result
        assert "CHANGELOG.md" in result
        # Must not be an ambiguous <slug> placeholder
        assert "<" not in result

    def test_per_entity_cache_key_prevents_collision(self, tmp_path):
        """Two add-ons at the same version use separate cache files."""
        import asyncio

        call_count = 0

        async def _fake_ws(entity_id):
            nonlocal call_count
            call_count += 1
            return f"Notes for {entity_id}"

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            r1 = await fetch_release_notes_cached(
                "0.7.1",
                cache_dir=str(tmp_path / "cache"),
                entity_id="update.addon_a_update",
                ws_fetcher=_fake_ws,
            )
            r2 = await fetch_release_notes_cached(
                "0.7.1",
                cache_dir=str(tmp_path / "cache"),
                entity_id="update.addon_b_update",
                ws_fetcher=_fake_ws,
            )
            return r1, r2

        r1, r2 = asyncio.run(_run())
        # Both fetched independently (ws called once each → 2 total)
        assert call_count == 2
        assert "addon_a" in r1
        assert "addon_b" in r2

    def test_core_cache_key_unchanged(self, tmp_path):
        """HA Core versions continue to use the bare version as cache key."""
        import asyncio

        async def _fake_fetcher(version):
            return f"Core notes for {version}"

        async def _run():
            from agents.ha_update_manager import fetch_release_notes_cached

            return await fetch_release_notes_cached(
                "2026.9.2",
                cache_dir=str(tmp_path / "cache"),
                entity_id="update.home_assistant_core_update",
                _fetcher=_fake_fetcher,
            )

        asyncio.run(_run())
        cache_files = list((tmp_path / "cache").glob("*.txt"))
        assert len(cache_files) == 1
        assert cache_files[0].name == "2026.9.2.txt"


# ---------------------------------------------------------------------------
# _get_update_release_notes — version substring matching
# ---------------------------------------------------------------------------


class TestGetUpdateReleaseNotesMatcher:
    """_get_update_release_notes matches 'component version' strings."""

    def _make_executor_with_ws(self, update_release_notes: dict):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.agent.tool_executor import ToolExecutor

        notifier = MagicMock()
        notifier.send = AsyncMock()
        ws = FakeHAWebSocketClient(update_release_notes=update_release_notes)
        executor = ToolExecutor(
            ha_ssh_client=MagicMock(),
            gate=MagicMock(),
            notifier=notifier,
            db_path=":memory:",
        )
        executor.set_ws_client(ws)
        update = SimpleNamespace(
            entity_id="update.openthread_border_router_update",
            component="openthread_border_router",
            installed_version="3.2.0",
            latest_version="3.2.1",
            release_url=None,
            release_summary=None,
        )
        executor.set_update_status(update)
        return executor

    def test_component_version_string_matches_pending(self, tmp_path, pueo_dirs):
        """'openthread_border_router 3.2.1' matches the pending 3.2.1 update."""
        import asyncio
        from unittest.mock import patch

        notes_text = "## 3.2.1\nKeep retrying unavailable network RCPs."
        executor = self._make_executor_with_ws(
            {"update.openthread_border_router_update": notes_text}
        )
        import config as _cfg

        with patch.object(
            _cfg, "HA_UPDATE_RELEASE_NOTES_CACHE_DIR", str(tmp_path / "cache")
        ):
            result = asyncio.run(
                executor._get_update_release_notes("openthread_border_router 3.2.1")
            )

        assert result.success
        assert "RCPs" in result.output
        assert executor._release_notes_obtained is True

    def test_release_notes_obtained_false_on_unavailable(self, tmp_path, pueo_dirs):
        """_release_notes_obtained stays False when WS returns None."""
        import asyncio
        from unittest.mock import patch

        executor = self._make_executor_with_ws({})  # WS returns None
        import config as _cfg

        with patch.object(
            _cfg, "HA_UPDATE_RELEASE_NOTES_CACHE_DIR", str(tmp_path / "cache")
        ):
            result = asyncio.run(
                executor._get_update_release_notes("openthread_border_router 3.2.1")
            )

        assert result.success
        assert executor._release_notes_obtained is False
        assert (
            "unavailable" in result.output.lower()
            or "changelog" in result.output.lower()
        )


# ---------------------------------------------------------------------------
# _finish_update_analysis — no-notes override
# ---------------------------------------------------------------------------


class TestFinishUpdateAnalysisNoNotes:
    """No-notes override in _finish_update_analysis."""

    def _make_executor(self, db_path):
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.autonomy import AutonomyGate
        from utils.agent.tool_executor import ToolExecutor

        notifier = MagicMock()
        notifier.send = AsyncMock()
        # AUTONOMOUS level (3) would normally permit auto-apply for add-ons.
        gate = AutonomyGate(level=3)
        executor = ToolExecutor(
            ha_ssh_client=MagicMock(),
            gate=gate,
            notifier=notifier,
            db_path=db_path,
        )
        return executor, notifier

    def _make_update(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            entity_id="update.openthread_border_router_update",
            component="openthread_border_router",
            installed_version="3.2.0",
            latest_version="3.2.1",
            release_url=None,
            release_summary=None,
        )

    def test_no_notes_forces_card_and_caps_confidence(self, tmp_path, pueo_dirs):
        """When _release_notes_obtained=False, create_hitl_card=False is overridden."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update())
        # _release_notes_obtained defaults to False — no notes obtained

        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            result = asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Looks fine.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=False,  # LLM says no card
                    confidence=0.9,  # high confidence from model
                )
            )

        # Must have sent a card despite LLM saying no card
        assert notifier.send.called, "Card must be sent when no release notes obtained"
        assert result.success
        # _pending_auto_apply must NOT be set — no auto-apply without notes
        assert executor._pending_auto_apply is False

    def test_no_notes_recommendation_prefixed(self, tmp_path, pueo_dirs):
        """Recommendation is prefixed with the no-notes warning string."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update())

        sent_payloads = []

        async def _capture_send(**kwargs):
            sent_payloads.append(kwargs)

        notifier.send.side_effect = _capture_send

        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            asyncio.run(
                executor._finish_update_analysis(
                    safe_to_update=True,
                    breaking_changes=[],
                    affected_config_keys=[],
                    pueo_command_risks=[],
                    recommendation="Auto-apply is fine.",
                    instance_impact="none",
                    proposed_config_fixes=[],
                    create_hitl_card=False,
                    confidence=0.95,
                )
            )

        assert sent_payloads, "Expected notifier.send to be called"
        body = sent_payloads[0].get("body", "") or ""
        assert "Release notes could not be retrieved" in body or any(
            "Release notes could not be retrieved" in str(p) for p in sent_payloads
        )

    def test_with_notes_auto_applies_as_before(self, tmp_path, pueo_dirs):
        """When _release_notes_obtained=True, auto-apply proceeds normally."""
        import asyncio
        from unittest.mock import patch

        db_path = _make_db(tmp_path)
        executor, notifier = self._make_executor(db_path)
        executor.set_update_status(self._make_update())
        executor._release_notes_obtained = True  # notes were obtained

        with patch("agents.ha_log_monitor._update_mark_card_sent"):
            with patch("utils.core.timeline.write_timeline_event"):
                asyncio.run(
                    executor._finish_update_analysis(
                        safe_to_update=True,
                        breaking_changes=[],
                        affected_config_keys=[],
                        pueo_command_risks=[],
                        recommendation="Safe bug fix.",
                        instance_impact="none",
                        proposed_config_fixes=[],
                        create_hitl_card=False,  # LLM says no card — gate allows
                        confidence=0.9,
                    )
                )

        # Auto-apply should be signalled (no card sent, pending_auto_apply set)
        assert executor._pending_auto_apply is True
        assert not notifier.send.called


# ---------------------------------------------------------------------------
# poll_for_updates — in_progress skip
# ---------------------------------------------------------------------------


class TestPollForUpdatesInProgress:
    """poll_for_updates skips updates that are already in_progress."""

    def test_in_progress_update_skipped(self, tmp_path, pueo_dirs):
        """An update with in_progress=True must not trigger _run_update_analysis."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from agents.ha_log_monitor import _update_check_should_send

        in_progress_update = SimpleNamespace(
            entity_id="update.openthread_border_router_update",
            component="openthread_border_router",
            installed_version="3.2.0",
            latest_version="3.2.1",
            update_available=True,
            in_progress=True,  # ← install running
            release_url=None,
            release_summary=None,
        )

        run_called = []

        async def _fake_run_update_analysis(*args, **kwargs):
            run_called.append(True)
            return True

        async def _run():
            from agents.ha_log_monitor import poll_for_updates

            rest = MagicMock()
            rest.get_states = AsyncMock(return_value=[])
            with (
                patch(
                    "agents.ha_log_monitor.get_update_status",
                    AsyncMock(return_value=[in_progress_update]),
                ),
                patch(
                    "agents.ha_log_monitor._update_check_should_send",
                    return_value=True,
                ),
                patch(
                    "agents.ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE",
                    True,
                ),
                patch(
                    "agents.ha_log_monitor._run_update_analysis_ref",
                    _fake_run_update_analysis,
                    create=True,
                ),
                patch(
                    "agents.ha_update_manager._run_update_analysis",
                    _fake_run_update_analysis,
                ),
                patch(
                    "agents.ha_log_monitor.HA_UPDATE_CHECK_INTERVAL_HOURS",
                    0,
                ),
                # _update_sweep_absent_pending hits DB; stub it out since we are
                # not testing that path here.
                patch(
                    "agents.ha_log_monitor._update_sweep_absent_pending",
                    return_value=[],
                ),
                # reconcile_stale_approved_cards is a deferred import from ha_update_manager.
                patch(
                    "agents.ha_update_manager.reconcile_stale_approved_cards",
                    return_value=0,
                ),
            ):
                # Run only one iteration by making sleep raise to break the while loop.
                async def _raise(*a, **kw):
                    raise asyncio.CancelledError

                with patch("asyncio.sleep", side_effect=_raise):
                    try:
                        await poll_for_updates(ha_rest_client=rest)
                    except asyncio.CancelledError:
                        pass

        asyncio.run(_run())
        assert run_called == [], "in_progress update must not trigger analysis"


# ---------------------------------------------------------------------------
# _run_update_analysis — returns bool
# ---------------------------------------------------------------------------


class TestRunUpdateAnalysisReturnsBool:
    """_run_update_analysis returns the work-queue submit result."""

    def _make_update(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            entity_id="update.test_addon_update",
            component="test_addon",
            installed_version="1.0.0",
            latest_version="1.0.1",
            update_available=True,
            in_progress=False,
            release_url=None,
            release_summary=None,
        )

    def test_returns_false_on_dedup(self, tmp_path, pueo_dirs):
        """Returns False when the work queue deduplicates the submission."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        fake_wq = MagicMock()
        fake_wq.submit = AsyncMock(return_value=False)  # deduped

        # get_work_queue_or_none is a deferred import inside _run_update_analysis;
        # patch at the source module, not the caller.
        with (
            patch(
                "utils.agent.work_queue.get_work_queue_or_none",
                return_value=fake_wq,
            ),
            patch("utils.llm.llm_factory.make_llm_client", MagicMock()),
            patch("utils.agent.agent_loop.AgentLoop", MagicMock()),
        ):
            result = asyncio.run(
                __import__(
                    "agents.ha_update_manager",
                    fromlist=["_run_update_analysis"],
                )._run_update_analysis(self._make_update())
            )

        assert result is False

    def test_returns_true_on_accept(self, tmp_path, pueo_dirs):
        """Returns True when the work queue accepts the item."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        fake_wq = MagicMock()
        fake_wq.submit = AsyncMock(return_value=True)

        with (
            patch(
                "utils.agent.work_queue.get_work_queue_or_none",
                return_value=fake_wq,
            ),
            patch("utils.llm.llm_factory.make_llm_client", MagicMock()),
            patch("utils.agent.agent_loop.AgentLoop", MagicMock()),
        ):
            result = asyncio.run(
                __import__(
                    "agents.ha_update_manager",
                    fromlist=["_run_update_analysis"],
                )._run_update_analysis(self._make_update())
            )

        assert result is True


# ---------------------------------------------------------------------------
# execute_addon_update — timeline events
# ---------------------------------------------------------------------------


class TestExecuteAddonUpdateTimeline:
    """execute_addon_update writes timeline events for backup, install start, result."""

    def _make_update(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            entity_id="update.test_addon_update",
            component="test_addon",
            installed_version="1.0.0",
            latest_version="1.0.1",
            update_available=True,
            in_progress=False,
            release_url=None,
            release_summary=None,
        )

    def test_timeline_events_on_success(self, tmp_path, pueo_dirs):
        """Backup created + install started + success result events are written."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        timeline_calls = []

        async def _fake_backup(**kwargs):
            return "abc123"

        async def _fake_poll(*args, **kwargs):
            return True

        fake_notifier = MagicMock()
        fake_notifier.send = AsyncMock()
        fake_gate = MagicMock()
        fake_ssh = MagicMock()
        # Rest client must be a mock with an AsyncMock call_service so the
        # install service call succeeds (and we reach the poll path).
        fake_rest = MagicMock()
        fake_rest.call_service = AsyncMock()

        with (
            patch(
                "agents.ha_agent_advanced.execute_remote_backup",
                side_effect=_fake_backup,
            ),
            patch("agents.ha_agent_advanced.record_backup_slug"),
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                AsyncMock(),
            ),
            patch(
                "agents.ha_update_manager._poll_addon_update_via_rest",
                side_effect=_fake_poll,
            ),
            patch("agents.ha_update_manager._send_post_update_card", AsyncMock()),
            patch("agents.ha_update_manager.set_restart_pending_after_update"),
            patch("agents.ha_update_manager._post_update_repair_scan", AsyncMock()),
            patch("asyncio.create_task"),
            patch(
                "utils.core.timeline.write_timeline_event",
                side_effect=lambda *a, **kw: timeline_calls.append(a),
            ),
        ):
            asyncio.run(
                __import__(
                    "agents.ha_update_manager",
                    fromlist=["execute_addon_update"],
                ).execute_addon_update(
                    self._make_update(),
                    ssh_client=fake_ssh,
                    notifier=fake_notifier,
                    gate=fake_gate,
                    ha_rest_client=fake_rest,
                )
            )

        # write_timeline_event(level, source, msg, extra) → 4-tuple; extra is index 3
        steps = [
            a[3].get("step")
            for a in timeline_calls
            if len(a) >= 4 and isinstance(a[3], dict)
        ]
        assert "backup_created" in steps
        assert "install_started" in steps
        assert "install_result" in steps
        result_ev = next(
            a
            for a in timeline_calls
            if len(a) >= 4
            and isinstance(a[3], dict)
            and a[3].get("step") == "install_result"
        )
        assert result_ev[3]["success"] is True

    def test_timeline_event_on_failure(self, tmp_path, pueo_dirs):
        """A failure result timeline event is written when the poll times out."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        timeline_calls = []

        async def _fake_backup(**kwargs):
            return "abc123"

        async def _fake_poll_fail(*args, **kwargs):
            return False

        fake_notifier = MagicMock()
        fake_notifier.send = AsyncMock()
        fake_rest = MagicMock()
        fake_rest.call_service = AsyncMock()

        with (
            patch(
                "agents.ha_agent_advanced.execute_remote_backup",
                side_effect=_fake_backup,
            ),
            patch("agents.ha_agent_advanced.record_backup_slug"),
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                AsyncMock(),
            ),
            patch(
                "agents.ha_update_manager._poll_addon_update_via_rest",
                side_effect=_fake_poll_fail,
            ),
            patch("agents.ha_update_manager._send_post_update_card", AsyncMock()),
            patch(
                "utils.core.timeline.write_timeline_event",
                side_effect=lambda *a, **kw: timeline_calls.append(a),
            ),
        ):
            asyncio.run(
                __import__(
                    "agents.ha_update_manager",
                    fromlist=["execute_addon_update"],
                ).execute_addon_update(
                    self._make_update(),
                    ssh_client=MagicMock(),
                    notifier=fake_notifier,
                    gate=MagicMock(),
                    ha_rest_client=fake_rest,
                )
            )

        result_events = [
            a
            for a in timeline_calls
            if len(a) >= 4
            and isinstance(a[3], dict)
            and a[3].get("step") == "install_result"
        ]
        assert result_events, "Expected an install_result timeline event on failure"
        assert result_events[0][3]["success"] is False


# ---------------------------------------------------------------------------
# FakeHAWebSocketClient — get_update_release_notes
# ---------------------------------------------------------------------------


class TestFakeWSClientReleaseNotes:
    """FakeHAWebSocketClient supports get_update_release_notes."""

    def test_returns_configured_notes(self):
        import asyncio

        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient(
            update_release_notes={
                "update.otbr": "## 3.2.1\nBug fix.",
            }
        )
        result = asyncio.run(fake.get_update_release_notes("update.otbr"))
        assert result == "## 3.2.1\nBug fix."
        assert "get_update_release_notes:update.otbr" in fake.calls

    def test_returns_none_for_missing_entity(self):
        import asyncio

        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient()
        result = asyncio.run(fake.get_update_release_notes("update.unknown"))
        assert result is None
