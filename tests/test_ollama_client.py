"""Tests for OllamaClient debug logging — no real Ollama connection required."""

import asyncio
import logging
import unittest.mock

import pytest


class TestOllamaClientChatLogging:
    """OllamaClient.chat() emits llm_request/llm_response DEBUG events."""

    def _make_client(self, monkeypatch):
        """Return an OllamaClient with the underlying ollama.Client stubbed out."""
        import ollama

        monkeypatch.setattr(ollama, "Client", lambda host: unittest.mock.MagicMock())
        # Re-import after patching so __init__ picks up the mock
        import importlib
        import utils.llm.ollama_client as mod

        importlib.reload(mod)
        client = mod.OllamaClient()
        fake_resp = unittest.mock.MagicMock()
        fake_resp.message.content = '{"field": "val"}'
        client._client.chat.return_value = fake_resp
        return client

    def test_chat_emits_llm_request_and_response(self, monkeypatch, caplog):
        """chat() emits both llm_request and llm_response at DEBUG level."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat(
                    model="test-model",
                    messages=[{"role": "user", "content": "hi"}],
                    options={},
                    format={},
                )
            )

        events = [r.msg for r in caplog.records]
        assert "llm_request" in events
        assert "llm_response" in events

    def test_chat_llm_request_has_call_type_chat(self, monkeypatch, caplog):
        """llm_request from chat() carries call_type='chat'."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat(
                    model="test-model",
                    messages=[{"role": "user", "content": "hi"}],
                    options={},
                    format={},
                )
            )

        req = next(r for r in caplog.records if r.msg == "llm_request")
        assert req.call_type == "chat"  # type: ignore[attr-defined]

    def test_chat_llm_request_has_messages_summary(self, monkeypatch, caplog):
        """llm_request includes messages_summary with role and preview per message."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat(
                    model="test-model",
                    messages=[{"role": "user", "content": "hello world"}],
                    options={},
                    format={},
                )
            )

        req = next(r for r in caplog.records if r.msg == "llm_request")
        summary = req.messages_summary  # type: ignore[attr-defined]
        assert summary == [{"role": "user", "preview": "hello world"}]

    def test_chat_llm_response_has_content_preview_and_duration(
        self, monkeypatch, caplog
    ):
        """llm_response includes content_preview and a non-negative duration_ms."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat(
                    model="test-model",
                    messages=[{"role": "user", "content": "hi"}],
                    options={},
                    format={},
                )
            )

        resp = next(r for r in caplog.records if r.msg == "llm_response")
        assert resp.call_type == "chat"  # type: ignore[attr-defined]
        assert hasattr(resp, "content_preview")
        assert resp.duration_ms >= 0  # type: ignore[attr-defined]

    def test_chat_emits_llm_request_full_at_debug(self, monkeypatch, caplog):
        """chat() emits llm_request_full with full messages at DEBUG level."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)
        msgs = [{"role": "user", "content": "full content here"}]

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(client.chat(model="m", messages=msgs, options={}, format={}))

        req_full = next(r for r in caplog.records if r.msg == "llm_request_full")
        assert req_full.messages == msgs  # type: ignore[attr-defined]

    def test_chat_emits_llm_response_full_at_debug(self, monkeypatch, caplog):
        """chat() emits llm_response_full with untruncated content at DEBUG level."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)
        long_content = "x" * 1000
        client._client.chat.return_value.message.content = long_content

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat(
                    model="m",
                    messages=[{"role": "user", "content": "hi"}],
                    options={},
                    format={},
                )
            )

        resp_full = next(r for r in caplog.records if r.msg == "llm_response_full")
        assert resp_full.content == long_content  # type: ignore[attr-defined]
        assert len(resp_full.content) == 1000  # type: ignore[attr-defined]

    def test_chat_no_full_events_at_info(self, monkeypatch, caplog):
        """chat() does NOT emit llm_request_full or llm_response_full at INFO level."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)

        with caplog.at_level(logging.INFO, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat(
                    model="m",
                    messages=[{"role": "user", "content": "hi"}],
                    options={},
                    format={},
                )
            )

        events = [r.msg for r in caplog.records]
        assert "llm_request_full" not in events
        assert "llm_response_full" not in events


class TestOllamaClientChatWithToolsLogging:
    """OllamaClient.chat_with_tools() full-payload DEBUG events."""

    def _make_client(self, monkeypatch):
        import ollama

        monkeypatch.setattr(ollama, "Client", lambda host: unittest.mock.MagicMock())
        import importlib
        import utils.llm.ollama_client as mod

        importlib.reload(mod)
        client = mod.OllamaClient()
        fake_resp = unittest.mock.MagicMock()
        fake_resp.message.content = "text response"
        fake_tc = unittest.mock.MagicMock()
        fake_tc.function.name = "run_ha_command"
        fake_tc.function.arguments = {"command": "df -h"}
        fake_resp.message.tool_calls = [fake_tc]
        fake_resp.eval_duration = None
        fake_resp.load_duration = None
        client._client.chat.return_value = fake_resp
        return client

    def test_chat_with_tools_emits_request_full(self, monkeypatch, caplog):
        """chat_with_tools() emits llm_request_full with tool names list."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)
        msgs = [{"role": "user", "content": "check disk"}]
        tools = [{"function": {"name": "run_ha_command", "parameters": {}}}]

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(client.chat_with_tools(model="m", messages=msgs, tools=tools))

        req_full = next(r for r in caplog.records if r.msg == "llm_request_full")
        assert req_full.messages == msgs  # type: ignore[attr-defined]
        assert "run_ha_command" in req_full.tools  # type: ignore[attr-defined]

    def test_chat_with_tools_emits_response_full_with_tool_args(
        self, monkeypatch, caplog
    ):
        """chat_with_tools() emits llm_response_full with full tool call arguments."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)
        msgs = [{"role": "user", "content": "check disk"}]
        tools = [{"function": {"name": "run_ha_command", "parameters": {}}}]

        with caplog.at_level(logging.DEBUG, logger="pueo.llm.ollama"):
            asyncio.run(client.chat_with_tools(model="m", messages=msgs, tools=tools))

        resp_full = next(r for r in caplog.records if r.msg == "llm_response_full")
        tc_list = resp_full.tool_calls  # type: ignore[attr-defined]
        assert len(tc_list) == 1
        assert tc_list[0]["name"] == "run_ha_command"
        assert tc_list[0]["arguments"] == {"command": "df -h"}

    def test_chat_with_tools_no_full_events_at_info(self, monkeypatch, caplog):
        """chat_with_tools() does NOT emit full events at INFO level."""
        client = self._make_client(monkeypatch)
        monkeypatch.setattr(logging.getLogger("pueo"), "propagate", True)

        with caplog.at_level(logging.INFO, logger="pueo.llm.ollama"):
            asyncio.run(
                client.chat_with_tools(
                    model="m",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                )
            )

        events = [r.msg for r in caplog.records]
        assert "llm_request_full" not in events
        assert "llm_response_full" not in events
