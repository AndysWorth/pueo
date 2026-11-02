"""Tests for the debug mode overhaul (issue #574) and debug level system (issue #578).

Covers:
- utils/debug/episode_writer.py bundle (index.html via episode_report)
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
        assert "LLM call 1" in html
        assert "LLM call 2" in html

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

    def test_no_side_files_and_long_content_inline(self, tmp_path):
        """Schema v2: everything is inlined; only index.html + JSON are written."""
        from utils.debug.episode_writer import write_episode_html
        from utils.debug.capture import LLMCallRecord, ToolCallRecord

        big_response = "R" * 5000 + "END_OF_RESPONSE"
        big_output = "O" * 5000 + "END_OF_OUTPUT"
        captures = [
            LLMCallRecord(
                seq=1,
                request_messages=[{"role": "user", "content": "x" * 5000}],
                request_tools=[],
                response_content=big_response,
                response_tool_calls=[],
                thinking=None,
                nudges_injected=[],
                duration_ms=1.0,
            )
        ]
        records = [
            ToolCallRecord(
                seq=0,
                name="read_logs",
                args={"lines": 500},
                output=big_output,
                error=None,
                success=True,
                discard_previous=False,
                duration_ms=3.0,
            )
        ]
        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), captures, [], records
        )
        assert sorted(p.name for p in episode_dir.iterdir()) == [
            "episode_data.json",
            "index.html",
        ]
        html = (episode_dir / "index.html").read_text()
        assert "END_OF_RESPONSE" in html
        assert "END_OF_OUTPUT" in html  # no 3000-char truncation

    def test_native_thinking_tokens_cached_and_model_saw_rendered(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html
        from utils.debug.capture import LLMCallRecord, ToolCallRecord

        captures = [
            LLMCallRecord(
                seq=1,
                request_messages=[],
                request_tools=[],
                response_content="Answer.",
                response_tool_calls=[],
                thinking=None,
                nudges_injected=[],
                duration_ms=5.0,
                thinking_native="NATIVE_REASONING",
                input_tokens=1200,
                output_tokens=34,
            )
        ]
        records = [
            ToolCallRecord(
                seq=0,
                name="read_config",
                args={},
                output="raw output",
                error=None,
                success=True,
                discard_previous=False,
                duration_ms=1.0,
                cached=True,
                model_saw="[Repeated call] raw output",
            )
        ]
        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), captures, [], records
        )
        html = (episode_dir / "index.html").read_text()
        assert "NATIVE_REASONING" in html
        assert "1200→34 tok" in html
        assert "cached (repeat call)" in html
        saw_len = len("[Repeated call] raw output")
        assert f"Model saw {saw_len} of 10 chars" in html

    def test_episode_data_json_written_without_tool_calls(self, tmp_path):
        """A session with LLM calls but no tool calls still writes JSON."""
        import json

        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir,
            self._make_session_meta(),
            self._make_captures(),
            [{"role": "user", "content": "hi"}],
            tool_call_records=None,
        )
        data = json.loads((episode_dir / "episode_data.json").read_text())
        assert data["schema_version"] == 2
        assert data["tool_calls"] == []
        assert len(data["llm_calls"]) == 2
        assert data["messages"] == [{"role": "user", "content": "hi"}]

    def test_build_episode_data_copies_v2_meta(self):
        from utils.debug.episode_writer import build_episode_data

        meta = {
            **self._make_session_meta(),
            "signature": "device:offline:zha",
            "subject_key": "dev1",
            "finish_tool": "finish_device_availability",
            "finish_args": {"outcome": "monitoring"},
            "offered_tools": ["read_logs"],
            "unrelated_key": "dropped",
        }
        data = build_episode_data(meta, [], [], None)
        assert data["episode_id"] == 42
        assert data["signature"] == "device:offline:zha"
        assert data["subject_key"] == "dev1"
        assert data["finish_args"] == {"outcome": "monitoring"}
        assert data["offered_tools"] == ["read_logs"]
        assert "unrelated_key" not in data
        assert "stuck_reason" not in data  # absent keys are not invented
        assert data["messages"] == []

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
        assert "No LLM calls were recorded" in html

    def test_system_prompt_only_in_appendix(self, tmp_path):
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        conv = [
            {"role": "system", "content": "SYSTEM PROMPT TEXT"},
            {"role": "user", "content": "Hello"},
        ]
        write_episode_html(episode_dir, self._make_session_meta(), [], conv)
        html = (episode_dir / "index.html").read_text()
        assert html.count("SYSTEM PROMPT TEXT") == 1
        assert html.index("SYSTEM PROMPT TEXT") > html.index('id="appendix"')
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

    def test_tool_message_without_record_rendered(self, tmp_path):
        """A tool message with no record or LLM call still appears, flagged."""
        from utils.debug.episode_writer import write_episode_html

        conv = [
            {"role": "user", "content": "check config"},
            {"role": "tool", "content": "home_assistant: true", "name": "read_config"},
        ]
        episode_dir = tmp_path / "ep"
        write_episode_html(episode_dir, self._make_session_meta(), [], conv)
        html = (episode_dir / "index.html").read_text()
        assert "read_config" in html
        assert "home_assistant: true" in html
        assert "no tool record" in html

    def test_records_without_llm_calls_rendered(self, tmp_path):
        """Tool records are shown even when no LLM call claimed them."""
        from utils.debug.episode_writer import write_episode_html
        from utils.debug.capture import ToolCallRecord

        records = [
            ToolCallRecord(
                seq=1,
                name="read_config",
                args={"path": "configuration.yaml"},
                output="domain: homeassistant",
                error=None,
                success=True,
                discard_previous=False,
                duration_ms=24.0,
            ),
            ToolCallRecord(
                seq=2,
                name="apply_fix",
                args={"yaml": "x: 1"},
                output="",
                error="Backup required",
                success=False,
                discard_previous=True,
                duration_ms=5.0,
            ),
        ]
        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), [], [], tool_call_records=records
        )
        html = (episode_dir / "index.html").read_text()
        assert "Tool calls without a recorded LLM call" in html
        assert "read_config" in html
        assert "apply_fix" in html
        assert "Backup required" in html
        assert "previous result discarded" in html
        assert "<th>Tool calls</th><td>2</td>" in html

    def test_no_unpaired_section_when_records_none(self, tmp_path):
        """No unpaired-tools section and a zero tool count without records."""
        from utils.debug.episode_writer import write_episode_html

        episode_dir = tmp_path / "ep"
        write_episode_html(
            episode_dir, self._make_session_meta(), [], [], tool_call_records=None
        )
        html = (episode_dir / "index.html").read_text()
        assert "Tool calls without a recorded LLM call" not in html
        assert "<th>Tool calls</th><td>0</td>" in html


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
    def db_path(self, monkeypatch, tmp_path, pueo_dirs):
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

    @staticmethod
    def _tc(name, args):
        return {"tool_calls": [{"function": {"name": name, "arguments": args}}]}

    def test_v2_llm_fields_captured(self, db_path):
        """Native thinking and Ollama token/timing stats land on the record."""
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        resp = self._tc("finish_chat", {"summary": "Done"})
        resp["_thinking"] = "native reasoning text"
        resp["_ollama_timing"] = {
            "eval_ms": 321.0,
            "load_ms": 12.0,
            "input_tokens": 900,
            "output_tokens": 40,
        }
        loop = self._make_loop(FakeToolCallingLLMClient([resp]), db_path)
        rec = asyncio.run(loop.run("Hello")).llm_captures[0]
        assert rec.thinking_native == "native reasoning text"
        assert rec.thinking is None  # <think>-tag parse is separate
        assert (rec.input_tokens, rec.output_tokens) == (900, 40)
        assert (rec.eval_ms, rec.load_ms) == (321.0, 12.0)

    def test_v2_llm_fields_none_without_provider_data(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient([self._tc("finish_chat", {"summary": "x"})])
        rec = asyncio.run(self._make_loop(llm, db_path).run("Hi")).llm_captures[0]
        assert rec.thinking_native is None
        assert rec.input_tokens is None and rec.eval_ms is None

    def test_cached_repeat_records_cached_and_model_saw(self, db_path):
        """A repeated identical call is marked cached; model_saw carries the
        '[Repeated call …]' text the model received.  The first execution's
        model_saw is None unless the guardrail changed the content."""
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                self._tc("recall", {"query": "server"}),
                self._tc("recall", {"query": "server"}),
                self._tc("finish_chat", {"summary": "Done"}),
            ]
        )
        result = asyncio.run(self._make_loop(llm, db_path).run("Hello"))
        first, second = [r for r in result.tool_call_records if r.name == "recall"]
        assert first.cached is False
        assert second.cached is True
        assert second.model_saw is not None
        assert second.model_saw.startswith("[Repeated call")
        raw_first = first.output if first.success else f"Error: {first.error}"
        assert first.model_saw is None or first.model_saw != raw_first
        tool_msgs = [m for m in result.debug_messages if m.get("role") == "tool"]
        assert tool_msgs[1]["content"] == second.model_saw

    def test_v2_session_meta_and_messages(self, db_path, pueo_dirs):
        """The loop exposes v2 meta + full history and writes them to JSON."""
        import json

        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                self._tc("recall", {"query": "server"}),
                self._tc("finish_chat", {"summary": "All done"}),
            ]
        )
        loop = self._make_loop(llm, db_path)
        result = asyncio.run(
            loop.run("Hello", signature="chat:general", subject_key="subj-1")
        )
        meta = result.debug_session_meta
        assert meta["schema_version"] == 2
        assert meta["signature"] == "chat:general"
        assert meta["subject_key"] == "subj-1"
        assert meta["finish_tool"] == "finish_chat"
        assert meta["finish_args"] == {"summary": "All done"}
        assert meta["steps"] == len(result.steps)
        assert meta["started_at"].endswith("Z") and meta["ended_at"].endswith("Z")
        assert meta["duration_seconds"] >= 0
        assert "finish_chat" in meta["offered_tools"]
        assert meta["system_prompt"]
        assert meta["provider"] in ("local", "cloud", "openai_compat")
        roles = [m["role"] for m in result.debug_messages]
        assert roles[0] == "system" and "tool" in roles and "assistant" in roles

        data = json.loads(
            (Path(result.debug_log_path).parent / "episode_data.json").read_text()
        )
        assert data["schema_version"] == 2
        assert data["signature"] == "chat:general"
        assert data["finish_args"] == {"summary": "All done"}
        assert any(m.get("name") == "recall" for m in data["messages"])

    def test_no_meta_when_capture_off(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient([self._tc("finish_chat", {"summary": "x"})])
        loop = self._make_loop(llm, db_path, capture_llm=False)
        result = asyncio.run(loop.run("Hi"))
        assert result.debug_session_meta == {}
        assert result.debug_messages == []

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

    def test_debug_level_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"debug_level": 2}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_LEVEL == 2

    def test_debug_level_1_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"debug_level": 1}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBUG_LEVEL == 1


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


# ---------------------------------------------------------------------------
# Schema-v2 pure helpers (#1055)
# ---------------------------------------------------------------------------


class TestEpisodeV2Helpers:
    def test_model_saw_none_when_unchanged(self):
        from utils.agent.agent_loop import _model_saw

        assert _model_saw("same", "same") is None
        assert _model_saw("raw", "trimmed") == "trimmed"

    def _step(self, name, args):
        from utils.agent.tool_registry import AgentStep, ToolCall, ToolResult

        return AgentStep(
            step_number=1,
            tool_call=ToolCall(name=name, arguments=args),
            tool_result=ToolResult(tool_name=name, success=True, output=""),
            timestamp=0.0,
        )

    def test_finish_call_picks_last_finish(self):
        from utils.agent.agent_loop import _finish_call

        steps = [
            self._step("read_logs", {}),
            self._step("finish_device_availability", {"outcome": "monitoring"}),
            self._step("read_logs", {"x": 1}),
        ]
        assert _finish_call(steps, "finish_repair") == (
            "finish_device_availability",
            {"outcome": "monitoring"},
        )
        assert _finish_call([self._step("custom_end", {"a": 1})], "custom_end") == (
            "custom_end",
            {"a": 1},
        )
        assert _finish_call([self._step("read_logs", {})], "finish_repair") == (
            "",
            {},
        )

    def test_iso_utc(self):
        from utils.agent.agent_loop import _iso_utc

        assert _iso_utc(0) == "1970-01-01T00:00:00Z"

    def test_build_debug_session_meta(self):
        from utils.agent.agent_loop import build_debug_session_meta

        meta = build_debug_session_meta(
            episode_id="ep1",
            outcome="stuck",
            model="m",
            provider="cloud",
            trigger="repair_poll",
            session_type="repair_issue",
            signature="repair:x:y",
            subject_key="s",
            initial_context="ctx",
            started_at=100.0,
            ended_at=112.5,
            steps=[self._step("finish_repair", {"outcome": "failed"})],
            terminal_tool_name="finish_repair",
            episode_stub={"summary": "gave up"},
            stuck_reason="host_sleep",
            top_retrieved_signature="repair:x:*",
            system_prompt="SYS",
            offered_tools=["read_logs", "finish_repair"],
        )
        assert meta["schema_version"] == 2
        assert meta["session_id"] == "ep1"
        assert meta["duration_seconds"] == 12.5
        assert meta["steps"] == 1
        assert meta["finish_tool"] == "finish_repair"
        assert meta["finish_args"] == {"outcome": "failed"}
        assert meta["episode_stub"] == {"summary": "gave up"}
        assert meta["stuck_reason"] == "host_sleep"
        assert meta["started_at"] == "1970-01-01T00:01:40Z"
        assert meta["provider"] == "cloud"

    def test_build_debug_session_meta_no_stub(self):
        from utils.agent.agent_loop import build_debug_session_meta

        meta = build_debug_session_meta(
            episode_id="e",
            outcome="exhausted",
            model="m",
            provider="local",
            trigger="t",
            session_type="t",
            signature="",
            subject_key="",
            initial_context="",
            started_at=1.0,
            ended_at=1.0,
            steps=[],
            terminal_tool_name="finish_repair",
            episode_stub=None,
            stuck_reason="",
            top_retrieved_signature="",
            system_prompt="",
            offered_tools=[],
        )
        assert meta["episode_stub"] is None
        assert meta["finish_tool"] == "" and meta["finish_args"] == {}

    def test_chat_episode_meta_overrides_trigger(self):
        from web.dashboard import _chat_episode_meta

        meta = _chat_episode_meta(
            {"trigger": "manual", "session_type": "chat", "signature": "chat:x"},
            session_id=7,
            outcome="success",
            model="qwen",
            agent_episode_id="uuid-1",
        )
        assert meta["trigger"] == "chat"
        assert meta["session_type"] == "chat"
        assert meta["session_id"] == 7
        assert meta["agent_episode_id"] == "uuid-1"
        assert meta["signature"] == "chat:x"
        assert meta["model"] == "qwen"

    def test_chat_episode_meta_without_loop_meta(self):
        from web.dashboard import _chat_episode_meta

        meta = _chat_episode_meta(
            {}, session_id=1, outcome="failed", model="m", agent_episode_id=None
        )
        assert meta["provider"] == "local"
        assert meta["timestamp"]
        assert meta["agent_episode_id"] == ""
