"""LLM interaction trace capture for HITL payload enrichment (item 23)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from utils.context import truncate_to_budget

_FIELD_CHAR_LIMIT = 4000
_TRUNCATION_MARKER = "\n...[truncated]..."


def _truncate(s: str, limit: int = _FIELD_CHAR_LIMIT) -> str:
    token_limit = limit // 4
    truncated = truncate_to_budget(s, token_limit, "head")
    if len(truncated) < len(s):
        return truncated[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    return truncated


@dataclass
class LLMTrace:
    model: str
    system_prompt: str
    user_prompt: str
    raw_response: str
    timestamp: int = field(default_factory=lambda: int(time.time()))

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "system_prompt": _truncate(self.system_prompt),
            "user_prompt": _truncate(self.user_prompt),
            "raw_response": self.raw_response,
            "timestamp": self.timestamp,
        }
