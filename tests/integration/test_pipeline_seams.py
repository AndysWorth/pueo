"""Integration tests: full pipeline paths that are 0% covered in the unit suite.

These tests exercise multi-module state transitions using fakes at the
SSH/LLM/notifier/DB boundaries only — no live HA or Ollama required.
"""

import asyncio
import json
import sqlite3

import pytest


class TestLogMonitorActionablePath:
    """Tests the actionable code path in ha_log_monitor.tail_remote_log_stream().

    Lines 167–221 of ha_log_monitor.py are 0% covered in the unit suite because
    the existing unit tests stop before the autonomy gate / notifier dispatch.
    This class exercises the full: CRITICAL log line → AI triage → gate blocks
    auto-execute → HITL card sent via notifier.
    """

    _CRITICAL_LINE = "ERROR Component error: Template rendering failed"

    def _make_log_eval_json(self, is_actionable=True, confidence=0.95):
        from ha_log_monitor import LogEvaluation

        return LogEvaluation(
            is_actionable=is_actionable,
            root_cause_summary="Template rendering failed",
            confidence_score=confidence,
        ).model_dump_json()

    @pytest.fixture
    def ssh_with_critical_line(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(stream_data=[self._CRITICAL_LINE])

    @pytest.fixture
    def ssh_with_info_line(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(stream_data=["INFO Loaded component light"])

    def _patch_module_globals(self, monkeypatch):
        """Reset module-level globals so tests don't bleed into each other."""
        import ha_log_monitor
        from utils.rate_limiter import Debouncer, RateLimiter

        monkeypatch.setattr(ha_log_monitor, "SELF_HEALING_ENABLED", True)
        monkeypatch.setattr(ha_log_monitor, "CONFIDENCE_THRESHOLD", 0.7)
        monkeypatch.setattr(ha_log_monitor, "_debouncer", Debouncer(0))
        monkeypatch.setattr(ha_log_monitor, "_rate_limiter", RateLimiter(100, 3600))
        ha_log_monitor._log_buffer.clear()

    def test_critical_line_sends_hitl_card_when_gate_blocks(
        self, monkeypatch, ssh_with_critical_line
    ):
        """CRITICAL log line → triage → gate blocks → notifier receives a HITL card."""
        import ha_log_monitor
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        self._patch_module_globals(monkeypatch)

        llm = FakeLLMClient(
            self._make_log_eval_json(is_actionable=True, confidence=0.95)
        )
        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()

        asyncio.run(
            ha_log_monitor.tail_remote_log_stream(
                ssh_client=ssh_with_critical_line,
                llm_client=llm,
                gate=gate,
                notifier=notifier,
            )
        )

        assert len(notifier.sent) == 1
        card = notifier.sent[0]
        assert "cause" in card["payload"]
        assert "log_buffer_snapshot" in card["payload"]["evidence_raw"]
        assert card["payload"]["cause"] == "Template rendering failed"

    def test_info_line_does_not_trigger_notifier(self, monkeypatch, ssh_with_info_line):
        """INFO log line below CRITICAL_LOG_PATTERN does not reach the notifier."""
        import ha_log_monitor
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        self._patch_module_globals(monkeypatch)

        llm = FakeLLMClient(
            self._make_log_eval_json(is_actionable=True, confidence=0.95)
        )
        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()

        asyncio.run(
            ha_log_monitor.tail_remote_log_stream(
                ssh_client=ssh_with_info_line,
                llm_client=llm,
                gate=gate,
                notifier=notifier,
            )
        )

        # INFO line doesn't match CRITICAL_LOG_PATTERN — no AI triage, no card
        assert len(notifier.sent) == 0

    def test_low_confidence_does_not_trigger_notifier(
        self, monkeypatch, ssh_with_critical_line
    ):
        """CRITICAL line with confidence below threshold does not send a card."""
        import ha_log_monitor
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        self._patch_module_globals(monkeypatch)

        # confidence 0.3 < CONFIDENCE_THRESHOLD 0.7
        llm = FakeLLMClient(
            self._make_log_eval_json(is_actionable=True, confidence=0.3)
        )
        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()

        asyncio.run(
            ha_log_monitor.tail_remote_log_stream(
                ssh_client=ssh_with_critical_line,
                llm_client=llm,
                gate=gate,
                notifier=notifier,
            )
        )

        assert len(notifier.sent) == 0

    def test_self_healing_disabled_skips_notifier(
        self, monkeypatch, ssh_with_critical_line
    ):
        """When SELF_HEALING_ENABLED is False, no HITL card is sent."""
        import ha_log_monitor
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient
        from utils.rate_limiter import Debouncer, RateLimiter

        monkeypatch.setattr(ha_log_monitor, "SELF_HEALING_ENABLED", False)
        monkeypatch.setattr(ha_log_monitor, "CONFIDENCE_THRESHOLD", 0.7)
        monkeypatch.setattr(ha_log_monitor, "_debouncer", Debouncer(0))
        monkeypatch.setattr(ha_log_monitor, "_rate_limiter", RateLimiter(100, 3600))
        ha_log_monitor._log_buffer.clear()

        llm = FakeLLMClient(
            self._make_log_eval_json(is_actionable=True, confidence=0.95)
        )
        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()

        asyncio.run(
            ha_log_monitor.tail_remote_log_stream(
                ssh_client=ssh_with_critical_line,
                llm_client=llm,
                gate=gate,
                notifier=notifier,
            )
        )

        assert len(notifier.sent) == 0

    def test_auto_execute_gate_does_not_send_hitl_card(
        self, monkeypatch, ssh_with_critical_line
    ):
        """When gate.should_auto_execute returns True, no HITL card is sent (repair is triggered)."""
        import ha_log_monitor
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        self._patch_module_globals(monkeypatch)

        # Prevent the asyncio.create_task(trigger_remediation_pipeline()) from doing anything real
        async def _noop():
            pass

        monkeypatch.setattr(ha_log_monitor, "trigger_remediation_pipeline", _noop)
        # Also suppress the cooldown sleep
        monkeypatch.setattr(ha_log_monitor, "REPAIR_COOLDOWN_SECONDS", 0)

        llm = FakeLLMClient(
            self._make_log_eval_json(is_actionable=True, confidence=0.95)
        )
        gate = FakeAutonomyGate(auto_execute_result=True)
        notifier = FakeNotifier()

        asyncio.run(
            ha_log_monitor.tail_remote_log_stream(
                ssh_client=ssh_with_critical_line,
                llm_client=llm,
                gate=gate,
                notifier=notifier,
            )
        )

        # Auto-execute path: repair task created, no HITL card
        assert len(notifier.sent) == 0


class TestPollForNotificationsLoopBody:
    """Tests ha_log_monitor.poll_for_notifications() — 0% covered in the unit suite.

    The function is a `while True` daemon loop. Tests cancel on the second sleep call,
    which allows one full processing iteration plus one no-op round.
    """

    # Real HA WebSocket format: notification_id uses hyphens (e.g. "http-login")
    _NOTIF = {
        "notification_id": "http-login",
        "title": "Login attempt failed",
        "message": "Failed login from 1.2.3.4",
    }

    def _make_llm_json(self):
        from ha_notification_manager import _NotificationLLMOutput

        return _NotificationLLMOutput(
            human_explanation="Someone tried to log into HA from IP 1.2.3.4.",
            recommended_action="Check if you recognise this device.",
            requires_hitl=True,
        ).model_dump_json()

    def _one_shot_sleep(self, monkeypatch):
        """Return a patched asyncio.sleep that allows the first call, then raises CancelledError."""
        import ha_log_monitor

        call_count = {"n": 0}

        async def fake_sleep(_t):
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise asyncio.CancelledError()

        monkeypatch.setattr(ha_log_monitor.asyncio, "sleep", fake_sleep)

    def test_new_notification_writes_card_file_and_db_row(self, monkeypatch, tmp_path):
        """One iteration: WS notification fetched → card JSON written → notification_history row inserted."""
        import ha_agent_advanced
        import ha_log_monitor
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FileNotifier
        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        import ha_notification_manager

        db_path = str(tmp_path / "test.db")
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        ha_agent_advanced.init_local_database()

        # Disable auth-failure enrichment so no real DNS calls are made
        monkeypatch.setattr(
            ha_notification_manager, "HA_NOTIFICATION_ENRICH_AUTH_FAILURES", False
        )

        self._one_shot_sleep(monkeypatch)
        monkeypatch.setattr(ha_log_monitor, "HA_NOTIFICATION_POLL_INTERVAL_MINUTES", 0)

        ws = FakeHAWebSocketClient(notifications=[self._NOTIF])
        llm = FakeLLMClient(self._make_llm_json())
        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        notifier = FileNotifier(str(watch_dir))

        try:
            asyncio.run(
                ha_log_monitor.poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=ssh,
                    db_path=db_path,
                )
            )
        except asyncio.CancelledError:
            pass

        # Card file uses "notif_<id>" prefix so the dashboard can find it
        card_file = watch_dir / "notif_http-login.json"
        assert card_file.exists(), "Card file not written to watch_dir"
        card = json.loads(card_file.read_text())
        assert card["payload"].get("is_notification_card") is True
        assert (
            card["payload"].get("human_explanation")
            == "Someone tried to log into HA from IP 1.2.3.4."
        )

        # notification_history row written with the real HA notification_id
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT notification_id, hitl_sent_at FROM notification_history WHERE notification_id = 'http-login'"
            ).fetchone()
        assert row is not None
        assert row[1] is not None, "hitl_sent_at should be set after card is sent"

    def test_duplicate_notification_not_resent(self, monkeypatch, tmp_path):
        """Second iteration with same notification: card file NOT re-created (idempotency)."""
        import ha_agent_advanced
        import ha_log_monitor as hlm
        from ha_notification_manager import (
            mark_notification_hitl_sent,
            record_notification_seen,
        )
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FileNotifier
        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        db_path = str(tmp_path / "test.db")
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        ha_agent_advanced.init_local_database()

        # Pre-populate: notification already seen and HITL card already sent
        record_notification_seen("http-login", "security", "HIGH", db_path=db_path)
        mark_notification_hitl_sent("http-login", db_path=db_path)

        call_count = {"n": 0}

        async def fake_sleep(_t):
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise asyncio.CancelledError()

        monkeypatch.setattr(hlm.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(hlm, "HA_NOTIFICATION_POLL_INTERVAL_MINUTES", 0)

        ws = FakeHAWebSocketClient(notifications=[self._NOTIF])
        llm = FakeLLMClient(self._make_llm_json())
        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        notifier = FileNotifier(str(watch_dir))

        try:
            asyncio.run(
                hlm.poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=ssh,
                    db_path=db_path,
                )
            )
        except asyncio.CancelledError:
            pass

        # No new card file — notification was already handled
        assert not (watch_dir / "notif_http-login.json").exists()
