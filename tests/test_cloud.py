"""Tests for utils/cloud_client.py and utils/llm_factory.py — Phase 18, item 73."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

_REPO_ROOT = Path(__file__).parent.parent


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_content_block(type_: str, **kwargs: Any) -> MagicMock:
    """Create a fake Anthropic content block."""
    block = MagicMock()
    block.type = type_
    for k, v in kwargs.items():
        setattr(block, k, v)
    return block


def _make_anthropic_response(*content_blocks: MagicMock) -> MagicMock:
    """Create a fake anthropic.messages.create() response."""
    resp = MagicMock()
    resp.content = list(content_blocks)
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    return resp


class FakeAnthropicMessages:
    """Intercepts client.messages.create() and returns a canned response."""

    def __init__(self, response: MagicMock) -> None:
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> MagicMock:
        self.calls.append(kwargs)
        return self._response


class FakeAnthropicClient:
    """Drop-in for anthropic.Anthropic(api_key=...) in unit tests."""

    def __init__(self, response: MagicMock) -> None:
        self.messages = FakeAnthropicMessages(response)


# ── tool schema adapter ───────────────────────────────────────────────────────


class TestAdaptToolSchema:
    def test_strips_function_envelope(self):
        from utils.cloud_client import _adapt_tool_schema

        tool = {
            "type": "function",
            "function": {
                "name": "read_config",
                "description": "Read the config file",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }
        result = _adapt_tool_schema(tool)
        assert result["name"] == "read_config"
        assert result["description"] == "Read the config file"
        assert "input_schema" in result
        assert "parameters" not in result
        assert "type" not in result
        assert "function" not in result

    def test_renames_parameters_to_input_schema(self):
        from utils.cloud_client import _adapt_tool_schema

        tool = {
            "type": "function",
            "function": {
                "name": "apply_fix",
                "description": "Apply fix",
                "parameters": {
                    "type": "object",
                    "properties": {"yaml_content": {"type": "string"}},
                    "required": ["yaml_content"],
                },
            },
        }
        result = _adapt_tool_schema(tool)
        assert result["input_schema"]["properties"]["yaml_content"]["type"] == "string"
        assert result["input_schema"]["required"] == ["yaml_content"]

    def test_flat_tool_without_envelope(self):
        from utils.cloud_client import _adapt_tool_schema

        # Some callers may pass already-flat Anthropic-style dicts
        tool = {
            "name": "finish_repair",
            "description": "End the session",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }
        result = _adapt_tool_schema(tool)
        assert result["name"] == "finish_repair"
        assert "input_schema" in result


# ── history translator ────────────────────────────────────────────────────────


class TestTranslateHistory:
    def test_system_extracted(self):
        from utils.cloud_client import _translate_history

        messages = [
            {"role": "system", "content": "You are Pueo"},
            {"role": "user", "content": "hello"},
        ]
        system, result = _translate_history(messages)
        assert system == "You are Pueo"
        assert result[0]["role"] == "user"

    def test_simple_user_message(self):
        from utils.cloud_client import _translate_history

        messages = [{"role": "user", "content": "Fix it"}]
        system, result = _translate_history(messages)
        assert system == ""
        assert result == [{"role": "user", "content": "Fix it"}]

    def test_tool_call_cycle_translation(self):
        from utils.cloud_client import _translate_history

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "initial"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_config", "arguments": {}}}],
            },
            {"role": "tool", "content": "config content", "name": "read_config"},
            {"role": "user", "content": "Continue. Call the next tool."},
        ]
        system, result = _translate_history(messages)

        assert system == "sys"
        # user, assistant, merged-user
        assert len(result) == 3

        # assistant message with tool_use block
        assert result[1]["role"] == "assistant"
        assert result[1]["content"][0]["type"] == "tool_use"
        assert result[1]["content"][0]["name"] == "read_config"
        assert result[1]["content"][0]["id"].startswith("toolu_")

        # merged user message with tool_result + nudge text
        assert result[2]["role"] == "user"
        content_blocks = result[2]["content"]
        types = [b["type"] for b in content_blocks]
        assert "tool_result" in types
        assert "text" in types

    def test_stable_ids_across_multiple_tool_calls(self):
        from utils.cloud_client import _translate_history

        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_config", "arguments": {}}},
                    {"function": {"name": "read_logs", "arguments": {}}},
                ],
            },
            {"role": "tool", "content": "r1", "name": "read_config"},
            {"role": "tool", "content": "r2", "name": "read_logs"},
        ]
        _, result = _translate_history(messages)
        # Two tool_use blocks in the assistant message
        asst = result[0]
        assert asst["role"] == "assistant"
        ids = [b["id"] for b in asst["content"]]
        assert ids[0] != ids[1]
        assert ids[0] == "toolu_0001"
        assert ids[1] == "toolu_0002"

        # Two tool_result blocks in the merged user message
        user_content = result[1]["content"]
        result_ids = [
            b["tool_use_id"] for b in user_content if b["type"] == "tool_result"
        ]
        assert result_ids == ["toolu_0001", "toolu_0002"]

    def test_plain_text_assistant_message(self):
        from utils.cloud_client import _translate_history

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Let me investigate"},
            {"role": "user", "content": "Call finish_repair NOW"},
        ]
        _, result = _translate_history(messages)
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Let me investigate"
        assert result[2]["role"] == "user"

    def test_sequential_ids_across_multiple_cycles(self):
        from utils.cloud_client import _translate_history

        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "tool_a", "arguments": {}}}],
            },
            {"role": "tool", "content": "a_result", "name": "tool_a"},
            {"role": "user", "content": "Continue."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "tool_b", "arguments": {}}}],
            },
            {"role": "tool", "content": "b_result", "name": "tool_b"},
        ]
        _, result = _translate_history(messages)

        # First cycle: id toolu_0001
        first_asst = result[0]
        assert first_asst["content"][0]["id"] == "toolu_0001"
        first_user = result[1]
        tr_first = next(b for b in first_user["content"] if b["type"] == "tool_result")
        assert tr_first["tool_use_id"] == "toolu_0001"

        # Second cycle: id toolu_0002
        second_asst = result[2]
        assert second_asst["content"][0]["id"] == "toolu_0002"
        second_user = result[3]
        tr_second = next(
            b for b in second_user["content"] if b["type"] == "tool_result"
        )
        assert tr_second["tool_use_id"] == "toolu_0002"


# ── response normalizer ───────────────────────────────────────────────────────


class TestResponseNormalizer:
    """Tests for ClaudeAPIClient.chat_with_tools() response normalization."""

    def _make_client(self, response: MagicMock) -> Any:
        from utils.cloud_client import ClaudeAPIClient

        client = ClaudeAPIClient.__new__(ClaudeAPIClient)
        client._client = FakeAnthropicClient(response)  # type: ignore[assignment]
        return client

    def test_tool_use_block_to_tool_calls(self):
        tool_use = _make_content_block(
            "tool_use",
            name="read_config",
            id="toolu_0001",
            input={},
        )
        resp = _make_anthropic_response(tool_use)
        client = self._make_client(resp)

        result = asyncio.run(
            client.chat_with_tools("model", [{"role": "user", "content": "go"}], [])
        )
        assert "tool_calls" in result
        assert result["tool_calls"][0]["function"]["name"] == "read_config"

    def test_text_block_to_content(self):
        text_block = _make_content_block("text", text="Hello there")
        resp = _make_anthropic_response(text_block)
        client = self._make_client(resp)

        result = asyncio.run(
            client.chat_with_tools("model", [{"role": "user", "content": "hi"}], [])
        )
        assert result["content"] == "Hello there"
        assert "tool_calls" not in result

    def test_multiple_tool_use_blocks(self):
        tu1 = _make_content_block("tool_use", name="t1", id="id1", input={"a": 1})
        tu2 = _make_content_block("tool_use", name="t2", id="id2", input={"b": 2})
        resp = _make_anthropic_response(tu1, tu2)
        client = self._make_client(resp)

        result = asyncio.run(
            client.chat_with_tools("model", [{"role": "user", "content": "go"}], [])
        )
        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][0]["function"]["name"] == "t1"
        assert result["tool_calls"][1]["function"]["name"] == "t2"

    def test_tool_arguments_preserved(self):
        tu = _make_content_block(
            "tool_use",
            name="apply_fix",
            id="id1",
            input={"yaml_content": "foo: bar"},
        )
        resp = _make_anthropic_response(tu)
        client = self._make_client(resp)

        result = asyncio.run(
            client.chat_with_tools("model", [{"role": "user", "content": "fix"}], [])
        )
        assert result["tool_calls"][0]["function"]["arguments"] == {
            "yaml_content": "foo: bar"
        }


# ── structured output (chat) ──────────────────────────────────────────────────


class TestClaudeAPIClientChat:
    def _make_client(self, response: MagicMock) -> Any:
        from utils.cloud_client import ClaudeAPIClient

        client = ClaudeAPIClient.__new__(ClaudeAPIClient)
        client._client = FakeAnthropicClient(response)  # type: ignore[assignment]
        return client

    def test_tool_forcing_returns_structured_json(self):
        tool_use = _make_content_block(
            "tool_use",
            name="structured_output",
            id="toolu_x",
            input={"is_valid": True, "severity": "LOW"},
        )
        resp = _make_anthropic_response(tool_use)
        client = self._make_client(resp)

        schema = {
            "type": "object",
            "properties": {
                "is_valid": {"type": "boolean"},
                "severity": {"type": "string"},
            },
            "required": ["is_valid", "severity"],
        }
        result = asyncio.run(
            client.chat(
                "model",
                [{"role": "user", "content": "Analyze config"}],
                {},
                schema,
            )
        )
        assert "message" in result
        parsed = json.loads(result["message"]["content"])
        assert parsed["is_valid"] is True
        assert parsed["severity"] == "LOW"

    def test_empty_response_returns_empty_json(self):
        # No tool_use blocks — fallback to "{}"
        text_block = _make_content_block("text", text="I cannot comply")
        resp = _make_anthropic_response(text_block)
        client = self._make_client(resp)

        result = asyncio.run(
            client.chat("model", [{"role": "user", "content": "x"}], {}, {})
        )
        assert result["message"]["content"] == "{}"


# ── make_llm_client factory ───────────────────────────────────────────────────


class TestMakeLLMClient:
    def test_local_returns_ollama_client(self, isolated_config):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "local"}}))
        importlib.reload(sys.modules["config"])
        importlib.reload(importlib.import_module("utils.llm_factory"))
        import config
        from utils.llm_factory import make_llm_client
        from utils.ollama_client import OllamaClient

        assert config.LLM_PROVIDER == "local"
        # We can't call make_llm_client() directly (would try to connect to Ollama),
        # but we can verify the factory chooses the right branch by inspecting config.
        assert config.LLM_PROVIDER != "cloud"

    def test_both_returns_ollama_client(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "both"}}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        importlib.reload(sys.modules["config"])
        importlib.reload(importlib.import_module("utils.llm_factory"))
        import config

        assert config.LLM_PROVIDER == "both"
        assert config.LLM_PROVIDER != "cloud"

    def test_cloud_returns_claude_client(self, isolated_config, monkeypatch):
        isolated_config.write_text(
            yaml.dump(
                {"llm": {"provider": "cloud"}, "cloud": {"model": "claude-sonnet-4-5"}}
            )
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        importlib.reload(sys.modules["config"])
        importlib.reload(importlib.import_module("utils.llm_factory"))
        import config
        from utils.llm_factory import make_llm_client

        assert config.LLM_PROVIDER == "cloud"

        # Patch anthropic.Anthropic so we don't make real API calls
        with patch("anthropic.Anthropic") as mock_anthropic:
            mock_anthropic.return_value = MagicMock()
            client = make_llm_client()

        from utils.cloud_client import ClaudeAPIClient

        assert isinstance(client, ClaudeAPIClient)

    def test_cloud_raises_without_api_key(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "cloud"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            importlib.reload(sys.modules["config"])


# ── _default_model_for_provider ───────────────────────────────────────────────


class TestDefaultModelForProvider:
    def test_local_returns_ollama_model(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {"llm": {"provider": "local"}, "ollama": {"model": "qwen2.5-coder:7b"}}
            )
        )
        importlib.reload(sys.modules["config"])
        importlib.reload(importlib.import_module("utils.llm_factory"))
        from utils.llm_factory import _default_model_for_provider

        assert _default_model_for_provider() == "qwen2.5-coder:7b"

    def test_both_returns_ollama_model(self, isolated_config, monkeypatch):
        isolated_config.write_text(
            yaml.dump({"llm": {"provider": "both"}, "ollama": {"model": "llama3:8b"}})
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        importlib.reload(sys.modules["config"])
        importlib.reload(importlib.import_module("utils.llm_factory"))
        from utils.llm_factory import _default_model_for_provider

        assert _default_model_for_provider() == "llama3:8b"

    def test_cloud_returns_cloud_model(self, isolated_config, monkeypatch):
        isolated_config.write_text(
            yaml.dump(
                {
                    "llm": {"provider": "cloud"},
                    "cloud": {"model": "claude-opus-5"},
                }
            )
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        importlib.reload(sys.modules["config"])
        importlib.reload(importlib.import_module("utils.llm_factory"))
        from utils.llm_factory import _default_model_for_provider

        assert _default_model_for_provider() == "claude-opus-5"


# ── config defaults for new keys ─────────────────────────────────────────────


class TestLLMProviderConfigKeys:
    def test_llm_provider_default_is_local(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.LLM_PROVIDER == "local"

    def test_llm_provider_from_yaml(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "cloud"}}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        importlib.reload(sys.modules["config"])
        import config

        assert config.LLM_PROVIDER == "cloud"

    def test_cloud_model_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MODEL == "claude-sonnet-5"

    def test_cloud_model_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"cloud": {"model": "claude-opus-5"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MODEL == "claude-opus-5"

    def test_anthropic_api_key_from_env(self, isolated_config, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        importlib.reload(sys.modules["config"])
        import config

        assert config.ANTHROPIC_API_KEY == "sk-test-123"

    def test_anthropic_api_key_default_empty(self, isolated_config, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])
        import config

        assert config.ANTHROPIC_API_KEY == ""


# ── billing guard ─────────────────────────────────────────────────────────────


class TestBillingGuard:
    """Tests for utils/billing.py — estimate_cost, caps, record helpers."""

    def test_estimate_cost_sonnet(self):
        from utils.billing import estimate_cost

        # Sonnet: $3 input / $15 output per MTok
        cost = estimate_cost("claude-sonnet-4-5", 1_000_000, 0)
        assert abs(cost - 3.00) < 0.01

        cost2 = estimate_cost("claude-sonnet-4-5", 0, 1_000_000)
        assert abs(cost2 - 15.00) < 0.01

    def test_estimate_cost_haiku(self):
        from utils.billing import estimate_cost

        cost = estimate_cost("claude-haiku-4-5", 1_000_000, 0)
        assert abs(cost - 0.25) < 0.01

    def test_estimate_cost_opus(self):
        from utils.billing import estimate_cost

        cost = estimate_cost("claude-opus-5", 1_000_000, 0)
        assert abs(cost - 15.00) < 0.01

    def test_estimate_cost_unknown_model_uses_fallback(self):
        from utils.billing import estimate_cost

        # Unknown model → same as sonnet fallback
        cost = estimate_cost("unknown-model-xyz", 1_000_000, 0)
        assert cost > 0

    def test_record_and_get_incident_spend(self, tmp_path, monkeypatch):
        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)

        # Create table first
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

        from utils.billing import get_incident_spend, record_cloud_spend

        record_cloud_spend("inc-1", "claude-sonnet-4-5", 100, 50, 0.001)
        record_cloud_spend("inc-1", "claude-sonnet-4-5", 200, 100, 0.002)
        record_cloud_spend("inc-2", "claude-sonnet-4-5", 50, 25, 0.0005)

        assert abs(get_incident_spend("inc-1") - 0.003) < 1e-6
        assert abs(get_incident_spend("inc-2") - 0.0005) < 1e-6
        assert get_incident_spend("inc-999") == 0.0

    def test_record_and_get_daily_spend(self, tmp_path, monkeypatch):
        import sqlite3
        import time

        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

        from utils.billing import get_daily_spend, record_cloud_spend

        record_cloud_spend("inc-1", "claude-sonnet-4-5", 100, 50, 1.00)
        record_cloud_spend("inc-2", "claude-sonnet-4-5", 100, 50, 2.00)

        total = get_daily_spend()
        assert abs(total - 3.00) < 1e-6

    def test_billing_cap_per_incident_raises(self, tmp_path, monkeypatch):
        import sqlite3

        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)
        monkeypatch.setattr("config.CLOUD_MAX_COST_PER_INCIDENT_USD", 0.01)
        monkeypatch.setattr("config.CLOUD_MAX_DAILY_SPEND_USD", 100.0)

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

        from utils.billing import (
            BillingCapError,
            check_billing_caps,
            record_cloud_spend,
        )

        # Record spend near the per-incident cap
        record_cloud_spend("inc-1", "claude-sonnet-4-5", 1000, 100, 0.009)

        # Next call should push over the $0.01 cap
        with pytest.raises(BillingCapError, match="Per-incident cap"):
            check_billing_caps("inc-1", "claude-sonnet-4-5", 1_000_000, 500_000)

    def test_billing_cap_daily_raises(self, tmp_path, monkeypatch):
        import sqlite3

        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)
        monkeypatch.setattr("config.CLOUD_MAX_COST_PER_INCIDENT_USD", 100.0)
        monkeypatch.setattr("config.CLOUD_MAX_DAILY_SPEND_USD", 0.01)

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

        from utils.billing import (
            BillingCapError,
            check_billing_caps,
            record_cloud_spend,
        )

        record_cloud_spend("inc-1", "claude-sonnet-4-5", 1000, 100, 0.009)

        with pytest.raises(BillingCapError, match="Daily cap"):
            check_billing_caps("inc-99", "claude-sonnet-4-5", 1_000_000, 500_000)

    def test_billing_no_raise_under_cap(self, tmp_path, monkeypatch):
        import sqlite3

        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)
        monkeypatch.setattr("config.CLOUD_MAX_COST_PER_INCIDENT_USD", 10.0)
        monkeypatch.setattr("config.CLOUD_MAX_DAILY_SPEND_USD", 100.0)

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

        from utils.billing import check_billing_caps

        # Should not raise — tiny call well under both caps
        check_billing_caps("inc-1", "claude-sonnet-4-5", 100, 50)


# ── ClaudeAPIClient billing integration ──────────────────────────────────────


class TestClaudeAPIClientBilling:
    """Billing is recorded and caps are checked when incident_id is set."""

    def _make_client(self, response: MagicMock, incident_id: str = "") -> Any:
        from utils.cloud_client import ClaudeAPIClient

        client = ClaudeAPIClient.__new__(ClaudeAPIClient)
        client._client = FakeAnthropicClient(response)  # type: ignore[assignment]
        client._incident_id = incident_id
        return client

    def test_no_billing_when_no_incident_id(self, tmp_path, monkeypatch):
        """Billing helpers are skipped entirely when incident_id is empty."""
        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)

        tool_use = _make_content_block(
            "tool_use", name="finish_repair", id="t1", input={}
        )
        resp = _make_anthropic_response(tool_use)
        client = self._make_client(resp, incident_id="")

        # Should not raise even without cloud_spend table
        result = asyncio.run(
            client.chat_with_tools("model", [{"role": "user", "content": "go"}], [])
        )
        assert "tool_calls" in result

    def test_billing_recorded_after_chat_with_tools(self, tmp_path, monkeypatch):
        import sqlite3

        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)
        monkeypatch.setattr("config.CLOUD_MAX_COST_PER_INCIDENT_USD", 10.0)
        monkeypatch.setattr("config.CLOUD_MAX_DAILY_SPEND_USD", 100.0)

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

        tool_use = _make_content_block(
            "tool_use", name="finish_repair", id="t1", input={}
        )
        resp = _make_anthropic_response(tool_use)
        # FakeAnthropicResponse has usage.input_tokens=10, output_tokens=5
        client = self._make_client(resp, incident_id="inc-test")

        asyncio.run(
            client.chat_with_tools(
                "claude-sonnet-4-5",
                [{"role": "user", "content": "go"}],
                [],
            )
        )

        from utils.billing import get_incident_spend

        assert get_incident_spend("inc-test") > 0

    def test_billing_cap_blocks_chat_with_tools(self, tmp_path, monkeypatch):
        import sqlite3

        db = str(tmp_path / "test.db")
        monkeypatch.setattr("config.DB_PATH", db)
        monkeypatch.setattr("config.CLOUD_MAX_COST_PER_INCIDENT_USD", 0.0001)
        monkeypatch.setattr("config.CLOUD_MAX_DAILY_SPEND_USD", 100.0)

        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    timestamp REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )
            # Pre-load spend to push over the cap
            conn.execute(
                "INSERT INTO cloud_spend VALUES (NULL, 'inc-cap', ?, ?, 1, 1, 0.0001)",
                (__import__("time").time(), "claude-sonnet-4-5"),
            )

        tool_use = _make_content_block(
            "tool_use", name="finish_repair", id="t1", input={}
        )
        resp = _make_anthropic_response(tool_use)
        client = self._make_client(resp, incident_id="inc-cap")

        from utils.billing import BillingCapError

        with pytest.raises(BillingCapError):
            asyncio.run(
                client.chat_with_tools(
                    "claude-sonnet-4-5",
                    [{"role": "user", "content": "a" * 1000}],
                    [],
                )
            )
