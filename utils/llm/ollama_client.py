"""LLM client implementations: real (Ollama) and fake (for tests)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import ollama

from config import OLLAMA_ENDPOINT
from utils.core.logging import get_logger

log = get_logger("llm.ollama")


class OllamaClient:
    """Wraps ollama.chat behind the LLMClientProtocol interface."""

    def __init__(self) -> None:
        self._client = ollama.Client(host=OLLAMA_ENDPOINT)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> Any:
        t0 = time.monotonic()
        log.debug(
            "llm_request",
            model=model,
            call_type="chat",
            messages_count=len(messages),
            last_user_msg=(
                str(messages[-1].get("content", ""))[:300] if messages else ""
            ),
            messages_summary=[
                {"role": m["role"], "preview": str(m.get("content", ""))[:200]}
                for m in messages
            ],
        )
        resp = await asyncio.to_thread(
            lambda: self._client.chat(
                model=model,
                messages=messages,
                options=options,
                format=format,
            )
        )
        log.debug(
            "llm_response",
            model=model,
            call_type="chat",
            content_preview=str(
                getattr(getattr(resp, "message", None), "content", "") or ""
            )[:300],
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        return resp

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
    ) -> dict:
        t0 = time.monotonic()
        log.debug(
            "llm_request",
            model=model,
            messages_count=len(messages),
            tools_count=len(tools) if tools else 0,
            last_user_msg=(
                str(messages[-1].get("content", ""))[:300] if messages else ""
            ),
            messages_summary=[
                {"role": m["role"], "preview": str(m.get("content", ""))[:200]}
                for m in messages
            ],
        )
        resp = await asyncio.to_thread(
            lambda: self._client.chat(
                model=model,
                messages=messages,
                tools=tools,
                options=options or {"temperature": 0.0},
            )
        )
        msg = resp.message
        result: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "function": {
                        "name": tc.function.name,
                        "arguments": (
                            dict(tc.function.arguments) if tc.function.arguments else {}
                        ),
                    }
                }
                for tc in msg.tool_calls
            ]
        # Expose Ollama-native timings (nanoseconds → milliseconds).
        # eval_duration: pure generation; load_duration: model load.
        # Stored separately so load spikes don't inflate latency expectations.
        eval_ns = getattr(resp, "eval_duration", None)
        load_ns = getattr(resp, "load_duration", None)
        result["_ollama_timing"] = {
            "eval_ms": eval_ns / 1_000_000 if eval_ns else None,
            "load_ms": load_ns / 1_000_000 if load_ns else None,
        }
        tool_calls = result.get("tool_calls", [])
        log.debug(
            "llm_response",
            model=model,
            tool_calls=[tc["function"]["name"] for tc in tool_calls],
            content_preview=str(result.get("content", ""))[:300],
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        return result


class FakeLLMClient:
    """Returns a pre-configured JSON response for tests — no Ollama required."""

    def __init__(self, response_json: str) -> None:
        self._response_json = response_json
        self.calls: list[dict] = []

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> dict:
        self.calls.append({"model": model, "messages": messages})
        return {"message": {"content": self._response_json}}

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
    ) -> dict:
        self.calls.append({"model": model, "messages": messages})
        return {"role": "assistant", "content": ""}


class FakeToolCallingLLMClient:
    """Simulates tool-call sequences for AgentLoop tests — no Ollama required.

    call_sequence: list of response dicts. Each dict is either:
    - {"tool_calls": [{"function": {"name": "...", "arguments": {...}}}]}
      The loop will execute those tools and continue.
    - {"content": "some text"}
      The loop treats this as a natural termination (no tool calls).
    When the sequence is exhausted, returns an empty-content message.
    """

    def __init__(self, call_sequence: list[dict]) -> None:
        self._sequence = call_sequence
        self._index = 0
        self.calls: list[dict] = []

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> dict:
        self.calls.append({"model": model, "messages": messages})
        return {"message": {"content": "{}"}}

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
    ) -> dict:
        self.calls.append({"model": model, "messages": messages})
        if self._index >= len(self._sequence):
            return {"role": "assistant", "content": ""}
        resp = dict(self._sequence[self._index])
        self._index += 1
        return {"role": "assistant", **resp}
