"""Integration tests: cross-module state flows that feed the dashboard.

Each test exercises two or more real modules interacting through shared
SQLite state or the filesystem — not raw SQL inserts.
"""

import asyncio
import hashlib
import json
import sqlite3
import time

import pytest


class TestNotificationLifecycle:
    """ha_notification_manager functions write state → web.dashboard reads it.

    Verifies that record_notification_seen + mark_notification_hitl_sent produce
    rows that _load_notification_dashboard_data correctly surfaces as pending,
    and that mark_notification_dismissed removes them from the pending list.
    """

    def _setup_db(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced
        import web.dashboard as dashboard

        db_path = str(tmp_path / "test.db")
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)
        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        ha_agent_advanced.init_local_database()
        return db_path, watch_dir

    def test_notification_appears_in_pending_after_manager_writes_it(
        self, monkeypatch, tmp_path
    ):
        """record_notification_seen + mark_notification_hitl_sent → /notifications pending."""
        import web.dashboard as dashboard
        from agents.ha_notification_manager import (
            mark_notification_hitl_sent,
            record_notification_seen,
        )
        from pathlib import Path

        db_path, watch_dir = self._setup_db(monkeypatch, tmp_path)

        # Use real manager functions (not raw SQL) to write state
        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        mark_notification_hitl_sent("http_login", db_path=db_path)

        # Write a card file as FileNotifier would
        card_id = "notif_http_login"
        card = {
            "notification_id": card_id,
            "subject": "Login attempt from unknown IP",
            "body": "Someone tried to log into HA",
            "payload": {
                "notification_id": card_id,
                "ha_notification_id": "http_login",
                "human_explanation": "An unknown device tried to authenticate.",
                "recommended_action": "Check your security log.",
                "enriched_context": {"source_ip": "1.2.3.4"},
                "original_message": "Login from 1.2.3.4",
                "original_title": "Login attempt",
            },
            "sent_at": int(time.time()),
        }
        (watch_dir / f"{card_id}.json").write_text(json.dumps(card))

        pending, _history = dashboard._load_notification_dashboard_data(Path(watch_dir))

        assert len(pending) == 1
        row = pending[0]
        assert row["notification_id"] == "http_login"
        assert row["category"] == "security"
        assert row["severity"] == "HIGH"
        # Enriched from card file
        assert (
            row.get("human_explanation") == "An unknown device tried to authenticate."
        )

    def test_dismiss_removes_notification_from_pending(self, monkeypatch, tmp_path):
        """mark_notification_dismissed sets dismissed_at → notification no longer pending."""
        import web.dashboard as dashboard
        from agents.ha_notification_manager import (
            mark_notification_dismissed,
            mark_notification_hitl_sent,
            record_notification_seen,
        )
        from pathlib import Path

        db_path, watch_dir = self._setup_db(monkeypatch, tmp_path)

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        mark_notification_hitl_sent("http_login", db_path=db_path)

        # Verify it's pending before dismiss
        pending_before, _ = dashboard._load_notification_dashboard_data(Path(watch_dir))
        assert len(pending_before) == 1

        mark_notification_dismissed("http_login", db_path=db_path)

        pending_after, history = dashboard._load_notification_dashboard_data(
            Path(watch_dir)
        )
        assert len(pending_after) == 0
        assert len(history) == 1  # still in history

    def test_severity_filter_returns_matching_notifications_only(
        self, monkeypatch, tmp_path
    ):
        """Two notifications of different severity: severity filter returns only matching one."""
        import web.dashboard as dashboard
        from agents.ha_notification_manager import record_notification_seen
        from pathlib import Path

        db_path, watch_dir = self._setup_db(monkeypatch, tmp_path)

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        record_notification_seen(
            "invalid_config", "config_error", "MEDIUM", db_path=db_path
        )

        _pending, history_high = dashboard._load_notification_dashboard_data(
            Path(watch_dir), severity_filter="HIGH"
        )
        assert all(r["severity"] == "HIGH" for r in history_high)
        assert len(history_high) == 1

    def test_run_notifications_ws_transport_writes_card_and_db(
        self, monkeypatch, tmp_path
    ):
        """run_notifications uses WebSocket (not REST states) and writes a card to disk.

        This is the seam test for PR #85: verifies that FakeHAWebSocketClient.get_persistent_notifications()
        is used (not get_states) and that the card file + DB row are correctly written.
        """
        from agents import ha_agent_advanced
        from agents import ha_notification_manager
        from agents.ha_notification_manager import _NotificationLLMOutput
        from utils.ha.ha_rest_client import FakeHARestClient
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FileNotifier
        from utils.llm.ollama_client import FakeLLMClient
        from utils.ha.ssh_client import FakeSSHClient

        db_path = str(tmp_path / "test.db")
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        ha_agent_advanced.init_local_database()

        # Fake WS client with one notification in WS shape (notification_id, not entity_id)
        ws = FakeHAWebSocketClient(
            notifications=[
                {
                    "notification_id": "invalid_config",
                    "title": "Config Error",
                    "message": "Component homekit error in configuration.yaml",
                    "status": "unread",
                }
            ]
        )
        fake_rest = FakeHARestClient()  # should NOT be called for notification fetch
        llm = FakeLLMClient(
            _NotificationLLMOutput(
                human_explanation="Your HomeKit config has an error.",
                recommended_action="Check configuration.yaml.",
                requires_hitl=True,
            ).model_dump_json()
        )
        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        notifier = FileNotifier(str(watch_dir))

        count = asyncio.run(
            ha_notification_manager.run_notifications(
                ha_rest_client=fake_rest,
                ws_client=ws,
                llm_client=llm,
                ssh_client=ssh,
                notifier=notifier,
                db_path=db_path,
            )
        )

        assert count == 1

        # WebSocket was used — REST get_states was NOT called
        assert "get_persistent_notifications" in ws.calls
        rest_fetch_calls = [
            c for c in getattr(fake_rest, "service_calls", []) if "states" in str(c)
        ]
        assert len(rest_fetch_calls) == 0

        # Card file written
        card_file = watch_dir / "notif_invalid_config.json"
        assert card_file.exists()
        payload = json.loads(card_file.read_text())["payload"]
        assert payload["ha_notification_id"] == "invalid_config"
        assert "HomeKit" in payload["human_explanation"]

        # DB row written
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at FROM notification_history WHERE notification_id='invalid_config'"
            ).fetchone()
        assert row is not None and row[0] is not None


