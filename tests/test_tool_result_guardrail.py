"""Tests for ToolResultGuardrail — each of the 10 rules plus integration path."""

from __future__ import annotations

import pytest

from utils.agent.tool_result_guardrail import ToolResultGuardrail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KNOWN_TOOLS = {
    "read_config",
    "read_logs",
    "read_file",
    "read_source",
    "apply_fix",
    "finish_repair",
    "query_knowledge",
}


def make_guardrail(**kwargs) -> ToolResultGuardrail:
    defaults: dict = {
        "max_result_tokens": 100,
        "max_tool_calls": 30,
        "known_tool_names": KNOWN_TOOLS,
    }
    defaults.update(kwargs)
    return ToolResultGuardrail(**defaults)


def passes_through(g: ToolResultGuardrail, output: str = "ok") -> bool:
    """Return True if the guardrail makes no change."""
    result = g.process("read_config", {}, output, success=True, remaining_budget=20)
    return result == output


# ---------------------------------------------------------------------------
# 1. output_too_large
# ---------------------------------------------------------------------------


def test_output_too_large_triggers() -> None:
    g = make_guardrail(max_result_tokens=10)
    big = "x" * 500  # way over 10-token limit (10 * 4 = 40 chars)
    result = g.process("read_config", {}, big, success=True, remaining_budget=20)
    assert "[SYSTEM NOTICE — output_too_large]" in result
    # Truncation — result should be much shorter than the original
    assert len(result) < len(big)


def test_output_small_does_not_trigger_size_rule() -> None:
    g = make_guardrail(max_result_tokens=1000)
    result = g.process(
        "read_config", {}, "small output", success=True, remaining_budget=20
    )
    assert "[SYSTEM NOTICE" not in result


# ---------------------------------------------------------------------------
# 2. error_pattern
# ---------------------------------------------------------------------------


def test_error_pattern_traceback_triggers() -> None:
    g = make_guardrail()
    output = (
        "Traceback (most recent call last):\n  File 'x.py', line 1\nValueError: bad"
    )
    result = g.process("read_config", {}, output, success=True, remaining_budget=20)
    assert "error_pattern" in result


def test_error_pattern_connection_refused_triggers() -> None:
    g = make_guardrail()
    output = "Connection refused while connecting to 192.168.1.5"
    result = g.process("read_config", {}, output, success=True, remaining_budget=20)
    assert "error_pattern" in result


def test_error_pattern_not_triggered_on_failed_result() -> None:
    """error_pattern only fires on success=True — a failed result is already signalled."""
    g = make_guardrail()
    output = "Error: Connection refused"
    result = g.process("read_config", {}, output, success=False, remaining_budget=20)
    # cascade_failure may fire after 3 failures; error_pattern should NOT be in result
    # for the first failure.
    assert "error_pattern" not in result


def test_error_pattern_clean_output_no_trigger() -> None:
    g = make_guardrail()
    result = g.process(
        "read_config",
        {},
        "homeassistant:\n  version: 2026.1",
        success=True,
        remaining_budget=20,
    )
    assert "error_pattern" not in result


# ---------------------------------------------------------------------------
# 3. repetition
# ---------------------------------------------------------------------------


def test_repetition_triggers_on_exact_repeat() -> None:
    g = make_guardrail()
    g.process(
        "read_config", {"path": "/config"}, "content", success=True, remaining_budget=20
    )
    result = g.process(
        "read_config", {"path": "/config"}, "content", success=True, remaining_budget=20
    )
    assert "repetition" in result


def test_repetition_does_not_trigger_on_different_args() -> None:
    g = make_guardrail()
    g.process("read_config", {"path": "/a"}, "a", success=True, remaining_budget=20)
    result = g.process(
        "read_config", {"path": "/b"}, "b", success=True, remaining_budget=20
    )
    assert "repetition" not in result


def test_repetition_does_not_trigger_on_first_call() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=20)
    assert "repetition" not in result


# ---------------------------------------------------------------------------
# 4. cascade_failure
# ---------------------------------------------------------------------------


