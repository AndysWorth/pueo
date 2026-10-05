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

        # autonomy_level=3 (GUIDED) → only LOW risk auto-executes;
        # updates are MEDIUM so gate blocks auto-execute → card required
        gate = AutonomyGate(level=3)
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

        # autonomy_level=4 (AUTONOMOUS) → MEDIUM risk auto-executes
        gate = AutonomyGate(level=4)
        executor, notifier = self._make_executor(gate)
        update = self._make_update("noaa_it_all")
        executor.set_update_status(update)

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

        gate = AutonomyGate(level=4)
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
