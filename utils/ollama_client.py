"""LLM client implementations: real (Ollama) and fake (for tests)."""

from __future__ import annotations

import asyncio
from typing import Any

import ollama


class OllamaClient:
    """Wraps ollama.chat behind the LLMClientProtocol interface."""

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> Any:
        return await asyncio.to_thread(
            lambda: ollama.chat(
                model=model,
                messages=messages,
                options=options,
                format=format,
            )
        )


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