def test_cascade_failure_triggers_after_three_failures() -> None:
    g = make_guardrail()
    g.process("read_config", {}, "Error: a", success=False, remaining_budget=20)
    g.process("read_logs", {}, "Error: b", success=False, remaining_budget=20)
    result = g.process("apply_fix", {}, "Error: c", success=False, remaining_budget=20)
    assert "cascade_failure" in result


def test_cascade_failure_resets_on_success() -> None:
    g = make_guardrail()
    g.process("read_config", {}, "err", success=False, remaining_budget=20)
    g.process("read_config", {}, "err", success=False, remaining_budget=20)
    g.process("read_config", {}, "ok", success=True, remaining_budget=20)  # resets
    result = g.process("read_config", {}, "err", success=False, remaining_budget=20)
    assert "cascade_failure" not in result


def test_cascade_failure_does_not_trigger_after_two() -> None:
    g = make_guardrail()
    g.process("read_config", {}, "err", success=False, remaining_budget=20)
    result = g.process("read_logs", {}, "err", success=False, remaining_budget=20)
    assert "cascade_failure" not in result


# ---------------------------------------------------------------------------
# 5. empty_output
# ---------------------------------------------------------------------------


def test_empty_output_triggers_for_content_tool() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "", success=True, remaining_budget=20)
    assert "empty_output" in result


def test_empty_output_does_not_trigger_for_non_content_tool() -> None:
    g = make_guardrail()
    result = g.process("finish_repair", {}, "", success=True, remaining_budget=20)
    assert "empty_output" not in result


def test_empty_output_does_not_trigger_on_failure() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "", success=False, remaining_budget=20)
    assert "empty_output" not in result


# ---------------------------------------------------------------------------
# 6. budget_pressure
# ---------------------------------------------------------------------------


def test_budget_pressure_triggers_at_five_remaining() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=5)
    assert "budget_pressure" in result


def test_budget_pressure_triggers_at_one_remaining() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=1)
    assert "budget_pressure" in result


def test_budget_pressure_does_not_trigger_above_five() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=6)
    assert "budget_pressure" not in result


def test_budget_pressure_does_not_trigger_at_zero() -> None:
    """Zero means the loop already handled budget-exhausted; don't double-fire."""
    g = make_guardrail()
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=0)
    assert "budget_pressure" not in result


# ---------------------------------------------------------------------------
# 7. circular_investigation
# ---------------------------------------------------------------------------


def test_circular_investigation_triggers_at_four_reads() -> None:
    g = make_guardrail()
    for _ in range(3):
        g.process("read_config", {}, "ok", success=True, remaining_budget=20)
    result = g.process("read_logs", {}, "ok", success=True, remaining_budget=20)
    assert "circular_investigation" in result


def test_circular_investigation_resets_after_action() -> None:
    g = make_guardrail()
    for _ in range(3):
        g.process("read_config", {}, "ok", success=True, remaining_budget=20)
    # An action tool should reset the counter.
    g.process("apply_fix", {}, "ok", success=True, remaining_budget=20)
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=20)
    assert "circular_investigation" not in result


def test_circular_investigation_does_not_trigger_early() -> None:
    g = make_guardrail()
    for _ in range(3):
        result = g.process("read_config", {}, "ok", success=True, remaining_budget=20)
    assert "circular_investigation" not in result


# ---------------------------------------------------------------------------
# 8. stale_re_read
# ---------------------------------------------------------------------------


def test_stale_re_read_triggers_after_fix_applied() -> None:
    g = make_guardrail()
    g.process(
        "read_file",
        {"path": "/config/configuration.yaml"},
        "yaml: content",
        success=True,
        remaining_budget=20,
    )
    g.process(
        "apply_fix",
        {"path": "/config/configuration.yaml"},
        "applied",
        success=True,
        remaining_budget=20,
    )
    result = g.process(
        "read_file",
        {"path": "/config/configuration.yaml"},
        "yaml: content",
        success=True,
        remaining_budget=20,
    )
    assert "stale_re_read" in result