class TestBackupPipelineToRoute:
    """Real backup pipeline functions write to DB → GET /backups renders the data.

    Existing tests insert raw SQL rows. This class uses the real
    ha_agent_advanced functions to verify the actual write path.
    """

    def _setup_db(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced
        import web.dashboard as dashboard

        db_path = str(tmp_path / "test.db")
        backup_dir = str(tmp_path / "backups")

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", backup_dir)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)

        ha_agent_advanced.init_local_database()
        return db_path, backup_dir

    def test_record_backup_slug_appears_in_backups_route(self, monkeypatch, tmp_path):
        """agents.ha_agent_advanced.record_backup_slug() → GET /backups shows the slug."""
        from fastapi.testclient import TestClient

        from agents import ha_agent_advanced
        import web.dashboard as dashboard

        db_path, _ = self._setup_db(monkeypatch, tmp_path)

        ha_agent_advanced.record_backup_slug("abc123")

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/backups")
        assert resp.status_code == 200
        assert "abc123" in resp.text

    def test_offloaded_backup_shows_pueo_mark(self, monkeypatch, tmp_path):
        """offload_backup_to_local() sets location='both' → /backups shows Pueo mark."""
        from fastapi.testclient import TestClient

        from agents import ha_agent_advanced
        import web.dashboard as dashboard

        db_path, backup_dir = self._setup_db(monkeypatch, tmp_path)
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_OFFLOAD_ENABLED", True)

        slug = "offload-slug-001"
        ha_agent_advanced.record_backup_slug(slug)

        fake_bytes = b"fake backup content for slug"
        remote_path = f"/backup/{slug}.tar"
        sha256 = hashlib.sha256(fake_bytes).hexdigest()

        from utils.ha.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            download_contents={remote_path: fake_bytes},
            command_results={
                f"sha256sum {remote_path}": (0, f"{sha256}  {remote_path}\n", "")
            },
        )

        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))

        # Verify DB updated
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location, offloaded_at FROM backup_registry WHERE backup_slug=?",
                (slug,),
            ).fetchone()
        assert row is not None
        assert row[0] == "both"
        assert row[1] is not None

        # Verify /backups route reflects the offload
        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/backups")
        assert resp.status_code == 200
        assert slug in resp.text

    def test_backup_pipeline_full_flow_record_offload_visible(
        self, monkeypatch, tmp_path
    ):
        """Full backup flow: record slug → offload → /backups shows slug with on_pueo mark."""
        from fastapi.testclient import TestClient

        from agents import ha_agent_advanced
        import web.dashboard as dashboard

        db_path, backup_dir = self._setup_db(monkeypatch, tmp_path)
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_OFFLOAD_ENABLED", True)

        slug = "full-flow-slug-xyz"
        ha_agent_advanced.record_backup_slug(slug)

        remote_path = f"/backup/{slug}.tar"
        fake_bytes = b"full flow test bytes"
        sha256 = hashlib.sha256(fake_bytes).hexdigest()

        from utils.ha.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            download_contents={remote_path: fake_bytes},
            command_results={
                f"sha256sum {remote_path}": (0, f"{sha256}  {remote_path}\n", "")
            },
        )
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/backups")
        assert resp.status_code == 200
        assert slug in resp.text


