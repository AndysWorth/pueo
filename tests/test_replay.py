"""Unit tests for deterministic episode replay."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures


def _make_finish_tool_call(tool_name: str = "finish_repair") -> dict:
    return {
        "function": {
            "name": tool_name,
            "arguments": {
                "outcome": "success",
                "summary": "Fixed",
                "human_explanation": "Fixed",
            },
        }
    }


def _make_llm_call(
    seq: int = 0,
    tool_calls: list | None = None,
    content: str = "",
    outcome_path: str | None = None,
) -> dict:
    return {
        "seq": seq,
        "request_messages": [{"role": "user", "content": "diagnose"}],
        "request_tools": ["read_logs", "finish_repair"],
        "response_content": content,
        "response_tool_calls": tool_calls or [],
        "thinking": None,
        "nudges_injected": [],
        "duration_ms": 500.0,
        "outcome_path": outcome_path,
    }


def _make_tool_call_record(
    seq: int = 0,
    name: str = "read_logs",
    output: str = "log output",
    success: bool = True,
    error: str | None = None,
    discard_previous: bool = False,
) -> dict:
    return {
        "seq": seq,
        "name": name,
        "args": {},
        "output": output,
        "error": error,
        "success": success,
        "discard_previous": discard_previous,
        "duration_ms": 50.0,
    }


def _make_episode_data(
    episode_id: str = "test-uuid",
    trigger: str = "repair_poll",
    outcome: str = "success",
    llm_calls: list | None = None,
    tool_calls: list | None = None,
    initial_context: str = "diagnose the system",
) -> dict:
    if llm_calls is None:
        llm_calls = [
            _make_llm_call(
                seq=0,
                tool_calls=[_make_finish_tool_call()],
                outcome_path="finish_repair",
            )
        ]
    if tool_calls is None:
        # finish_repair is dispatched through the executor like any tool
        tool_calls = [
            _make_tool_call_record(
                seq=0,
                name="finish_repair",
                output=str(
                    {
                        "outcome": "success",
                        "summary": "Fixed",
                        "human_explanation": "Fixed",
                    }
                ),
            )
        ]
    return {
        "episode_id": episode_id,
        "trigger": trigger,
        "model": "test-model",
        "outcome": outcome,
        "timestamp": "2026-09-14 10:00:00",
        "initial_context": initial_context,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
    }


# ---------------------------------------------------------------------------
# ToolCallRecord + episode_data.json serialization


class TestEpisodeDataJsonWritten:
    def test_episode_data_json_written(self, tmp_path):
        """write_episode_html writes episode_data.json when tool_call_records passed."""
        from utils.debug.capture import LLMCallRecord, ToolCallRecord
        from utils.debug.episode_writer import write_episode_html

        ep_dir = tmp_path / "test_ep"
        session_meta = {
            "session_id": "abc-123",
            "outcome": "success",
            "model": "qwen3",
            "provider": "local",
            "timestamp": "2026-09-14 10:00:00",
            "trigger": "repair_poll",
            "initial_context": "diagnose thing",
        }
        captures = [
            LLMCallRecord(
                seq=0,
                request_messages=[],
                request_tools=[],
                response_content="",
                response_tool_calls=[],
                thinking=None,
                nudges_injected=[],
                duration_ms=100.0,
            )
        ]
        tool_records = [
            ToolCallRecord(
                seq=0,
                name="read_logs",
                args={},
                output="some logs",
                error=None,
                success=True,
                discard_previous=False,
                duration_ms=45.0,
            )
        ]
        write_episode_html(ep_dir, session_meta, captures, [], tool_records)

        data_file = ep_dir / "episode_data.json"
        assert data_file.exists()

    def test_episode_data_json_not_written_without_tool_records(self, tmp_path):
        """write_episode_html does NOT write episode_data.json when records absent."""
        from utils.debug.capture import LLMCallRecord
        from utils.debug.episode_writer import write_episode_html

        ep_dir = tmp_path / "test_ep2"
        session_meta = {
            "session_id": "xyz",
            "outcome": "success",
            "model": "qwen3",
            "provider": "local",
            "timestamp": "2026-09-14",
            "trigger": "manual",
        }
        write_episode_html(ep_dir, session_meta, [], [], None)
        assert not (ep_dir / "episode_data.json").exists()

    def test_load_episode_data_round_trip(self, tmp_path):
        """Fields survive write_episode_html → episode_data.json → load round trip."""
        from utils.debug.capture import LLMCallRecord, ToolCallRecord
        from utils.debug.episode_writer import write_episode_html
        from utils.replay.episode_replayer import EpisodeReplayer

        ep_dir = tmp_path / "round_trip"
        session_meta = {
            "session_id": "round-trip-id",
            "outcome": "success",
            "model": "qwen3",
            "provider": "local",
            "timestamp": "2026-09-14 11:00:00",
            "trigger": "repair_poll",
            "initial_context": "fix the thing",
        }
        captures = [
            LLMCallRecord(
                seq=0,
                request_messages=[{"role": "user", "content": "go"}],
                request_tools=["read_logs"],
                response_content="ok",
                response_tool_calls=[],
                thinking=None,
                nudges_injected=[],
                duration_ms=200.0,
            )
        ]
        tool_records = [
            ToolCallRecord(
                seq=0,
                name="read_logs",
                args={"lines": 50},
                output="log output",
                error=None,
                success=True,
                discard_previous=False,
                duration_ms=30.0,
            )
        ]
        write_episode_html(ep_dir, session_meta, captures, [], tool_records)

        replayer = EpisodeReplayer()
        data = replayer.load_episode_data(ep_dir)

        assert data["episode_id"] == "round-trip-id"
        assert data["trigger"] == "repair_poll"
        assert data["initial_context"] == "fix the thing"
        assert len(data["llm_calls"]) == 1
        assert data["llm_calls"][0]["request_tools"] == ["read_logs"]
        assert len(data["tool_calls"]) == 1
        assert data["tool_calls"][0]["name"] == "read_logs"
        assert data["tool_calls"][0]["output"] == "log output"


# ---------------------------------------------------------------------------
# ReplayLLMClient


class TestReplayLLMClient:
    def test_returns_recorded_responses_in_order(self):
        """ReplayLLMClient returns responses in sequence."""
        from utils.replay.replay_client import ReplayLLMClient

        calls = [
            _make_llm_call(seq=0, content="first"),
            _make_llm_call(seq=1, content="second"),
        ]
        client = ReplayLLMClient(calls)

        async def _run() -> tuple:
            r1 = await client.chat_with_tools("model", [], [])
            r2 = await client.chat_with_tools("model", [], [])
            return r1, r2

        r1, r2 = asyncio.run(_run())
        assert r1["content"] == "first"
        assert r2["content"] == "second"

    def test_raises_when_exhausted(self):
        """ReplayLLMClient raises ReplayExhaustedError when calls run out."""
        from utils.replay.replay_client import ReplayExhaustedError, ReplayLLMClient

        client = ReplayLLMClient([])
        with pytest.raises(ReplayExhaustedError):
            asyncio.run(client.chat_with_tools("model", [], []))


# ---------------------------------------------------------------------------
# ReplayToolExecutor


class TestReplayToolExecutor:
    def _run(self, coro):
        return asyncio.run(coro)

    def _make_tool_call(self, name: str) -> object:
        from utils.agent.tool_registry import ToolCall

        return ToolCall(name=name, arguments={})

    def test_returns_recorded_result(self):
        """ReplayToolExecutor returns pre-recorded output."""
        from utils.replay.replay_executor import ReplayToolExecutor

        records = [_make_tool_call_record(name="read_logs", output="log data")]
        executor = ReplayToolExecutor(records)
        result = self._run(executor.execute(self._make_tool_call("read_logs")))
        assert result.success is True
        assert result.output == "log data"

    def test_strict_mode_raises_on_wrong_tool(self):
        """ReplayToolExecutor raises ReplayDivergenceError on tool name mismatch in strict mode."""
        from utils.replay.replay_executor import (
            ReplayDivergenceError,
            ReplayToolExecutor,
        )

        records = [_make_tool_call_record(name="read_logs")]
        executor = ReplayToolExecutor(records, strict=True)
        with pytest.raises(ReplayDivergenceError):
            self._run(executor.execute(self._make_tool_call("read_config")))

    def test_loose_mode_tolerates_tool_rename(self):
        """ReplayToolExecutor does NOT raise on tool name mismatch in loose mode."""
        from utils.replay.replay_executor import ReplayToolExecutor

        records = [_make_tool_call_record(name="read_logs", output="data")]
        executor = ReplayToolExecutor(records, strict=False)
        result = self._run(executor.execute(self._make_tool_call("read_config")))
        assert result.success is True
        assert result.output == "data"

    def test_raises_when_exhausted(self):
        """ReplayToolExecutor raises ReplayExhaustedError when records run out."""
        from utils.replay.replay_executor import (
            ReplayExhaustedError,
            ReplayToolExecutor,
        )

        executor = ReplayToolExecutor([])
        with pytest.raises(ReplayExhaustedError):
            self._run(executor.execute(self._make_tool_call("read_logs")))

    def test_error_result_surfaced(self):
        """ReplayToolExecutor surfaces pre-recorded errors."""
        from utils.replay.replay_executor import ReplayToolExecutor

        records = [
            _make_tool_call_record(
                name="read_logs", output="", success=False, error="SSH failed"
            )
        ]
        executor = ReplayToolExecutor(records)
        result = self._run(executor.execute(self._make_tool_call("read_logs")))
        assert result.success is False
        assert result.error == "SSH failed"


# ---------------------------------------------------------------------------
# EpisodeReplayer — clean replay


class TestEpisodeReplayer:
    def _run(self, coro):
        return asyncio.run(coro)

    def _make_minimal_registry(self):
        """Build a minimal ToolRegistry with just finish_repair."""
        from utils.agent.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        reg.register(
            ToolDefinition(
                name="finish_repair",
                description="Complete the session",
                parameters={
                    "type": "object",
                    "properties": {
                        "outcome": {"type": "string"},
                        "summary": {"type": "string"},
                        "human_explanation": {"type": "string"},
                    },
                    "required": ["outcome", "summary", "human_explanation"],
                },
            )
        )
        return reg

    def test_clean_replay_matches_outcome(self):
        """Deterministic replay of a simple success episode returns matched=True."""
        from utils.replay.episode_replayer import EpisodeReplayer

        data = _make_episode_data(
            outcome="success",
            llm_calls=[
                _make_llm_call(
                    seq=0,
                    tool_calls=[_make_finish_tool_call("finish_repair")],
                    outcome_path="finish_repair",
                )
            ],
            # finish_repair is dispatched through executor — use the default which includes it
        )
        replayer = EpisodeReplayer()
        result = self._run(
            replayer.run_deterministic(
                data, tool_registry=self._make_minimal_registry()
            )
        )
        assert result.matched is True
        assert result.replay_outcome == "success"
        assert result.error is None

    def test_divergence_on_extra_llm_call(self):
        """Replay fails gracefully when the loop makes more LLM calls than recorded."""
        from utils.agent.tool_registry import ToolDefinition, ToolRegistry
        from utils.replay.episode_replayer import EpisodeReplayer

        # Registry with read_logs so the model's tool call is valid, but
        # episode data has zero LLM calls — immediately exhausted.
        reg = ToolRegistry()
        reg.register(
            ToolDefinition(
                name="finish_repair",
                description="done",
                parameters={
                    "type": "object",
                    "properties": {
                        "outcome": {"type": "string"},
                        "summary": {"type": "string"},
                        "human_explanation": {"type": "string"},
                    },
                    "required": ["outcome", "summary", "human_explanation"],
                },
            )
        )
        # Zero LLM calls — any call at all will exhaust the replay client
        data = _make_episode_data(outcome="success", llm_calls=[], tool_calls=[])
        replayer = EpisodeReplayer()
        result = self._run(replayer.run_deterministic(data, tool_registry=reg))
        assert result.matched is False
        assert "exhausted" in result.replay_outcome or result.error is not None
