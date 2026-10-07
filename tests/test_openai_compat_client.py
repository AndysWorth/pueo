"""Unit tests for OpenAICompatClient and FakeOpenAICompatClient."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client():
    """Return an OpenAICompatClient with internals patched to known values."""
    from utils.llm.openai_compat_client import OpenAICompatClient

    client = OpenAICompatClient.__new__(OpenAICompatClient)
    client._base_url = "http://localhost:1234/v1"
    client._model = "qwen2.5-coder:7b"
    client._api_key = "not-needed"
    client._client = MagicMock()
    return client


def _choices_response(
    content: str = "",
    tool_calls: list[dict] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> dict:
    msg: dict = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    return {
        "choices": [{"message": msg, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _mock_http_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# _translate_messages_to_openai
# ---------------------------------------------------------------------------


class TestTranslateMessages:
    def _translate(self, messages: list[dict]) -> list[dict]:
        from utils.llm.openai_compat_client import OpenAICompatClient

        obj = OpenAICompatClient.__new__(OpenAICompatClient)
        return obj._translate_messages_to_openai(messages)

    def test_system_and_user_pass_through(self):
        msgs = [
            {"role": "system", "content": "You are Pueo."},
            {"role": "user", "content": "What is the issue?"},
        ]
        result = self._translate(msgs)
        assert result == msgs

    def test_assistant_no_tool_calls(self):
        msgs = [{"role": "assistant", "content": "I looked at the logs."}]
        result = self._translate(msgs)
        assert result == [{"role": "assistant", "content": "I looked at the logs."}]

    def test_assistant_tool_calls_dict_args_serialised(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_config",
                            "arguments": {"path": "/config"},
                        }
                    }
                ],
            }
        ]
        result = self._translate(msgs)
        assert len(result) == 1
        tc = result[0]["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "read_config"
        # arguments must be a JSON string
        assert isinstance(tc["function"]["arguments"], str)
        assert json.loads(tc["function"]["arguments"]) == {"path": "/config"}

    def test_assistant_tool_calls_string_args_left_alone(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_config",
                            "arguments": '{"path": "/config"}',
                        }
                    }
                ],
            }
        ]
        result = self._translate(msgs)
        tc = result[0]["tool_calls"][0]
        assert tc["function"]["arguments"] == '{"path": "/config"}'

    def test_tool_result_gets_matching_id(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "function": {"name": "read_config", "arguments": {}},
                    }
                ],
            },
            {"role": "tool", "name": "read_config", "content": "result-yaml"},
        ]
        result = self._translate(msgs)
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "call_abc"
        assert result[1]["content"] == "result-yaml"

    def test_tool_result_fallback_id_when_no_id_on_assistant(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "read_logs", "arguments": {}}}],
            },
            {"role": "tool", "name": "read_logs", "content": "log lines"},
        ]
        result = self._translate(msgs)
        assigned_id = result[0]["tool_calls"][0]["id"]
        assert assigned_id
        assert result[1]["tool_call_id"] == assigned_id

    def test_multiple_tool_calls_matched_by_name(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "id_a", "function": {"name": "tool_a", "arguments": {}}},
                    {"id": "id_b", "function": {"name": "tool_b", "arguments": {}}},
                ],
            },
            {"role": "tool", "name": "tool_b", "content": "result-b"},
            {"role": "tool", "name": "tool_a", "content": "result-a"},
        ]
        result = self._translate(msgs)
        tool_results = [m for m in result if m.get("role") == "tool"]
        assert tool_results[0]["tool_call_id"] == "id_b"
        assert tool_results[1]["tool_call_id"] == "id_a"

    def test_round_trip_preserves_content(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user msg"},
            {"role": "assistant", "content": "thinking"},
            {"role": "user", "content": "follow-up"},
        ]
        result = self._translate(msgs)
        assert [m["content"] for m in result] == [
            "sys",
            "user msg",
            "thinking",
            "follow-up",
        ]


# ---------------------------------------------------------------------------
# chat_with_tools
# ---------------------------------------------------------------------------


class TestChatWithTools:
    def test_returns_content_and_timing(self):
        client = _make_client()
        client._client.post = AsyncMock(
            return_value=_mock_http_response(
                _choices_response(
                    content="Hello", prompt_tokens=20, completion_tokens=8
                )
            )
        )
        result = asyncio.run(
            client.chat_with_tools(
                model="qwen2.5-coder:7b",
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
            )
        )
        assert result["content"] == "Hello"
        assert result["_ollama_timing"]["input_tokens"] == 20
        assert result["_ollama_timing"]["output_tokens"] == 8
        assert result["_ollama_timing"]["eval_ms"] is None
        assert result["_ollama_timing"]["load_ms"] is None

    def test_tool_calls_normalised(self):
        client = _make_client()
        client._client.post = AsyncMock(
            return_value=_mock_http_response(
                _choices_response(
                    tool_calls=[
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {
                                "name": "read_config",
                                "arguments": '{"path": "/config"}',
                            },
                        }
                    ]
                )
            )
        )
        result = asyncio.run(
            client.chat_with_tools(
                model="qwen2.5-coder:7b",
                messages=[{"role": "user", "content": "Check config"}],
                tools=[],
            )
        )
        assert "tool_calls" in result
        tc = result["tool_calls"][0]
        assert tc["id"] == "call_xyz"
        assert tc["function"]["name"] == "read_config"
        # arguments must be a dict, not a string
        assert isinstance(tc["function"]["arguments"], dict)
        assert tc["function"]["arguments"] == {"path": "/config"}

    def test_request_body_has_correct_fields(self):
        client = _make_client()
        captured: list[dict] = []

        async def capture_post(path: str, json: dict) -> MagicMock:  # noqa: A002
            captured.append(json)
            return _mock_http_response(_choices_response())

        client._client.post = capture_post
        tools = [
            {"type": "function", "function": {"name": "read_logs", "parameters": {}}}
        ]
        asyncio.run(
            client.chat_with_tools(
                model="test-model",
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
                options={"temperature": 0.5},
            )
        )
        assert len(captured) == 1
        body = captured[0]
        assert body["model"] == "test-model"
        assert body["temperature"] == 0.5
        assert body["tool_choice"] == "auto"
        assert body["tools"] == tools

    def test_no_tool_calls_no_key(self):
        client = _make_client()
        client._client.post = AsyncMock(
            return_value=_mock_http_response(_choices_response(content="Done."))
        )
        result = asyncio.run(
            client.chat_with_tools(
                model="m", messages=[{"role": "user", "content": "x"}], tools=[]
            )
        )
        assert "tool_calls" not in result


# ---------------------------------------------------------------------------
# chat (structured one-shot)
# ---------------------------------------------------------------------------


class TestChat:
    def test_returns_message_content_shape(self):
        client = _make_client()
        client._client.post = AsyncMock(
            return_value=_mock_http_response(
                {
                    "choices": [{"message": {"content": '{"is_valid": true}'}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                }
            )
        )
        result = asyncio.run(
            client.chat(
                model="m",
                messages=[{"role": "user", "content": "x"}],
                options={"temperature": 0.0},
                format={"type": "object"},
            )
        )
        assert result == {"message": {"content": '{"is_valid": true}'}}

    def test_response_format_json_object_in_request(self):
        client = _make_client()
        captured: list[dict] = []

        async def capture(path: str, json: dict) -> MagicMock:  # noqa: A002
            captured.append(json)
            return _mock_http_response(
                {"choices": [{"message": {"content": "{}"}}], "usage": {}}
            )

        client._client.post = capture
        asyncio.run(
            client.chat(
                model="m",
                messages=[{"role": "user", "content": "x"}],
                options={},
                format={"type": "object"},
            )
        )
        assert captured[0]["response_format"] == {"type": "json_object"}

    def test_schema_injected_into_system_message(self):
        client = _make_client()
        captured: list[dict] = []

        async def capture(path: str, json: dict) -> MagicMock:  # noqa: A002
            captured.append(json)
            return _mock_http_response(
                {"choices": [{"message": {"content": "{}"}}], "usage": {}}
            )

        client._client.post = capture
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        asyncio.run(
            client.chat(
                model="m",
                messages=[
                    {"role": "system", "content": "You are an assistant."},
                    {"role": "user", "content": "Evaluate."},
                ],
                options={},
                format=schema,
            )
        )
        system_content = captured[0]["messages"][0]["content"]
        assert "Respond ONLY with valid JSON matching this schema" in system_content
        assert '"ok"' in system_content

    def test_schema_prepended_when_no_system_message(self):
        client = _make_client()
        captured: list[dict] = []

        async def capture(path: str, json: dict) -> MagicMock:  # noqa: A002
            captured.append(json)
            return _mock_http_response(
                {"choices": [{"message": {"content": "{}"}}], "usage": {}}
            )

        client._client.post = capture
        asyncio.run(
            client.chat(
                model="m",
                messages=[{"role": "user", "content": "Go."}],
                options={},
                format={"type": "object"},
            )
        )
        # A system message should have been prepended.
        assert captured[0]["messages"][0]["role"] == "system"
        assert "schema" in captured[0]["messages"][0]["content"].lower()


# ---------------------------------------------------------------------------
# FakeOpenAICompatClient
# ---------------------------------------------------------------------------


class TestFakeOpenAICompatClient:
    def test_chat_returns_empty_json(self):
        from utils.llm.openai_compat_client import FakeOpenAICompatClient

        fake = FakeOpenAICompatClient()
        result = asyncio.run(fake.chat("m", [], {}, {}))
        assert result == {"message": {"content": "{}"}}

    def test_call_sequence_followed(self):
        from utils.llm.openai_compat_client import FakeOpenAICompatClient

        seq = [
            {"tool_calls": [{"function": {"name": "read_config", "arguments": {}}}]},
            {"content": "done"},
        ]
        fake = FakeOpenAICompatClient(seq)
        r1 = asyncio.run(fake.chat_with_tools("m", [], []))
        assert r1["tool_calls"][0]["function"]["name"] == "read_config"
        r2 = asyncio.run(fake.chat_with_tools("m", [], []))
        assert r2["content"] == "done"

    def test_exhausted_sequence_returns_empty(self):
        from utils.llm.openai_compat_client import FakeOpenAICompatClient

        fake = FakeOpenAICompatClient([])
        result = asyncio.run(fake.chat_with_tools("m", [], []))
        assert result == {"role": "assistant", "content": ""}

    def test_calls_recorded(self):
        from utils.llm.openai_compat_client import FakeOpenAICompatClient

        fake = FakeOpenAICompatClient()
        asyncio.run(fake.chat("m", [{"role": "user", "content": "x"}], {}, {}))
        asyncio.run(fake.chat_with_tools("m", [{"role": "user", "content": "y"}], []))
        assert len(fake.calls) == 2
        assert fake.calls[0]["type"] == "chat"
        assert fake.calls[1]["type"] == "chat_with_tools"