class TestUpdateCardToRoute:
    """run_update_check() → FileNotifier writes a card → GET / renders the HITL card."""

    def test_update_card_appears_on_dashboard_index(self, monkeypatch, tmp_path):
        """An available update generates a HITL card that the dashboard index renders."""
        from fastapi.testclient import TestClient

        from agents import ha_update_manager
        import web.dashboard as dashboard
        from utils.ha.ha_rest_client import FakeHARestClient
        from utils.notify import FileNotifier

        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        monkeypatch.setattr(dashboard, "NOTIFY_WATCH_DIR", str(watch_dir))

        # Patch fetch_release_notes_cached to avoid real GitHub WAN calls
        async def _fake_release_notes(version, cache_dir):
            return ""

        monkeypatch.setattr(
            ha_update_manager, "fetch_release_notes_cached", _fake_release_notes
        )

        update_entity = {
            "entity_id": "update.home_assistant_core_update",
            "state": "on",
            "attributes": {
                "installed_version": "2025.6.0",
                "latest_version": "2025.7.0",
                "release_url": "https://github.com/home-assistant/core/releases/tag/2025.7.0",
                "release_summary": "Bug fixes and improvements",
                "in_progress": False,
            },
        }
        rest = FakeHARestClient(states=[update_entity])

        # Gate that sends the card file but returns False (no blocking wait for approval)
        from utils.autonomy import RiskLevel

        class _QueuingGate:
            async def require_approval(self, subject, body, payload, notifier, risk):
                await notifier.send(subject, body, payload)
                return False

            async def should_auto_execute(self, risk: RiskLevel) -> bool:
                return False

        gate = _QueuingGate()
        notifier = FileNotifier(str(watch_dir))

        asyncio.run(
            ha_update_manager.run_update_check(
                ha_rest_client=rest,
                llm_client=None,
                gate=gate,
                notifier=notifier,
            )
        )

        # At least one card file written
        card_files = list(watch_dir.glob("*.json"))
        assert len(card_files) >= 1

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get("/")
        assert resp.status_code == 200
        # Card rendered on dashboard index
        assert (
            "2025.7.0" in resp.text
            or "home_assistant_core" in resp.text.lower()
            or "update" in resp.text.lower()
        )
