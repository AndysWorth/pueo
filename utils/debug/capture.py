"""LLM call capture dataclass for debug episode recording."""

from __future__ import annotations

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
