"""Tests for the debug mode overhaul (issue #574) and debug level system (issue #578).

Covers:
- utils/debug/episode_writer.py HTML structure
- AgentLoop capture_llm=True: captures in result, nudge pass-back
- GET/POST /api/debug-mode: enable/disable/verbose states + level field
- config.py DEBUG_LEVEL, DEBUG_MODE / DEBUG_VERBOSE defaults and YAML loading
- V32 migration: debug_log_path column exists
- V33 migration: llm_one_shot_calls table exists
- record_one_shot() helper: happy path, DB error swallowed, input truncated
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# TestEpisodeWriter
# ---------------------------------------------------------------------------


class TestEpisodeWriter:
    """HTML structure assertions for utils/debug/episode_writer.py."""

    def _make_captures(self):
        from utils.debug.capture import LLMCallRecord

        return [
            LLMCallRecord(
                seq=1,
                request_messages=[
                    {"role": "system", "content": "You are Pueo."},
                    {"role": "user", "content": "What is the disk usage?"},
                ],
                request_tools=["get_disk_usage", "finish_chat"],
                response_content="I'll check disk usage now.",
                response_tool_calls=[],
                thinking=None,
                nudges_injected=[],
                duration_ms=123.4,
                outcome_path=None,
            ),
            LLMCallRecord(
                seq=2,
                request_messages=[{"role": "user", "content": "retry"}],
                request_tools=["finish_chat"],
                response_content="",
                response_tool_calls=[
                    {
                        "function": {
                            "name": "finish_chat",
                            "arguments": {"summary": "OK"},
                        }
                    }
                ],
                thinking=None,
                nudges_injected=["Call finish_chat now."],
                duration_ms=88.0,
                outcome_path="finish_chat",
            ),
        ]

    def _make_session_meta(self):
        return {
            "session_id": 42,
            "outcome": "success",
            "model": "qwen2.5-coder:7b",
            "provider": "local",
            "timestamp": "2026-09-08 12:00:00",
        }

    def test_index_html_written(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir,
            self._make_session_meta(),
            self._make_captures(),
            [{"role": "user", "content": "What is the disk usage?"}],
        )
        assert (episode_dir / "index.html").exists()

    def test_index_contains_session_id(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), self._make_captures(), []
        )
        html = (episode_dir / "index.html").read_text()
        assert "Session 42" in html or "session 42" in html.lower() or "42" in html

    def test_index_contains_call_cards(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), self._make_captures(), []
        )
        html = (episode_dir / "index.html").read_text()
        assert "Call 1" in html
        assert "Call 2" in html

    def test_nudge_highlighted_in_html(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), self._make_captures(), []
        )
        html = (episode_dir / "index.html").read_text()
        assert "nudge" in html.lower() or "Nudge" in html

    def test_outcome_path_shown(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), self._make_captures(), []
        )
        html = (episode_dir / "index.html").read_text()
        assert "finish_chat" in html

    def test_large_request_written_as_separate_file(self, tmp_path):
        """When request page > 2 KB the writer saves llm_N_request.html."""
        from utils.debug.episode_writer import write_episode_html, _INLINE_THRESHOLD
        from utils.debug.capture import LLMCallRecord

        big_content = "x" * (_INLINE_THRESHOLD + 100)
        captures = [
            LLMCallRecord(
                seq=1,
                request_messages=[{"role": "user", "content": big_content}],
                request_tools=[],
                response_content="",
                response_tool_calls=[],
                thinking=None,
                nudges_injected=[],
                duration_ms=1.0,
            )
        ]
        episode_dir = tmp_path / "ep"
        write_episode_html(episode_dir, self._make_session_meta(), captures, [])
        assert (episode_dir / "llm_1_request.html").exists()

    def test_thinking_block_rendered(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html
        from utils.debug.capture import LLMCallRecord

        captures = [
            LLMCallRecord(
                seq=1,
                request_messages=[],
                request_tools=[],
                response_content="<think>My hidden thought</think>Answer.",
                response_tool_calls=[],
                thinking="My hidden thought",
                nudges_injected=[],
                duration_ms=50.0,
            )
        ]
        episode_dir = tmp_path / "ep"
        write_episode_html(episode_dir, self._make_session_meta(), captures, [])
        html = (episode_dir / "index.html").read_text()
        # The thinking keyword appears in some form
        assert "thinking" in html.lower() or "Thinking" in html

    def test_no_captures_renders_placeholder(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(episode_dir, self._make_session_meta(), [], [])
        html = (episode_dir / "index.html").read_text()
        assert "no captures" in html.lower()

    def test_system_messages_excluded_from_conversation(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        conv = [
            {"role": "system", "content": "TOP SECRET SYSTEM PROMPT"},
            {"role": "user", "content": "Hello"},
        ]
        write_episode_html(episode_dir, self._make_session_meta(), [], conv)
        html = (episode_dir / "index.html").read_text()
        assert "TOP SECRET SYSTEM PROMPT" not in html
        assert "Hello" in html

    def test_html_is_self_contained(self, tmp_path):
        """index.html must not reference external CDNs."""
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), self._make_captures(), []
        )
        html = (episode_dir / "index.html").read_text()
        assert "cdn.jsdelivr.net" not in html
        assert "cdnjs.cloudflare.com" not in html


# ---------------------------------------------------------------------------
# TestLLMCallRecord
# ---------------------------------------------------------------------------


class TestLLMCallRecord:
    def test_valid_construction(self):
        from utils.debug.capture import LLMCallRecord

        rec = LLMCallRecord(
            seq=1,
            request_messages=[],
            request_tools=["finish_chat"],
            response_content="hi",
            response_tool_calls=[],
            thinking=None,
            nudges_injected=[],
            duration_ms=99.9,
        )
        assert rec.seq == 1
        assert rec.outcome_path is None

    def test_outcome_path_default_is_none(self):
        from utils.debug.capture import LLMCallRecord

        rec = LLMCallRecord(
            seq=5,
            request_messages=[],
            request_tools=[],
            response_content="",
            response_tool_calls=[],
            thinking=None,
            nudges_injected=[],
            duration_ms=0.0,
        )
        assert rec.outcome_path is None

    def test_outcome_path_can_be_set(self):
        from utils.debug.capture import LLMCallRecord

        rec = LLMCallRecord(
            seq=1,
            request_messages=[],
            request_tools=[],
            response_content="",
            response_tool_calls=[],
            thinking=None,
            nudges_injected=[],
            duration_ms=0.0,
        )
        rec.outcome_path = "finish_chat"
        assert rec.outcome_path == "finish_chat"


# ---------------------------------------------------------------------------
# TestAgentLoopCaptureLLM
# ---------------------------------------------------------------------------


class TestAgentLoopCaptureLLM:
    """AgentLoop capture_llm=True stores LLMCallRecord objects in result."""

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        path = str(tmp_path / "test_agent.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _make_loop(self, llm, db_path, capture_llm=True):
        from utils.agent.agent_loop import AgentLoop
        from utils.agent.tool_registry import build_chat_tool_registry
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        return AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
            capture_llm=capture_llm,
        )

    def test_captures_present_on_success(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "All done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "success"
        assert len(result.llm_captures) == 1

    def test_capture_has_correct_seq(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.llm_captures[0].seq == 1

    def test_capture_outcome_path_set_on_terminal_tool(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.llm_captures[-1].outcome_path == "finish_chat"

    def test_capture_outcome_path_fallback_on_exhaustion(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        # Exhaust the loop with plain text so it never terminates normally
        llm = FakeToolCallingLLMClient(
            [{"content": "I don't want to call tools"} for _ in range(10)]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "exhausted"
        if result.llm_captures:
            assert result.llm_captures[-1].outcome_path == "exhaustion_fallback"

    def test_no_captures_when_capture_llm_false(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, db_path, capture_llm=False)
        result = asyncio.run(loop.run("Hello"))
        assert result.llm_captures == []

    def test_captures_reset_between_runs(self, db_path):
        """A second call to loop.run() does not accumulate captures from the first."""
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done again"},
                            }
                        }
                    ]
                },
            ]
        )
        loop = self._make_loop(llm, db_path)
        r1 = asyncio.run(loop.run("First"))
        r2 = asyncio.run(loop.run("Second"))
        assert len(r1.llm_captures) == 1
        assert len(r2.llm_captures) == 1

    def test_nudge_recorded_in_capture(self, db_path):
        """When the model returns plain text (no tools) then a tool, the nudge is
        recorded in the capture for the LLM call that followed the nudge."""
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                # First call returns plain text → nudge injected
                {"content": "Here is my answer in plain text."},
                # Second call returns the terminal tool
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                },
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "success"
        # The second capture should have the nudge recorded
        nudged_calls = [c for c in result.llm_captures if c.nudges_injected]
        assert len(nudged_calls) >= 1

    def test_nudge_content_includes_model_text(self, db_path):
        """When the first plain-text response has non-empty content and at least
        one tool call has already happened, the nudge includes the model's text."""
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        plain_text = "My detailed answer to your question."

        llm = FakeToolCallingLLMClient(
            [
                # First call: a real tool call (so tool_call_count >= 1)
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "recall",
                                "arguments": {"query": "server"},
                            }
                        }
                    ]
                },
                # Second call: plain text → triggers nudge with content pass-back
                {"content": plain_text},
                # Third call: terminal tool
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": plain_text},
                            }
                        }
                    ]
                },
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        # At least one capture should have a nudge containing the model's plain text
        nudge_texts = [n for c in result.llm_captures for n in c.nudges_injected]
        assert any(plain_text[:50] in n for n in nudge_texts)

    def test_multiple_llm_calls_produce_multiple_captures(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "recall",
                                "arguments": {"query": "server"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                },
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert len(result.llm_captures) == 2
        assert result.llm_captures[0].seq == 1
        assert result.llm_captures[1].seq == 2


# ---------------------------------------------------------------------------
# TestDebugModeAPI
# ---------------------------------------------------------------------------


class TestDebugModeAPI:
    """GET/POST /api/debug-mode endpoint states."""

    @pytest.fixture(autouse=True)
    def reset_debug_state(self, monkeypatch):
        """Reset _debug_level_enabled between tests."""
        import web.dashboard as dash

        monkeypatch.setattr(dash, "_debug_level_enabled", 0)

    def _client(self):
        from starlette.testclient import TestClient
        import web.dashboard as dash

        return TestClient(dash.app, raise_server_exceptions=False)

    def test_get_returns_off_by_default(self):
        resp = self._client().get("/api/debug-mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["verbose"] is False
        assert body["level"] == 0

    def test_post_enables_debug_level_1(self):
        client = self._client()
        resp = client.post("/api/debug-mode", json={"enabled": True, "verbose": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["level"] == 1

    def test_post_enables_verbose_sets_level_2(self):
        client = self._client()
        resp = client.post("/api/debug-mode", json={"enabled": True, "verbose": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["verbose"] is True
        assert body["level"] == 2

    def test_post_level_field_takes_precedence(self):
        client = self._client()
        resp = client.post(
            "/api/debug-mode", json={"enabled": False, "verbose": False, "level": 3}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == 3
        assert body["enabled"] is True
        assert body["verbose"] is True

    def test_post_disables_debug(self):
        import web.dashboard as dash

        dash._debug_level_enabled = 1
        client = self._client()
        resp = client.post("/api/debug-mode", json={"enabled": False, "verbose": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert resp.json()["level"] == 0

    def test_get_reflects_post_state(self):
        client = self._client()
        client.post("/api/debug-mode", json={"enabled": True, "verbose": False})
        resp = client.get("/api/debug-mode")
        assert resp.json()["enabled"] is True

    def test_verbose_legacy_maps_to_level_2(self):
        """verbose=True with enabled=False maps to level 2."""
        client = self._client()
        resp = client.post("/api/debug-mode", json={"enabled": False, "verbose": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == 2
        assert body["verbose"] is True

    def test_episode_file_served_when_debug_on(self, tmp_path, monkeypatch):
        """Static episode files are served when debug mode is on."""
        import web.dashboard as dash

        monkeypatch.setattr(dash, "_debug_level_enabled", 1)

        # Write a fake episode file
        def fake_get_dirs():
            from paths import PueoDirectories

            return PueoDirectories(
                config_dir=tmp_path / "config",
                data_dir=tmp_path / "data",
                state_dir=tmp_path / "state",
                cache_dir=tmp_path / "cache",
                log_dir=tmp_path / "logs",
                runtime_dir=tmp_path / "run",
                resources_dir=Path(__file__).parent.parent,
            )

        monkeypatch.setattr(dash, "_get_dirs", fake_get_dirs)

        episode_dir = tmp_path / "data" / "debug_episodes" / "session_7"
        episode_dir.mkdir(parents=True)
        (episode_dir / "index.html").write_text("<html>test</html>")

        client = self._client()
        resp = client.get("/debug-episodes/session_7/index.html")
        assert resp.status_code == 200
        assert b"test" in resp.content

    def test_episode_file_404_when_debug_off(self, tmp_path, monkeypatch):
        import web.dashboard as dash

        monkeypatch.setattr(dash, "_debug_level_enabled", 0)
        client = self._client()
        resp = client.get("/debug-episodes/session_7/index.html")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestDebugModeConfig
# ---------------------------------------------------------------------------


class TestDebugModeConfig:
    def test_debug_level_default_zero(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_LEVEL == 0

    def test_debug_mode_default_false(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_MODE is False

    def test_debug_verbose_default_false(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_VERBOSE is False

    def test_debug_level_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"debug_level": 2}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_LEVEL == 2
        assert config.DEBUG_MODE is True
        assert config.DEBUG_VERBOSE is True

    def test_debug_level_1_sets_mode_only(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"debug_level": 1}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_LEVEL == 1
        assert config.DEBUG_MODE is True
        assert config.DEBUG_VERBOSE is False

    def test_debug_mode_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"debug_mode": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_MODE is True
        assert config.DEBUG_LEVEL >= 1

    def test_debug_verbose_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"debug_verbose": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_VERBOSE is True
        assert config.DEBUG_LEVEL >= 2


# ---------------------------------------------------------------------------
# TestV32Migration
# ---------------------------------------------------------------------------


class TestV32Migration:
    """V32 adds debug_log_path to chat_sessions and repair_episodes."""

    def _get_cols(self, db_path: str, table: str) -> list[str]:
        with sqlite3.connect(db_path) as conn:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

    def test_debug_log_path_in_chat_sessions(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        db = str(tmp_path / "v32_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        ha_agent_advanced.init_local_database()
        cols = self._get_cols(db, "chat_sessions")
        assert "debug_log_path" in cols

    def test_debug_log_path_in_repair_episodes(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        db = str(tmp_path / "v32_test_repair.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        ha_agent_advanced.init_local_database()
        cols = self._get_cols(db, "repair_episodes")
        assert "debug_log_path" in cols

    def test_debug_log_path_in_sandbox_engine_db(self, monkeypatch, tmp_path):
        from agents import ha_agent_sandbox_engine

        db = str(tmp_path / "sandbox_v32.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", db)
        ha_agent_sandbox_engine.init_local_database()
        cols = self._get_cols(db, "chat_sessions")
        assert "debug_log_path" in cols


# ---------------------------------------------------------------------------
# TestV33Migration
# ---------------------------------------------------------------------------


class TestV33Migration:
    """V33 adds llm_one_shot_calls table."""

    def _has_table(self, db_path: str, table: str) -> bool:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            return row is not None

    def test_llm_one_shot_calls_in_advanced_db(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        db = str(tmp_path / "v33_adv.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        ha_agent_advanced.init_local_database()
        assert self._has_table(db, "llm_one_shot_calls")

    def test_llm_one_shot_calls_in_sandbox_db(self, monkeypatch, tmp_path):
        from agents import ha_agent_sandbox_engine

        db = str(tmp_path / "v33_sb.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", db)
        ha_agent_sandbox_engine.init_local_database()
        assert self._has_table(db, "llm_one_shot_calls")

    def test_llm_one_shot_calls_columns(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        db = str(tmp_path / "v33_cols.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(llm_one_shot_calls)")]
        for expected in (
            "caller",
            "model",
            "input_summary",
            "output_summary",
            "duration_ms",
            "outcome",
        ):
            assert expected in cols


# ---------------------------------------------------------------------------
# TestRecordOneShot
# ---------------------------------------------------------------------------


class TestRecordOneShot:
    """record_one_shot() helper in utils/debug/capture.py."""

    def test_happy_path_inserts_row(self, tmp_path):
        db = str(tmp_path / "os_happy.db")
        # Initialise schema so the table exists
        import sqlite3 as _sqlite3

        with _sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE llm_one_shot_calls "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, caller TEXT, model TEXT, "
                "input_summary TEXT, output_summary TEXT, duration_ms REAL, "
                "outcome TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )

        from utils.debug.capture import record_one_shot

        record_one_shot("test_caller", "qwen3:7b", "in", "out", 123.4, "success", db)
        with _sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT caller, model, outcome FROM llm_one_shot_calls"
            ).fetchone()
        assert row == ("test_caller", "qwen3:7b", "success")

    def test_db_error_swallowed(self, tmp_path):
        """record_one_shot must not raise even when the table is missing."""
        from utils.debug.capture import record_one_shot

        record_one_shot(
            "caller",
            "model",
            "in",
            "out",
            10.0,
            "error",
            str(tmp_path / "nonexistent.db"),
        )
        # No exception raised

    def test_input_truncated_to_500(self, tmp_path):
        import sqlite3 as _sqlite3

        db = str(tmp_path / "os_trunc.db")
        with _sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE llm_one_shot_calls "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, caller TEXT, model TEXT, "
                "input_summary TEXT, output_summary TEXT, duration_ms REAL, "
                "outcome TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )

        from utils.debug.capture import record_one_shot

        long_input = "x" * 1000
        record_one_shot("c", "m", long_input, "out", 1.0, "success", db)
        with _sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT input_summary FROM llm_one_shot_calls"
            ).fetchone()
        assert len(row[0]) <= 500
