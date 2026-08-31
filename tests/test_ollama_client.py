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
