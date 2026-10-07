"""LLM client factory — item 73.

Single decision point that reads LLM_PROVIDER from config and returns the
appropriate client.  All production call-sites use make_llm_client() instead
of constructing OllamaClient() or ClaudeAPIClient() directly.

Provider semantics:
  "local"         (default) — OllamaClient; no WAN for inference
  "cloud"                   — ClaudeAPIClient; all inference goes to Anthropic
  "both"                    — OllamaClient for autonomous cycles; ClaudeAPIClient is
                              instantiated explicitly for approved escalation (item 76)
  "openai_compat"           — OpenAICompatClient; any OpenAI-compatible server
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import config
from utils.llm.ollama_client import OllamaClient

if TYPE_CHECKING:
    from interfaces import LLMClientProtocol


def make_llm_client() -> "LLMClientProtocol":
    """Return the configured LLM client.

    Imports ClaudeAPIClient and OpenAICompatClient lazily so their dependencies
    are never loaded when LLM_PROVIDER is "local" or "both".
    """
    if config.LLM_PROVIDER == "cloud":
        from utils.llm.cloud_client import ClaudeAPIClient

        return ClaudeAPIClient()
    if config.LLM_PROVIDER == "openai_compat":
        from utils.llm.openai_compat_client import OpenAICompatClient

        return OpenAICompatClient()
    return OllamaClient()


def _default_model_for_provider() -> str:
    """Return the default model name for the configured provider."""
    if config.LLM_PROVIDER == "cloud":
        return config.CLOUD_MODEL
    if config.LLM_PROVIDER == "openai_compat":
        return config.OPENAI_COMPAT_MODEL
    return config.OLLAMA_MODEL


def _provider_name() -> str:
    """Return a short provider identifier for latency tracking."""
    if config.LLM_PROVIDER == "cloud":
        return "cloud"
    if config.LLM_PROVIDER == "openai_compat":
        return "openai_compat"
    return "local"
