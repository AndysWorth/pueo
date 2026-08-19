"""Tests for ToolExecutor — enrichment path and _apply_fix payload shape."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_VALID_YAML = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
_PROPOSED_YAML = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"


class _FakeSyncResp:
    """Minimal urllib response mock for urlopen patches."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


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


class TestFetchHaDocs:
    """Tests for ToolExecutor._fetch_ha_docs."""

    def _run(
        self,
        domain: str,
        filename: str,
        *,
        provider: str = "local",
        cache: dict | None = None,
        tmp_path=None,
    ):
        """Run _fetch_ha_docs with a temp cache dir and optional pre-seeded files."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "ha_source"
            if cache:
                for rel, content in cache.items():
                    dest = cache_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content)

            with patch(
                "utils.tool_executor._config_mod.HA_SOURCE_CACHE_DIR", str(cache_dir)
            ), patch("utils.tool_executor._config_mod.LLM_PROVIDER", provider):
                executor = _make_executor()
                return asyncio.run(executor._fetch_ha_docs(domain, filename))

    def test_cache_hit_returns_content_no_http(self):
        result = self._run(
            "zha",
            "manifest.json",
            cache={"zha/manifest.json": '{"domain": "zha"}'},
        )
        assert result.success is True
        assert '"domain": "zha"' in result.output

    def test_local_mode_cache_miss_raises_tool_error(self):
        result = self._run("zha", "manifest.json", provider="local")
        assert result.success is False
        assert "LLM_PROVIDER=local" in result.error

    def test_cloud_mode_live_fetch(self):
        fake_content = b'{"domain": "zha", "name": "Zigbee Home Automation"}'

        with patch("urllib.request.urlopen", return_value=_FakeSyncResp(fake_content)):
            result = self._run("zha", "manifest.json", provider="cloud")
        assert result.success is True
        assert "Zigbee" in result.output

    def test_sensor_py_accessible(self):
        # Previously blocked by allowlist — sensor.py should now be served from cache.
        result = self._run(
            "noaa_tides",
            "sensor.py",
            cache={"noaa_tides/sensor.py": "class NOAATidesSensor: pass"},
        )
        assert result.success is True
        assert "NOAATidesSensor" in result.output

    def test_path_traversal_rejected(self):
        result = self._run("../../../etc", "passwd")
        assert result.success is False
        assert "traversal" in result.error.lower()

    def test_large_response_truncated(self):
        big_content = ("x" * 20_000).encode()
        with patch("urllib.request.urlopen", return_value=_FakeSyncResp(big_content)):
            result = self._run("zha", "sensor.py", provider="cloud")
        assert result.success is True
        from utils.tool_executor import _MAX_HA_DOC_FETCH_CHARS

        assert len(result.output) <= _MAX_HA_DOC_FETCH_CHARS


class TestFetchUrl:
    """Tests for ToolExecutor._fetch_url."""

    def _run(self, url: str, *, allow_wan: bool = True):
        with patch(
            "utils.tool_executor._config_mod.ALLOW_DIAGNOSTIC_WAN", allow_wan
        ), patch("utils.tool_executor._config_mod.DIAGNOSTIC_WAN_TIMEOUT_SECONDS", 10):
            executor = _make_executor()
            return asyncio.run(executor._fetch_url(url))

    def test_disallowed_when_config_false(self):
        result = self._run("https://example.com", allow_wan=False)
        assert result.success is False
        assert "ALLOW_DIAGNOSTIC_WAN" in result.error

    def test_private_ip_blocked(self):
        result = self._run("http://192.168.1.1/test")
        assert result.success is False
        assert "Blocked" in result.error

    def test_loopback_blocked(self):
        result = self._run("http://127.0.0.1/test")
        assert result.success is False
        assert "Blocked" in result.error

    def test_non_http_scheme_blocked(self):
        result = self._run("ftp://example.com/file")
        assert result.success is False
        assert "Only http" in result.error

    def test_successful_get(self):
        fake_body = b"OK response body"
        with patch("urllib.request.urlopen", return_value=_FakeSyncResp(fake_body)):
            result = self._run("https://example.com/api")
        assert result.success is True
        assert "OK response body" in result.output

    def test_truncation(self):
        big_body = ("x" * 10_000).encode()
        with patch("urllib.request.urlopen", return_value=_FakeSyncResp(big_body)):
            result = self._run("https://example.com/api")
        assert result.success is True
        from utils.tool_executor import _MAX_FETCH_URL_CHARS

        assert len(result.output) <= _MAX_FETCH_URL_CHARS