def test_stale_re_read_does_not_trigger_without_fix() -> None:
    g = make_guardrail()
    g.process(
        "read_file",
        {"path": "/config/a.yaml"},
        "content",
        success=True,
        remaining_budget=20,
    )
    result = g.process(
        "read_file",
        {"path": "/config/a.yaml"},
        "content",
        success=True,
        remaining_budget=20,
    )
    assert "stale_re_read" not in result


def test_stale_re_read_does_not_trigger_for_different_file() -> None:
    g = make_guardrail()
    g.process(
        "apply_fix",
        {"path": "/config/a.yaml"},
        "applied",
        success=True,
        remaining_budget=20,
    )
    result = g.process(
        "read_file",
        {"path": "/config/b.yaml"},
        "content",
        success=True,
        remaining_budget=20,
    )
    assert "stale_re_read" not in result


# ---------------------------------------------------------------------------
# 9. hallucinated_tool
# ---------------------------------------------------------------------------


def test_hallucinated_tool_triggers_for_unknown_name() -> None:
    g = make_guardrail()
    result = g.process(
        "nonexistent_tool_xyz", {}, "ok", success=True, remaining_budget=20
    )
    assert "hallucinated_tool" in result


def test_hallucinated_tool_does_not_trigger_for_known_tool() -> None:
    g = make_guardrail()
    result = g.process("read_config", {}, "ok", success=True, remaining_budget=20)
    assert "hallucinated_tool" not in result


def test_hallucinated_tool_does_not_trigger_when_registry_empty() -> None:
    """Empty known_tools set means registry unknown — don't fire hallucinated_tool."""
    g = ToolResultGuardrail(
        max_result_tokens=4000, max_tool_calls=30, known_tool_names=set()
    )
    result = g.process("any_tool", {}, "ok", success=True, remaining_budget=20)
    assert "hallucinated_tool" not in result


# ---------------------------------------------------------------------------
# No-trigger path
# ---------------------------------------------------------------------------


def test_clean_result_passes_through_unchanged() -> None:
    g = make_guardrail(max_result_tokens=1000)
    output = "homeassistant:\n  url: http://homeassistant.local:8123"
    result = g.process("read_config", {}, output, success=True, remaining_budget=20)
    assert result == output
    assert "[SYSTEM NOTICE" not in result


# ---------------------------------------------------------------------------
# Integration: agent_loop uses guardrail
# ---------------------------------------------------------------------------


def test_agent_loop_guardrail_integration(pueo_dirs) -> None:
    """AgentLoop passes large tool output through the guardrail and the message
    received by the LLM on the second call contains the [SYSTEM NOTICE] prefix."""
    import asyncio
    from unittest.mock import patch

    from utils.agent.agent_loop import AgentLoop
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_ha_tool_registry
    from utils.ha.ssh_client import FakeSSHClient
    from utils.hitl.notify import FakeNotifier
    from utils.llm.ollama_client import FakeToolCallingLLMClient

    big_yaml = "# placeholder: " + ("x" * 5000)

    fake_ssh = FakeSSHClient(file_contents={"/config/configuration.yaml": big_yaml})
    executor = ToolExecutor(
        ha_ssh_client=fake_ssh,
        gate=FakeAutonomyGate(),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
    )

    llm = FakeToolCallingLLMClient(
        call_sequence=[
            {"tool_calls": [{"function": {"name": "read_config", "arguments": {}}}]},
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "finish_repair",
                            "arguments": {
                                "outcome": "success",
                                "summary": "done",
                                "action_taken": "none",
                            },
                        }
                    }
                ]
            },
        ]
    )

    with patch("utils.agent.agent_loop.AGENT_MAX_TOOL_RESULT_TOKENS", 10):
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=executor,
            tool_registry=build_ha_tool_registry(),
        )
        asyncio.run(loop.run("test trigger"))

    # The second LLM call receives messages including the tool result from
    # read_config.  That message should carry the guardrail notice.
    assert len(llm.calls) >= 2, "Expected at least two LLM calls"
    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert tool_messages, "No tool messages in second LLM call"
    assert any(
        "[SYSTEM NOTICE" in m.get("content", "") for m in tool_messages
    ), "Guardrail notice was not injected into the tool message content"
