"""OpenAI-compatible LLM client — implements LLMClientProtocol.

Translates Pueo's Ollama-shaped message history to OpenAI wire format and
normalises responses back so AgentLoop is unchanged.  Uses httpx (already in
requirements.txt) — no new dependency.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Literal, Union

import httpx

from utils.core.logging import get_logger

log = get_logger("llm.openai_compat")


class OpenAICompatClient:
    """Implements LLMClientProtocol against an OpenAI-compatible server.

    Translation responsibilities (AgentLoop is not modified):
    - _translate_messages_to_openai: converts Ollama-shaped history to OpenAI wire format
    - chat_with_tools: normalises response (arguments string → dict, keep tool id)
    - chat: uses response_format=json_object + schema in system prompt
    """

    def __init__(self) -> None:
        import config as _cfg

        self._base_url = _cfg.OPENAI_COMPAT_BASE_URL.rstrip("/")
        self._model = _cfg.OPENAI_COMPAT_MODEL
        self._api_key = os.environ.get("OPENAI_COMPAT_API_KEY", "not-needed")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(connect=10.0, read=1800.0, write=30.0, pool=5.0),
        )

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> Any:
        """Structured one-shot call; returns {"message": {"content": "<json>"}}."""
        t0 = time.monotonic()
        log.debug(
            "llm_request",
            model=model,
            call_type="chat",
            messages_count=len(messages),
        )

        # Inject schema into the system prompt so the model knows the target shape.
        openai_msgs = list(messages)
        schema_hint = (
            f"\n\nRespond ONLY with valid JSON matching this schema:\n"
            f"{json.dumps(format, indent=2)}"
        )
        if openai_msgs and openai_msgs[0].get("role") == "system":
            openai_msgs[0] = {
                **openai_msgs[0],
                "content": (openai_msgs[0].get("content", "") or "") + schema_hint,
            }
        else:
            openai_msgs.insert(0, {"role": "system", "content": schema_hint})

        temperature = options.get("temperature", 0.0) if options else 0.0
        payload: dict[str, Any] = {
            "model": model,
            "messages": openai_msgs,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content", "") or ""
        duration_ms = round((time.monotonic() - t0) * 1000)
        log.debug(
            "llm_response", model=model, call_type="chat", duration_ms=duration_ms
        )
        return {"message": {"content": content}}

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
        think: Union[bool, Literal["low", "medium", "high"], None] = None,
        keep_alive: int | str | None = None,
    ) -> dict:
        """Tool-calling call; normalises response to Ollama-compatible shape."""
        t0 = time.monotonic()
        log.debug(
            "llm_request",
            model=model,
            messages_count=len(messages),
            tools_count=len(tools) if tools else 0,
            last_user_msg=(
                str(messages[-1].get("content", ""))[:300] if messages else ""
            ),
        )
        if log._logger.isEnabledFor(logging.DEBUG):
            log.debug(
                "llm_request_full",
                messages=messages,
                tools=[t["function"]["name"] for t in (tools or [])],
            )

        temperature = options.get("temperature", 0.0) if options else 0.0
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._translate_messages_to_openai(messages),
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        msg = data["choices"][0]["message"]
        content: str = msg.get("content") or ""
        tool_calls_raw: list[dict] = msg.get("tool_calls") or []

        # Normalise tool calls: arguments JSON string → dict, preserve id.
        normalised_tool_calls: list[dict[str, Any]] = []
        for tc in tool_calls_raw:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            try:
                args_dict = (
                    json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                )
            except json.JSONDecodeError:
                args_dict = {}
            normalised_tool_calls.append(
                {
                    "id": tc.get("id"),
                    "function": {"name": fn.get("name", ""), "arguments": args_dict},
                }
            )

        usage = data.get("usage", {})
        result: dict = {
            "role": "assistant",
            "content": content,
            "_ollama_timing": {
                "eval_ms": None,
                "load_ms": None,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            },
        }
        if normalised_tool_calls:
            result["tool_calls"] = normalised_tool_calls

        duration_ms = round((time.monotonic() - t0) * 1000)
        tool_names = [tc["function"]["name"] for tc in normalised_tool_calls]
        log.debug(
            "llm_response",
            model=model,
            tool_calls=tool_names,
            content_preview=str(content)[:300],
            duration_ms=duration_ms,
        )
        if log._logger.isEnabledFor(logging.DEBUG):
            log.debug(
                "llm_response_full",
                content=content,
                tool_calls=[
                    {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    }
                    for tc in normalised_tool_calls
                ],
                duration_ms=duration_ms,
            )
        return result

    def _translate_messages_to_openai(self, messages: list[dict]) -> list[dict]:
        """Convert Ollama/Pueo message history to OpenAI wire format.

        Handles the tool-result id threading: walks forward tracking
        {function_name: tc_id} from each assistant turn so tool result messages
        get the right tool_call_id.
        """
        result: list[dict] = []
        # Map function name → tc_id from the most recent assistant message with tool calls.
        # Rebuilt on every assistant+tool_calls message encountered.
        pending_ids: dict[str, str] = {}

        for seq, msg in enumerate(messages, start=1):
            role = msg.get("role", "")

            if role in ("system", "user"):
                result.append({"role": role, "content": msg.get("content", "") or ""})

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    result.append(
                        {"role": "assistant", "content": msg.get("content", "") or ""}
                    )
                    pending_ids = {}
                else:
                    # Rebuild the name→id map for this assistant turn.
                    openai_tcs = []
                    new_pending: dict[str, str] = {}
                    for tc_seq, tc in enumerate(tool_calls, start=1):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        tc_id = tc.get("id") or f"tc_{seq:04d}_{tc_seq:02d}"
                        args = fn.get("arguments", {})
                        openai_tcs.append(
                            {
                                "id": tc_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": (
                                        json.dumps(args)
                                        if isinstance(args, dict)
                                        else args
                                    ),
                                },
                            }
                        )
                        # First encounter of this name wins for the lookup.
                        if name not in new_pending:
                            new_pending[name] = tc_id
                    pending_ids = new_pending
                    result.append({"role": "assistant", "tool_calls": openai_tcs})

            elif role == "tool":
                name = msg.get("name", "")
                tc_id = pending_ids.pop(name, f"tc_unknown_{seq:04d}")
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": msg.get("content", "") or "",
                    }
                )

        return result


class FakeOpenAICompatClient:
    """Fake for unit tests — mirrors FakeToolCallingLLMClient call-sequence pattern."""

    def __init__(self, call_sequence: list[dict] | None = None) -> None:
        self._sequence = call_sequence or []
        self._index = 0
        self.calls: list[dict] = []

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> dict:
        self.calls.append({"model": model, "messages": messages, "type": "chat"})
        return {"message": {"content": "{}"}}

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
        think: Union[bool, Literal["low", "medium", "high"], None] = None,
        keep_alive: int | str | None = None,
    ) -> dict:
        self.calls.append(
            {"model": model, "messages": messages, "type": "chat_with_tools"}
        )
        if self._index >= len(self._sequence):
            return {"role": "assistant", "content": ""}
        resp = dict(self._sequence[self._index])
        self._index += 1
        return {"role": "assistant", **resp}
