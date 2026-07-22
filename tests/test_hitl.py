#!/usr/bin/env python3
"""HITL, autonomy gate, and notification tests — notifiers, requires_hitl logic, pipeline gate, AutonomyGate levels."""

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


_ORIGINAL_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
_FIXED_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"
_BAD_FIX = "http:\n  server_port: 8124\n"  # missing homeassistant: block
# ── utils/notify.py ──────────────────────────────────────────────────────────────


class TestFakeNotifier:
    def test_send_records_call(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier()
        asyncio.run(n.send("subject", "body", {"notification_id": "x"}))
        assert len(n.sent) == 1
        assert n.sent[0]["subject"] == "subject"

    def test_send_records_payload(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier()
        asyncio.run(
            n.send("s", "b", {"notification_id": "abc", "severity": "CRITICAL"})
        )
        assert n.sent[0]["payload"]["severity"] == "CRITICAL"

    def test_wait_for_approval_returns_true_when_configured(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier(approve=True)
        result = asyncio.run(n.wait_for_approval("any-id"))
        assert result is True

    def test_wait_for_approval_returns_false_when_rejected(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier(approve=False)
        result = asyncio.run(n.wait_for_approval("any-id"))
        assert result is False

    def test_multiple_sends_accumulate(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier()
        asyncio.run(n.send("s1", "b1", {}))
        asyncio.run(n.send("s2", "b2", {}))
        assert len(n.sent) == 2


class TestFileNotifier:
    def test_send_writes_json_file(self, tmp_path):
        from utils.notify import FileNotifier
        import json

        n = FileNotifier(watch_dir=str(tmp_path))
        asyncio.run(n.send("Test subject", "Test body", {"notification_id": "nid-1"}))
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["subject"] == "Test subject"
        assert data["notification_id"] == "nid-1"

    def test_send_creates_watch_dir_if_missing(self, tmp_path):
        from utils.notify import FileNotifier

        new_dir = tmp_path / "hitl"
        n = FileNotifier(watch_dir=str(new_dir))
        asyncio.run(n.send("s", "b", {"notification_id": "x"}))
        assert new_dir.exists()

    def test_wait_for_approval_resolves_approved(self, tmp_path):
        from utils.notify import FileNotifier

        n = FileNotifier(watch_dir=str(tmp_path), poll_interval=0.01)
        (tmp_path / "nid-ok.approved").touch()
        result = asyncio.run(n.wait_for_approval("nid-ok"))
        assert result is True

    def test_wait_for_approval_resolves_rejected(self, tmp_path):
        from utils.notify import FileNotifier

        n = FileNotifier(watch_dir=str(tmp_path), poll_interval=0.01)
        (tmp_path / "nid-no.rejected").touch()
        result = asyncio.run(n.wait_for_approval("nid-no"))
        assert result is False


class TestGetNotifier:
    def test_default_returns_file_notifier(self, tmp_path):
        from utils.notify import FileNotifier, get_notifier

        n = get_notifier("file", notify_watch_dir=str(tmp_path))
        assert isinstance(n, FileNotifier)

    def test_ntfy_returns_ntfy_notifier(self, tmp_path):
        from utils.notify import NtfyNotifier, get_notifier

        n = get_notifier(
            "ntfy", notify_url="https://ntfy.sh/topic", notify_watch_dir=str(tmp_path)
        )
        assert isinstance(n, NtfyNotifier)

    def test_ntfy_wait_for_approval_delegates_to_file(self, tmp_path):
        from utils.notify import NtfyNotifier

        n = NtfyNotifier(url="https://ntfy.sh/topic", watch_dir=str(tmp_path))
        nid = "test-ntfy-approval"
        (tmp_path / f"{nid}.approved").write_text("")
        assert asyncio.run(n.wait_for_approval(nid)) is True

    def test_ntfy_wait_for_rejection_delegates_to_file(self, tmp_path):
        from utils.notify import NtfyNotifier

        n = NtfyNotifier(url="https://ntfy.sh/topic", watch_dir=str(tmp_path))
        nid = "test-ntfy-rejection"
        (tmp_path / f"{nid}.rejected").write_text("")
        assert asyncio.run(n.wait_for_approval(nid)) is False

    def test_webhook_returns_webhook_notifier(self):
        from utils.notify import WebhookNotifier, get_notifier

        n = get_notifier("webhook", notify_url="http://example.com/hook")
        assert isinstance(n, WebhookNotifier)

    def test_unknown_type_defaults_to_file(self, tmp_path):
        from utils.notify import FileNotifier, get_notifier

        n = get_notifier("unknown_type", notify_watch_dir=str(tmp_path))
        assert isinstance(n, FileNotifier)


# ── requires_hitl ────────────────────────────────────────────────────────────────


class TestRequiresHitl:
    def _make_report(self, severity, issues):
        from ha_agent_sandbox_engine import DiagnosticsReport

        return DiagnosticsReport(
            is_valid=False,
            severity=severity,
            identified_issues=issues,
            recommended_fix_yaml=None,
        )

    def test_critical_severity_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("CRITICAL", ["YAML error"])
        assert requires_hitl(report) is True

    def test_low_severity_no_keywords_does_not_require_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["wrong port number"])
        assert requires_hitl(report) is False

    def test_medium_severity_no_keywords_does_not_require_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("MEDIUM", ["deprecated syntax"])
        assert requires_hitl(report) is False

    def test_hacs_keyword_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["HACS integration needs update"])
        assert requires_hitl(report) is True

    def test_hacs_keyword_case_insensitive(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("MEDIUM", ["hacs component failed"])
        assert requires_hitl(report) is True

    def test_database_keyword_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["database schema migration needed"])
        assert requires_hitl(report) is True

    def test_multiple_issues_one_matching_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["wrong port", "database corruption"])
        assert requires_hitl(report) is True

    def test_none_severity_clean_config_does_not_require_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("NONE", [])
        assert requires_hitl(report) is False

    def test_hitl_always_true_triggers_for_low_severity(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["minor formatting issue"])
        assert requires_hitl(report, hitl_always=True) is True

    def test_hitl_always_true_triggers_for_none_severity(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("NONE", [])
        assert requires_hitl(report, hitl_always=True) is True

    def test_hitl_always_false_preserves_normal_logic(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["minor formatting issue"])
        assert requires_hitl(report, hitl_always=False) is False


# ── HITL config keys ─────────────────────────────────────────────────────────────


class TestHitlConfigKeys:
    def test_notifier_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFIER == "file"

    def test_notify_url_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_URL == ""

    def test_notify_watch_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_WATCH_DIR == "hitl/"

    def test_notifier_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"notifier": "ntfy"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFIER == "ntfy"

    def test_notify_url_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"notify_url": "http://ntfy.sh/pueo"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_URL == "http://ntfy.sh/pueo"

    def test_notify_watch_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"notify_watch_dir": "/var/pueo/hitl/"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_WATCH_DIR == "/var/pueo/hitl/"

    def test_hitl_always_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HITL_ALWAYS is False

    def test_hitl_always_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"hitl_always": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.HITL_ALWAYS is True


# ── HITL pipeline gate ────────────────────────────────────────────────────────────


class TestHitlPipelineGate:
    @pytest.fixture
    def ssh_ok(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: hitl-slug-1\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_sandbox_engine

        path = str(tmp_path / "hitl_test.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", path)
        return path

    @pytest.fixture
    def llm_critical(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="CRITICAL",
            identified_issues=["critical YAML error"],
            recommended_fix_yaml=_FIXED_CONFIG,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def llm_low_fix(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="LOW",
            identified_issues=["wrong port"],
            recommended_fix_yaml=_FIXED_CONFIG,
        )
        return FakeLLMClient(r.model_dump_json())

    def test_critical_issue_sends_notification(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert len(notifier.sent) == 1

    def test_critical_issue_notification_contains_severity(
        self, ssh_ok, llm_critical, db_path
    ):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert "CRITICAL" in notifier.sent[0]["subject"]

    def test_approval_proceeds_to_backup(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_rejection_aborts_backup(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_rejection_records_state(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate
        import sqlite3 as sqlite3_mod

        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        with sqlite3_mod.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        assert "rejected" in action[0].lower()

    def test_low_severity_no_notification_sent(self, ssh_ok, llm_low_fix, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_low_fix, notifier=notifier, gate=gate
            )
        )
        assert len(notifier.sent) == 0

    def test_low_severity_proceeds_directly_to_backup(
        self, ssh_ok, llm_low_fix, db_path
    ):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_low_fix, notifier=notifier, gate=gate
            )
        )
        assert any("ha backup new" in cmd for cmd in ssh_ok.commands_run)


# ── AutonomyGate config keys ──────────────────────────────────────────────────────


class TestAutonomyConfigKeys:
    def test_autonomy_level_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 2

    def test_autonomy_level_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"autonomy_level": 4}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 4

    def test_netalertx_mode_diagnose_maps_to_level1(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"mode": "diagnose"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 1

    def test_netalertx_mode_auto_fix_maps_to_level3(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"mode": "auto_fix"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 3

    def test_netalertx_mode_autonomous_maps_to_level4(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"mode": "autonomous"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 4

    def test_agent_autonomy_level_takes_precedence_over_netalertx_mode(
        self, isolated_config
    ):
        isolated_config.write_text(
            yaml.dump(
                {"agent": {"autonomy_level": 3}, "netalertx": {"mode": "diagnose"}}
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 3


# ── Dashboard config ──────────────────────────────────────────────────────────────


class TestDashboardConfig:
    def test_dashboard_port_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DASHBOARD_PORT == 8080

    def test_dashboard_port_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"dashboard_port": 9090}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DASHBOARD_PORT == 9090


# ── AutonomyGate ──────────────────────────────────────────────────────────────────


class TestAutonomyGate:
    # ── should_auto_execute: 4 levels × 4 risks ──────────────────────────────────

    def test_level1_never_auto_executes(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=1)
        for risk in RiskLevel:
            assert gate.should_auto_execute(risk) is False

    def test_level2_never_auto_executes(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=2)
        for risk in RiskLevel:
            assert gate.should_auto_execute(risk) is False

    def test_level3_auto_executes_low_only(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=3)
        assert gate.should_auto_execute(RiskLevel.LOW) is True
        assert gate.should_auto_execute(RiskLevel.MEDIUM) is False
        assert gate.should_auto_execute(RiskLevel.HIGH) is False
        assert gate.should_auto_execute(RiskLevel.CRITICAL) is False

    def test_level4_auto_executes_except_critical(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=4)
        assert gate.should_auto_execute(RiskLevel.LOW) is True
        assert gate.should_auto_execute(RiskLevel.MEDIUM) is True
        assert gate.should_auto_execute(RiskLevel.HIGH) is True
        assert gate.should_auto_execute(RiskLevel.CRITICAL) is False

    # ── should_ask_preference ────────────────────────────────────────────────────

    def test_levels_1_2_3_ask_preference(self):
        from utils.autonomy import AutonomyGate

        for level in [1, 2, 3]:
            assert AutonomyGate(level=level).should_ask_preference("context") is True

    def test_level4_does_not_ask_preference(self):
        from utils.autonomy import AutonomyGate

        assert AutonomyGate(level=4).should_ask_preference("context") is False

    # ── require_approval short-circuits ─────────────────────────────────────────

    def test_level1_rejects_without_notifying_for_all_risks(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=1)
        for risk in RiskLevel:
            notifier = FakeNotifier(approve=True)
            result = asyncio.run(
                gate.require_approval(
                    "s", "b", {"notification_id": "x"}, notifier, risk
                )
            )
            assert result is False
            assert len(notifier.sent) == 0

    def test_level4_approves_low_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.LOW
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level4_approves_medium_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.MEDIUM
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level4_approves_high_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level4_notifies_for_critical_and_approves(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "test-id"}, notifier, RiskLevel.CRITICAL
            )
        )
        assert result is True
        assert len(notifier.sent) == 1

    def test_level4_notifies_for_critical_and_rejects(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "test-id"}, notifier, RiskLevel.CRITICAL
            )
        )
        assert result is False
        assert len(notifier.sent) == 1

    def test_level3_approves_low_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.LOW
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level3_notifies_for_medium(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.MEDIUM
            )
        )
        assert len(notifier.sent) == 1

    def test_level3_notifies_for_high(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert len(notifier.sent) == 1

    def test_level3_notifies_for_critical(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.CRITICAL
            )
        )
        assert len(notifier.sent) == 1

    def test_level2_notifies_for_all_risks(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=2)
        for risk in RiskLevel:
            notifier = FakeNotifier(approve=True)
            asyncio.run(
                gate.require_approval(
                    "s", "b", {"notification_id": "x"}, notifier, risk
                )
            )
            assert len(notifier.sent) == 1, f"level 2 should notify for {risk.name}"

    def test_require_approval_waits_until_approved(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=2)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert result is True
        assert len(notifier.sent) == 1

    def test_require_approval_waits_until_rejected(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=2)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert result is False
        assert len(notifier.sent) == 1

    def test_require_approval_logs_hitl_wait_and_result(self, caplog):
        import logging
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=2)
        notifier = FakeNotifier(approve=True)
        with caplog.at_level(logging.INFO):
            result = asyncio.run(
                gate.require_approval(
                    subject="test action",
                    body="test body",
                    payload={"notification_id": "test_nid"},
                    notifier=notifier,
                    risk=RiskLevel.HIGH,
                )
            )
        assert result is True
        messages = [r.message for r in caplog.records]
        assert any("hitl_waiting_for_approval" in m for m in messages)
        assert any("hitl_approval_received" in m for m in messages)

    # ── done criteria — level 1: no SSH writes ───────────────────────────────────

    def test_level1_sandbox_pipeline_produces_no_ssh_writes(self):
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport
        import ha_agent_sandbox_engine

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: s1\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )
        llm = FakeLLMClient(
            DiagnosticsReport(
                is_valid=False,
                severity="WARNING",
                identified_issues=["deprecated key"],
                recommended_fix_yaml=_FIXED_CONFIG,
            ).model_dump_json()
        )
        gate = AutonomyGate(level=1)
        notifier = FakeNotifier(approve=True)
        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )
        assert ssh.written_files == {}

    # ── done criteria — level 4: full pipeline for WARNING, pauses for CRITICAL ──

    def test_level4_warning_severity_runs_without_hitl(self):
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport
        import ha_agent_sandbox_engine

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: s1\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )
        llm = FakeLLMClient(
            DiagnosticsReport(
                is_valid=False,
                severity="WARNING",
                identified_issues=["deprecated key"],
                recommended_fix_yaml=_FIXED_CONFIG,
            ).model_dump_json()
        )
        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )
        assert len(notifier.sent) == 0
        assert any("ha backup new" in cmd for cmd in ssh.commands_run)

    def test_level4_critical_severity_pauses_for_approval(self):
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport
        import ha_agent_sandbox_engine

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: s1\n", ""),
                "ha core check": (0, "", ""),
                "ha core restart": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )
        llm = FakeLLMClient(
            DiagnosticsReport(
                is_valid=False,
                severity="CRITICAL",
                identified_issues=["critical error"],
                recommended_fix_yaml=_FIXED_CONFIG,
            ).model_dump_json()
        )
        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )
        assert len(notifier.sent) == 1


# ── netalertx.* config keys ─────────────────────────────────────────────────────
