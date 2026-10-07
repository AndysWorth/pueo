"""Unit tests for llm_factory — make_llm_client, _default_model_for_provider, _provider_name."""

from __future__ import annotations

import importlib
import sys

import pytest


class TestMakeLlmClient:
    def test_local_returns_ollama_client(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(yaml.dump({"llm": {"provider": "local"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])
        import config  # noqa: F401

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import make_llm_client
        from utils.llm.ollama_client import OllamaClient

        client = make_llm_client()
        assert isinstance(client, OllamaClient)

    def test_cloud_returns_claude_api_client(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(yaml.dump({"llm": {"provider": "cloud"}}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import make_llm_client
        from utils.llm.cloud_client import ClaudeAPIClient

        client = make_llm_client()
        assert isinstance(client, ClaudeAPIClient)

    def test_openai_compat_returns_openai_compat_client(
        self, isolated_config, monkeypatch
    ):
        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "llm": {
                        "provider": "openai_compat",
                        "openai_compat": {
                            "base_url": "http://localhost:1234/v1",
                            "model": "qwen2.5-coder:7b",
                        },
                    }
                }
            )
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import make_llm_client
        from utils.llm.openai_compat_client import OpenAICompatClient

        client = make_llm_client()
        assert isinstance(client, OpenAICompatClient)


class TestDefaultModelForProvider:
    def test_local_returns_ollama_model(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(
            yaml.dump(
                {"llm": {"provider": "local"}, "ollama": {"model": "my-ollama-model"}}
            )
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import _default_model_for_provider

        assert _default_model_for_provider() == "my-ollama-model"

    def test_cloud_returns_cloud_model(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(
            yaml.dump(
                {"llm": {"provider": "cloud"}, "cloud": {"model": "claude-opus-5"}}
            )
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import _default_model_for_provider

        assert _default_model_for_provider() == "claude-opus-5"

    def test_openai_compat_returns_openai_compat_model(
        self, isolated_config, monkeypatch
    ):
        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "llm": {
                        "provider": "openai_compat",
                        "openai_compat": {"model": "llama-3.2-3b"},
                    }
                }
            )
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import _default_model_for_provider

        assert _default_model_for_provider() == "llama-3.2-3b"


class TestProviderName:
    def test_local_returns_local(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(yaml.dump({"llm": {"provider": "local"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import _provider_name

        assert _provider_name() == "local"

    def test_cloud_returns_cloud(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(yaml.dump({"llm": {"provider": "cloud"}}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import _provider_name

        assert _provider_name() == "cloud"

    def test_openai_compat_returns_openai_compat(self, isolated_config, monkeypatch):
        import yaml

        isolated_config.write_text(yaml.dump({"llm": {"provider": "openai_compat"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])

        importlib.reload(importlib.import_module("utils.llm.llm_factory"))
        from utils.llm.llm_factory import _provider_name

        assert _provider_name() == "openai_compat"
