"""LLM client factory — item 73.

Single decision point that reads LLM_PROVIDER from config and returns the
appropriate client.  All production call-sites use make_llm_client() instead
of constructing OllamaClient() or ClaudeAPIClient() directly.

Provider semantics:
  "local"  (default) — OllamaClient; no WAN for inference
  "cloud"            — ClaudeAPIClient; all inference goes to Anthropic
  "both"             — OllamaClient for autonomous cycles; ClaudeAPIClient is
                       instantiated explicitly for HITL escalation (item 76)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import config
from utils.ollama_client import OllamaClient

if TYPE_CHECKING:
    from interfaces import LLMClientProtocol


def make_llm_client() -> "LLMClientProtocol":
    """Return the configured LLM client.

    Imports ClaudeAPIClient lazily so the anthropic SDK is never loaded when
    LLM_PROVIDER is "local" or "both".
    """
    if config.LLM_PROVIDER == "cloud":
        from utils.cloud_client import ClaudeAPIClient

        return ClaudeAPIClient()
    return OllamaClient()


def _default_model_for_provider() -> str:
    """Return the default model name for the configured provider."""
    if config.LLM_PROVIDER == "cloud":
        return config.CLOUD_MODEL
    return config.OLLAMA_MODEL
