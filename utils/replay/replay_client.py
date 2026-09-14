"""Replay LLM client — returns pre-recorded responses for deterministic replay."""

from __future__ import annotations

from typing import Any


class ReplayExhaustedError(Exception):
    """The replay client ran out of recorded LLM calls."""


class ReplayLLMClient:
    """Implements LLMClientProtocol by replaying pre-recorded responses.

    Construct with the ``llm_calls`` list from ``episode_data.json`` and pass
    as the ``llm_client`` argument to ``AgentLoop``.  Each ``chat_with_tools``
    call returns the next recorded response in sequence.
    """

    def __init__(self, llm_calls: list[dict[str, Any]]) -> None:
        self._calls = llm_calls
        self._pos = 0

    @property
    def calls_remaining(self) -> int:
        return len(self._calls) - self._pos

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        format: dict[str, Any],
    ) -> Any:
        raise NotImplementedError("ReplayLLMClient does not support structured chat")

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._pos >= len(self._calls):
            raise ReplayExhaustedError(
                f"Ran out of recorded LLM calls at position {self._pos} "
                f"(recorded {len(self._calls)} total)"
            )
        call = self._calls[self._pos]
        self._pos += 1
        # OllamaClient returns tool_calls and content at the top level,
        # not nested under "message".
        result: dict[str, Any] = {
            "role": "assistant",
            "content": call.get("response_content", "") or "",
            "_ollama_timing": {"eval_ms": call.get("duration_ms", 0), "load_ms": 0},
        }
        tool_calls = call.get("response_tool_calls") or []
        if tool_calls:
            result["tool_calls"] = list(tool_calls)
        return result
