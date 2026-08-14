"""Tests for ToolExecutor — enrichment path and _apply_fix payload shape."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_VALID_YAML = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
_PROPOSED_YAML = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"


class _CapturingFakeLLMClient:
    """Returns caller-supplied JSON; records all calls."""

    def __init__(self, response_json: str) -> None:
        self._response = response_json
        self.calls: list[list[dict]] = []

    async def chat(
        self, model: str, messages: list[dict], options: dict, format: dict
    ) -> dict:
        self.calls.append(list(messages))
        return {"message": {"content": self._response}}

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        options: dict | None = None,
    ) -> dict:
        return {"role": "assistant", "content": ""}


def _make_enrichment_json() -> str:
    from utils.tool_registry import FixEnrichment

    return FixEnrichment(
        relevant_config_section="http:\n  server_port: 8123",
        explanation="The server_port value is wrong; 8124 is the correct value.",
        confidence="high",
        suggested_fix_summary="Change server_port from 8123 to 8124.",
    ).model_dump_json()


def _make_executor(*, llm_client=None, notifier=None):
    from utils.autonomy import FakeAutonomyGate
    from utils.notify import FakeNotifier
    from utils.ssh_client import FakeSSHClient
    from utils.tool_executor import ToolExecutor

    ssh = FakeSSHClient(
        file_contents={"/config/configuration.yaml": _VALID_YAML},
        command_results={
            "ha backup new": (0, "Slug: abc123\n", ""),
            "ha core check": (0, "", ""),
        },
    )
    gate = FakeAutonomyGate(auto_execute_result=False)
    return ToolExecutor(
        ha_ssh_client=ssh,
        gate=gate,
        notifier=notifier or FakeNotifier(),
        llm_client=llm_client,
    )


class TestEnrichFixContext:
    def test_returns_enrichment_when_llm_client_provided(self):
        notifier_obj = None
        from utils.notify import FakeNotifier

        notifier_obj = FakeNotifier()
        llm = _CapturingFakeLLMClient(_make_enrichment_json())
        executor = _make_executor(llm_client=llm, notifier=notifier_obj)

        result = asyncio.run(
            executor._enrich_fix_context(_VALID_YAML, _PROPOSED_YAML, "Fix port")
        )
        from utils.tool_registry import FixEnrichment

        assert isinstance(result, FixEnrichment)
        assert result.confidence == "high"
        assert "8124" in result.explanation
        assert len(llm.calls) == 1

    def test_returns_none_when_no_llm_client(self):
        executor = _make_executor(llm_client=None)
        result = asyncio.run(
            executor._enrich_fix_context(_VALID_YAML, _PROPOSED_YAML, "Fix port")
        )
        assert result is None

    def test_returns_none_on_parse_failure(self):
        llm = _CapturingFakeLLMClient("not valid json {{{")
        executor = _make_executor(llm_client=llm)
        result = asyncio.run(
            executor._enrich_fix_context(_VALID_YAML, _PROPOSED_YAML, "Fix port")
        )
        assert result is None


class TestApplyFixPayload:
    def _run_apply_fix(self, llm_client):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier(approve=False)
        executor = _make_executor(llm_client=llm_client, notifier=notifier)

        with patch("utils.yaml_validator.validate_proposed_fix") as mock_val:
            mock_val.return_value = type("R", (), {"is_safe": True, "reasons": []})()
            result = asyncio.run(executor._apply_fix(_PROPOSED_YAML, "Fix port"))

        return result, notifier

    def test_enrichment_in_payload_when_llm_provided(self):
        llm = _CapturingFakeLLMClient(_make_enrichment_json())
        result, notifier = self._run_apply_fix(llm_client=llm)

        assert result.awaiting_approval is True
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["enrichment"] is not None
        assert payload["enrichment"]["confidence"] == "high"
        assert payload["enrichment"]["explanation"] != ""

    def test_enrichment_none_when_no_llm_client(self):
        result, notifier = self._run_apply_fix(llm_client=None)

        assert result.awaiting_approval is True
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["enrichment"] is None

    def test_body_contains_explanation_when_enriched(self):
        llm = _CapturingFakeLLMClient(_make_enrichment_json())
        _, notifier = self._run_apply_fix(llm_client=llm)

        body = notifier.sent[0]["body"]
        assert "8124" in body

    def test_body_falls_back_to_yaml_preview_without_llm(self):
        _, notifier = self._run_apply_fix(llm_client=None)

        body = notifier.sent[0]["body"]
        assert "Proposed fix:" in body
