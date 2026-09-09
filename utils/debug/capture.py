"""LLM call capture dataclass for debug episode recording."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMCallRecord:
    seq: int
    request_messages: list[dict[str, Any]]
    request_tools: list[str]
    response_content: str
    response_tool_calls: list[dict[str, Any]]
    thinking: str | None
    nudges_injected: list[str]
    duration_ms: float
    # "finish_chat" | "exhaustion_fallback" | None (mid-session or non-terminal)
    outcome_path: str | None = None

    def __post_init__(self) -> None:
        # Defensive copies so callers can mutate original lists safely.
        self.request_messages = list(self.request_messages)
        self.request_tools = list(self.request_tools)
        self.response_tool_calls = list(self.response_tool_calls)
        self.nudges_injected = list(self.nudges_injected)


def record_one_shot(
    caller: str,
    model: str,
    input_summary: str,
    output_summary: str,
    duration_ms: float,
    outcome: str,
    db_path: str,
) -> None:
    """Insert a one-shot LLM call record into llm_one_shot_calls. Never raises."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO llm_one_shot_calls"
                " (caller, model, input_summary, output_summary, duration_ms, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    caller,
                    model,
                    input_summary[:500],
                    output_summary[:500],
                    duration_ms,
                    outcome,
                ),
            )
    except Exception:  # nosec B110
        pass
