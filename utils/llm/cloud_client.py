"""Anthropic Claude API client implementing LLMClientProtocol.

Translates between Pueo's Ollama-shaped interface and the Anthropic Messages API:
  - Tool schema adapter   : OpenAI/Ollama → Anthropic format
  - Response normalizer   : Anthropic content blocks → {tool_calls: [...]} dict
  - History translator    : Ollama-shaped message history → Anthropic messages + system
  - Structured output     : tool-forcing instead of Ollama's format= parameter
  - Billing guard         : per-incident and daily spend caps enforced per call
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import config


def _adapt_tool_schema(tool: dict) -> dict:
    """Strip the OpenAI function envelope and rename parameters → input_schema."""
    fn = tool.get("function", tool)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _translate_history(messages: list[dict]) -> tuple[str, list[dict]]:
    """Translate Ollama-shaped messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).

    Handles the asymmetry between formats:
    - System messages become a top-level system param (extracted, not in messages list)
    - Tool calls use stable sequential IDs (toolu_NNNN) assigned in history order
    - Tool results and the "Continue..." nudge are merged into one user message so
      Anthropic's alternating-role constraint is satisfied
    """
    system = ""
    result: list[dict] = []
    tool_counter = 0

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "system":
            system = msg.get("content", "")
            i += 1
            continue

        if role == "user":
            content = msg.get("content", "")
            result.append({"role": "user", "content": content})
            i += 1
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            text_content = msg.get("content", "")

            if tool_calls:
                # Build Anthropic assistant content blocks with stable IDs
                content_blocks = []
                tc_ids = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    tool_counter += 1
                    tc_id = f"toolu_{tool_counter:04d}"
                    tc_ids.append(tc_id)
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": fn.get("name", ""),
                            "input": fn.get("arguments", {}),
                        }
                    )
                result.append({"role": "assistant", "content": content_blocks})
                i += 1

                # Consume matching tool result messages
                tool_result_blocks: list[dict] = []
                for tc_id in tc_ids:
                    if i < len(messages) and messages[i].get("role") == "tool":
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tc_id,
                                "content": messages[i].get("content", ""),
                            }
                        )
                        i += 1

                if tool_result_blocks:
                    # Merge the "Continue..." nudge (next user message, if present)
                    # into the same user message so there's no consecutive user msgs
                    user_content: list[dict] = list(tool_result_blocks)
                    if i < len(messages) and messages[i].get("role") == "user":
                        nudge = messages[i].get("content", "")
                        if nudge:
                            user_content.append({"type": "text", "text": nudge})
                        i += 1
                    result.append({"role": "user", "content": user_content})
            else:
                # Plain text assistant turn
                if text_content:
                    result.append({"role": "assistant", "content": text_content})
                i += 1
            continue

        # Skip standalone tool messages that don't follow an assistant turn
        i += 1

    return system, result


def _count_message_chars(messages: list[dict]) -> int:
    """Rough character count across all message content for token estimation."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("content", ""))) + len(
                        str(block.get("text", ""))
                    )
    return total


class ClaudeAPIClient:
    """Anthropic Claude client implementing LLMClientProtocol."""

    def __init__(self, incident_id: str = "") -> None:
        import anthropic  # lazy import — only loaded when provider = cloud/both

        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Export it in ~/.zshenv and reload your shell."
            )
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._incident_id = incident_id

    def _billing_preflight(
        self, model: str, messages: list[dict], tools: list[dict] | None = None
    ) -> None:
        """Raise BillingCapError before making an API call if caps would be exceeded."""
        if not getattr(self, "_incident_id", ""):
            return
        from utils.billing import check_billing_caps

        char_count = _count_message_chars(messages)
        if tools:
            for t in tools:
                char_count += len(json.dumps(t))
        estimated_input = char_count // 4
        estimated_output = 512  # conservative output estimate
        check_billing_caps(self._incident_id, model, estimated_input, estimated_output)

    def _billing_record(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> None:
        """Record actual token usage after a successful API call."""
        if not getattr(self, "_incident_id", ""):
            return
        from utils.billing import estimate_cost, record_cloud_spend

        cost = estimate_cost(model, input_tokens, output_tokens)
        record_cloud_spend(self._incident_id, model, input_tokens, output_tokens, cost)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict,
        format: dict,
    ) -> Any:
        """Structured output via Anthropic tool-forcing.

        Defines a single tool with the target Pydantic schema as input_schema,
        forces the model to call it, then returns the input field as JSON content
        in the same shape OllamaClient.chat() returns: {"message": {"content": "..."}}
        """
        _SCHEMA_TOOL_NAME = "structured_output"
        tools = [
            {
                "name": _SCHEMA_TOOL_NAME,
                "description": "Return structured output matching the schema exactly.",
                "input_schema": format,
            }
        ]
        self._billing_preflight(model, messages, tools)
        system, anthropic_messages = _translate_history(messages)
        kwargs: dict = dict(
            model=model,
            messages=anthropic_messages,
            tools=tools,
            tool_choice={"type": "tool", "name": _SCHEMA_TOOL_NAME},
            max_tokens=4096,
        )
        if system:
            kwargs["system"] = system

        response = await asyncio.to_thread(
            lambda: self._client.messages.create(**kwargs)
        )
        self._billing_record(
            model,
            getattr(response.usage, "input_tokens", 0),
            getattr(response.usage, "output_tokens", 0),
        )

        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                return {"message": {"content": json.dumps(block.input)}}

        return {"message": {"content": "{}"}}

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
    ) -> dict:
        """Tool-calling chat against the Anthropic Messages API.

        Translates full history to Anthropic format on each call, makes the API
        request, then normalizes the response back to the Ollama-shaped dict that
        agent_loop.py expects.
        """
        self._billing_preflight(model, messages, tools)
        system, anthropic_messages = _translate_history(messages)
        anthropic_tools = [_adapt_tool_schema(t) for t in tools]
        kwargs: dict = dict(
            model=model,
            messages=anthropic_messages,
            tools=anthropic_tools,
            max_tokens=4096,
        )
        if system:
            kwargs["system"] = system

        response = await asyncio.to_thread(
            lambda: self._client.messages.create(**kwargs)
        )
        self._billing_record(
            model,
            getattr(response.usage, "input_tokens", 0),
            getattr(response.usage, "output_tokens", 0),
        )

        # Normalize to Ollama-shaped response dict
        result: dict = {"role": "assistant", "content": ""}
        tool_calls = []

        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "tool_use":
                tool_calls.append(
                    {
                        "function": {
                            "name": block.name,
                            "arguments": dict(block.input) if block.input else {},
                        }
                    }
                )
            elif block_type == "text":
                result["content"] = block.text

        if tool_calls:
            result["tool_calls"] = tool_calls

        return result
