"""Unit tests for the Logs tab — _parse_log_line_ts, _logs_source_to_command,
/logs/fetch, and /logs/prefs endpoints."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Pure-logic helpers (importable without the full app)
# ---------------------------------------------------------------------------


def _get_helpers():
    """Import the two pure helpers from dashboard.py."""
    from web.dashboard import _logs_source_to_command, _parse_log_line_ts  # type: ignore

    return _parse_log_line_ts, _logs_source_to_command


class TestParseLogLineTs:
    def test_iso_datetime_with_T(self):
        parse, _ = _get_helpers()
        result = parse("2026-09-03T14:22:01.123456+00:00 something happened")
        assert result > 0
        # Rough sanity: should be in the 2026 range
        assert 1756000000 < result < 1800000000

    def test_iso_datetime_space_separator(self):
        parse, _ = _get_helpers()
        result = parse("2026-09-03 14:22:01 INFO some log line")
        assert result > 0

    def test_line_without_timestamp_returns_zero(self):
        parse, _ = _get_helpers()
        assert parse("no timestamp here") == 0

    def test_empty_string_returns_zero(self):
        parse, _ = _get_helpers()
        assert parse("") == 0

    def test_malformed_timestamp_returns_zero(self):
        parse, _ = _get_helpers()
        assert parse("9999-99-99 99:99:99 bad") == 0


class TestLogsSourceToCommand:
    def test_apps_source(self):
        _, cmd_fn = _get_helpers()
        cmd = cmd_fn("apps:mosquitto", 500)
        assert "ha apps logs mosquitto" in cmd
        assert "500" in cmd

    def test_netalertx_source(self):
        _, cmd_fn = _get_helpers()
        cmd = cmd_fn("netalertx", 1000)
        assert "netalertx" in cmd.lower() or "app.log" in cmd
        assert "1000" in cmd

    def test_unknown_source_returns_fallback(self):
        _, cmd_fn = _get_helpers()
        cmd = cmd_fn("syslog", 100)
        assert "100" in cmd

    def test_apps_slug_injected_correctly(self):
        _, cmd_fn = _get_helpers()
        cmd = cmd_fn("apps:zigbee2mqtt", 200)
        assert "zigbee2mqtt" in cmd


# ---------------------------------------------------------------------------
# FastAPI route tests
# ---------------------------------------------------------------------------


def _make_client(db_path: str):
    """Return (app, db_path) for use with httpx AsyncClient in-process."""
    import web.dashboard as dash  # type: ignore

    return dash.app, db_path


class TestLogsPrefEndpoints:
    def test_get_prefs_empty(self, pueo_dirs, tmp_path):
        import agents.ha_agent_advanced as adv
        import web.dashboard as dash  # type: ignore
        from httpx import ASGITransport, AsyncClient

        db_path = tmp_path / "prefs.db"
        with sqlite3.connect(str(db_path)) as conn:
            adv._migrate_v28(conn.cursor())

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=dash.app), base_url="http://test"
            ) as ac:
                with patch.object(dash, "DB_PATH", str(db_path)):
                    return await ac.get("/logs/prefs")

        with patch.object(dash, "DB_PATH", str(db_path)):
            r = asyncio.run(_run())
        assert r.status_code == 200
        assert r.json()["sources"] == []

    def test_set_and_get_prefs(self, pueo_dirs, tmp_path):
        import agents.ha_agent_advanced as adv
        import web.dashboard as dash  # type: ignore
        from httpx import ASGITransport, AsyncClient

        db_path = tmp_path / "prefs2.db"
        with sqlite3.connect(str(db_path)) as conn:
            adv._migrate_v28(conn.cursor())

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=dash.app), base_url="http://test"
            ) as ac:
                with patch.object(dash, "DB_PATH", str(db_path)):
                    post_r = await ac.post(
                        "/logs/prefs",
                        json={"sources": ["apps:mosquitto", "netalertx"]},
                    )
                    get_r = await ac.get("/logs/prefs")
            return post_r, get_r

        with patch.object(dash, "DB_PATH", str(db_path)):
            post_r, get_r = asyncio.run(_run())
        assert post_r.status_code == 200
        assert get_r.json()["sources"] == ["apps:mosquitto", "netalertx"]

    def test_set_prefs_overwrites_previous(self, pueo_dirs, tmp_path):
        import agents.ha_agent_advanced as adv
        import web.dashboard as dash  # type: ignore
        from httpx import ASGITransport, AsyncClient

        db_path = tmp_path / "prefs3.db"
        with sqlite3.connect(str(db_path)) as conn:
            adv._migrate_v28(conn.cursor())

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=dash.app), base_url="http://test"
            ) as ac:
                with patch.object(dash, "DB_PATH", str(db_path)):
                    await ac.post("/logs/prefs", json={"sources": ["apps:foo"]})
                    await ac.post("/logs/prefs", json={"sources": ["apps:bar"]})
                    return await ac.get("/logs/prefs")

        with patch.object(dash, "DB_PATH", str(db_path)):
            r = asyncio.run(_run())
        assert r.json()["sources"] == ["apps:bar"]


class TestLogsFetchEndpoint:
    def test_logs_fetch_returns_structured_response(self, pueo_dirs):
        import web.dashboard as dash  # type: ignore
        from httpx import ASGITransport, AsyncClient

        fake_stdout = (
            "2026-09-03 14:00:01 INFO client connected\n"
            "2026-09-03 14:00:02 ERROR client dropped\n"
        )

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=dash.app), base_url="http://test"
            ) as ac:
                with patch("utils.ha.ssh_client.AsyncSSHClient") as mock_ssh_cls:
                    mock_ssh = AsyncMock()
                    mock_ssh.run.return_value = (0, fake_stdout, "")
                    mock_ssh_cls.return_value = mock_ssh
                    return await ac.get("/logs/fetch?source=apps:mosquitto&limit=100")

        r = asyncio.run(_run())
        assert r.status_code == 200
        data = r.json()
        assert "lines" in data
        assert "fetched_at" in data
        assert "fetch_duration_ms" in data
        assert data["source"] == "apps:mosquitto"
        assert len(data["lines"]) == 2

    def test_logs_fetch_is_match_flagged(self, pueo_dirs):
        import web.dashboard as dash  # type: ignore
        from httpx import ASGITransport, AsyncClient

        critical_line = (
            "2026-09-03 14:00:01 CRITICAL homeassistant.core: Error doing job"
        )
        normal_line = "2026-09-03 14:00:02 INFO everything is fine"

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=dash.app), base_url="http://test"
            ) as ac:
                with patch("utils.ha.ssh_client.AsyncSSHClient") as mock_ssh_cls:
                    mock_ssh = AsyncMock()
                    mock_ssh.run.return_value = (
                        0,
                        critical_line + "\n" + normal_line,
                        "",
                    )
                    mock_ssh_cls.return_value = mock_ssh
                    return await ac.get("/logs/fetch?source=apps:mosquitto")

        r = asyncio.run(_run())
        assert r.status_code == 200
        lines = r.json()["lines"]
        assert lines[0]["is_match"] is True
        assert lines[1]["is_match"] is False

    def test_logs_fetch_missing_source_returns_4xx(self, pueo_dirs):
        import web.dashboard as dash  # type: ignore
        from httpx import ASGITransport, AsyncClient

        async def _run():
            async with AsyncClient(
                transport=ASGITransport(app=dash.app), base_url="http://test"
            ) as ac:
                return await ac.get("/logs/fetch")  # no source param

        r = asyncio.run(_run())
        assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# V28 migration
# ---------------------------------------------------------------------------


class TestV28Migration:
    def test_user_prefs_table_created(self, tmp_path):
        import agents.ha_agent_advanced as adv

        db_path = tmp_path / "v28.db"
        with sqlite3.connect(str(db_path)) as conn:
            adv._migrate_v28(conn.cursor())
        with sqlite3.connect(str(db_path)) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "user_prefs" in tables

    def test_user_prefs_insert_and_select(self, tmp_path):
        import agents.ha_agent_advanced as adv

        db_path = tmp_path / "v28b.db"
        with sqlite3.connect(str(db_path)) as conn:
            adv._migrate_v28(conn.cursor())
            conn.execute("INSERT INTO user_prefs (key, value) VALUES ('k', 'v')")
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT value FROM user_prefs WHERE key='k'").fetchone()
        assert row[0] == "v"

    def test_migration_idempotent(self, tmp_path):
        import agents.ha_agent_advanced as adv

        db_path = tmp_path / "v28c.db"
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            adv._migrate_v28(cur)
            adv._migrate_v28(cur)  # second call must not raise

    def test_v28_in_sandbox_engine_migrations(self):
        import agents.ha_agent_sandbox_engine as eng

        versions = [ver for ver, _ in eng._MIGRATIONS]
        assert 28 in versions
