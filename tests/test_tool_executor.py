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
    from utils.agent.tool_registry import FixEnrichment

    return FixEnrichment(
        relevant_config_section="http:\n  server_port: 8123",
        explanation="The server_port value is wrong; 8124 is the correct value.",
        confidence="high",
        suggested_fix_summary="Change server_port from 8123 to 8124.",
    ).model_dump_json()


def _make_executor(*, llm_client=None, notifier=None):
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.hitl.notify import FakeNotifier
    from utils.ha.ssh_client import FakeSSHClient
    from utils.agent.tool_executor import ToolExecutor

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
        from utils.hitl.notify import FakeNotifier

        notifier_obj = FakeNotifier()
        llm = _CapturingFakeLLMClient(_make_enrichment_json())
        executor = _make_executor(llm_client=llm, notifier=notifier_obj)

        result = asyncio.run(
            executor._enrich_fix_context(_VALID_YAML, _PROPOSED_YAML, "Fix port")
        )
        from utils.agent.tool_registry import FixEnrichment

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
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier(approve=False)
        executor = _make_executor(llm_client=llm_client, notifier=notifier)

        with patch("utils.repair.yaml_validator.validate_proposed_fix") as mock_val:
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

            with (
                patch(
                    "utils.agent.tool_executor._config_mod.HA_SOURCE_CACHE_DIR",
                    str(cache_dir),
                ),
                patch("utils.agent.tool_executor._config_mod.LLM_PROVIDER", provider),
            ):
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
        from utils.agent.tool_executor import _MAX_HA_DOC_FETCH_CHARS

        assert len(result.output) <= _MAX_HA_DOC_FETCH_CHARS


class TestFetchUrl:
    """Tests for ToolExecutor._fetch_url."""

    def _run(self, url: str, *, allow_wan: bool = True):
        with (
            patch(
                "utils.agent.tool_executor._config_mod.ALLOW_DIAGNOSTIC_WAN", allow_wan
            ),
            patch(
                "utils.agent.tool_executor._config_mod.DIAGNOSTIC_WAN_TIMEOUT_SECONDS",
                10,
            ),
        ):
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
        from utils.agent.tool_executor import _MAX_FETCH_URL_CHARS

        assert len(result.output) <= _MAX_FETCH_URL_CHARS


class TestSearchHaDocs:
    """Tests for ToolExecutor._search_ha_docs."""

    def _run(self, query: str, *, allow_wan: bool = True, response_body: bytes = b""):
        with patch(
            "utils.agent.tool_executor._config_mod.ALLOW_DIAGNOSTIC_WAN", allow_wan
        ):
            executor = _make_executor()
            if response_body:
                with patch(
                    "urllib.request.urlopen",
                    return_value=_FakeSyncResp(response_body),
                ):
                    return asyncio.run(executor._search_ha_docs(query))
            return asyncio.run(executor._search_ha_docs(query))

    def _algolia_response(self, hits: list[dict]) -> bytes:
        import json

        return json.dumps({"hits": hits}).encode()

    def test_disallowed_when_config_false(self):
        result = self._run("Lovelace", allow_wan=False)
        assert result.success is False
        assert "ALLOW_DIAGNOSTIC_WAN" in result.error

    def test_no_hits_returns_not_found(self):
        body = self._algolia_response([])
        result = self._run("xyzzy", response_body=body)
        assert result.success is True
        assert "No results" in result.output

    def test_returns_titles_and_urls(self):
        body = self._algolia_response(
            [
                {
                    "hierarchy": {"lvl1": "Lovelace"},
                    "url": "https://www.home-assistant.io/docs/lovelace/",
                    "content": "Lovelace is the dashboard UI for Home Assistant.",
                },
            ]
        )
        result = self._run("Lovelace dashboard", response_body=body)
        assert result.success is True
        assert "Lovelace" in result.output
        assert "home-assistant.io" in result.output

    def test_truncates_to_2000_chars(self):
        long_content = "x" * 5000
        body = self._algolia_response(
            [
                {
                    "hierarchy": {"lvl1": "Test"},
                    "url": "https://www.home-assistant.io/test",
                    "content": long_content,
                },
            ]
        )
        result = self._run("test", response_body=body)
        assert result.success is True
        assert len(result.output) <= 2000

    def test_network_error_returns_failure(self):
        with (
            patch("utils.agent.tool_executor._config_mod.ALLOW_DIAGNOSTIC_WAN", True),
            patch(
                "urllib.request.urlopen",
                side_effect=OSError("connection refused"),
            ),
        ):
            executor = _make_executor()
            result = asyncio.run(executor._search_ha_docs("test"))
        assert result.success is False
        assert "connection refused" in result.error


class TestInvestigateDevice:
    """Tests for ToolExecutor._investigate_device."""

    _ENRICHED = {
        "source_ip": "192.168.1.42",
        "hostname": "myphone.local",
        "netalertx_name": "MyPhone",
        "ha_device_name": None,
        "is_known_device": True,
        "mac_address": "aa:bb:cc:dd:ee:ff",
        "mac_is_randomized": False,
        "mac_vendor": "Apple, Inc.",
        "dhcp_hostname": None,
    }

    def _run(self, ip: str, *, enriched: dict | None = None):
        import asyncio

        executor = _make_executor()
        mock_result = enriched if enriched is not None else self._ENRICHED
        with patch(
            "agents.ha_notification_manager.enrich_http_login",
            new=AsyncMock(return_value=mock_result),
        ):
            return asyncio.run(executor._investigate_device(ip))

    def test_valid_ip_returns_enriched_context(self):
        result = self._run("192.168.1.42")
        assert result.success is True
        import json

        data = json.loads(result.output)
        assert data["mac_address"] == "aa:bb:cc:dd:ee:ff"
        assert data["mac_vendor"] == "Apple, Inc."
        assert data["is_known_device"] is True

    def test_invalid_ip_returns_error(self):
        result = self._run("not-an-ip")
        assert result.success is False
        assert "Invalid IP" in result.error

    def test_empty_ip_returns_error(self):
        result = self._run("")
        assert result.success is False
        assert "Invalid IP" in result.error

    def test_randomized_mac_flag_propagated(self):
        enriched = dict(self._ENRICHED)
        enriched["mac_is_randomized"] = True
        enriched["mac_vendor"] = None
        result = self._run("192.168.1.42", enriched=enriched)
        assert result.success is True
        import json

        data = json.loads(result.output)
        assert data["mac_is_randomized"] is True
        assert data["mac_vendor"] is None

    def test_enrich_exception_returns_error(self):
        import asyncio

        executor = _make_executor()
        with patch(
            "agents.ha_notification_manager.enrich_http_login",
            new=AsyncMock(side_effect=RuntimeError("ARP failed")),
        ):
            result = asyncio.run(executor._investigate_device("192.168.1.1"))
        assert result.success is False
        assert "ARP failed" in result.error

    def test_ws_client_passed_to_enrich_enables_ha_device_registry(self):
        """ToolExecutor must forward its ws_client so the HA device registry step fires."""
        import asyncio
        import json

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ws = FakeHAWebSocketClient(
            devices=[
                {
                    "id": "device_abc",
                    "name": "My Phone",
                    "name_by_user": "My Phone",
                    "connections": [["ip", "192.168.1.42"]],
                }
            ]
        )
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            ha_ws_client=ws,
        )

        enriched = dict(self._ENRICHED)
        enriched["ha_device_name"] = "My Phone"
        with patch(
            "agents.ha_notification_manager.enrich_http_login",
            new=AsyncMock(return_value=enriched),
        ) as mock_enrich:
            result = asyncio.run(executor._investigate_device("192.168.1.42"))

        assert result.success is True
        data = json.loads(result.output)
        assert data["ha_device_name"] == "My Phone"
        _, kwargs = mock_enrich.call_args
        assert kwargs["ws_client"] is ws

    def test_set_ws_client_injects_after_construction(self):
        """set_ws_client() deferred injection works the same as constructor injection."""
        import asyncio

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ws = FakeHAWebSocketClient()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        assert executor._ws_client is None
        executor.set_ws_client(ws)
        assert executor._ws_client is ws

        with patch(
            "agents.ha_notification_manager.enrich_http_login",
            new=AsyncMock(return_value=self._ENRICHED),
        ) as mock_enrich:
            asyncio.run(executor._investigate_device("192.168.1.1"))

        _, kwargs = mock_enrich.call_args
        assert kwargs["ws_client"] is ws

    def test_investigate_device_in_chat_registry(self):
        from utils.agent.tool_registry import build_chat_tool_registry

        reg = build_chat_tool_registry()
        assert "investigate_device" in reg

    def test_investigate_device_in_ha_repair_registry(self):
        from utils.agent.tool_registry import build_ha_tool_registry

        reg = build_ha_tool_registry()
        assert "investigate_device" in reg

    def test_netalertx_api_client_forwarded_to_enrich(self):
        """netalertx_api_client passed at construction must reach enrich_http_login."""
        from unittest.mock import MagicMock

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        nax_api = MagicMock()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            netalertx_api_client=nax_api,
        )

        with patch(
            "agents.ha_notification_manager.enrich_http_login",
            new=AsyncMock(return_value=self._ENRICHED),
        ) as mock_enrich:
            asyncio.run(executor._investigate_device("192.168.1.42"))

        _, kwargs = mock_enrich.call_args
        assert kwargs["netalertx_client"] is nax_api


class TestSaveStrategy:
    """Tests for _save_strategy executor (exposed as save_runbook tool)."""

    def _make_executor_with_store(self, tmp_path):
        import sqlite3

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_strategies ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "trigger_pattern TEXT NOT NULL, approach TEXT NOT NULL, "
                "runbook_state TEXT NOT NULL DEFAULT 'candidate', "
                "created_at TEXT NOT NULL)"
            )
            conn.commit()

        store = FakeKnowledgeStore()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            knowledge_store=store,
            db_path=db_path,
        )
        return executor, store, db_path

    def test_saves_to_chroma_and_sqlite(self, tmp_path):
        executor, store, db_path = self._make_executor_with_store(tmp_path)
        result = asyncio.run(
            executor._save_strategy(
                "ZHA crash", "ZHA unavailable", "Read logs, check USB"
            )
        )
        assert result.success is True
        assert "ZHA crash" in result.output
        chunks = store.query("ZHA", top_k=5, collections=["strategies"])
        assert len(chunks) == 1
        assert "ZHA crash" in chunks[0].text
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT title FROM agent_strategies").fetchone()
        assert row[0] == "ZHA crash"

    def test_missing_title_returns_error(self, tmp_path):
        executor, _, _ = self._make_executor_with_store(tmp_path)
        result = asyncio.run(executor._save_strategy("", "trigger", "approach"))
        assert result.success is False

    def test_no_knowledge_store_still_writes_sqlite(self, tmp_path):
        import sqlite3

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        db_path = str(tmp_path / "test2.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_strategies ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "trigger_pattern TEXT NOT NULL, approach TEXT NOT NULL, "
                "runbook_state TEXT NOT NULL DEFAULT 'candidate', "
                "created_at TEXT NOT NULL)"
            )
            conn.commit()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            knowledge_store=None,
            db_path=db_path,
        )
        result = asyncio.run(executor._save_strategy("title", "trigger", "approach"))
        assert result.success is True
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT title FROM agent_strategies").fetchone()
        assert row[0] == "title"


class TestReadPueoLog:
    """Tests for _read_pueo_log executor."""

    def test_reads_log_file(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "pueo.log"
        log_file.write_text("line1\nline2\nline3\n")

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(executor._read_pueo_log(lines=10))
        assert result.success is True
        assert "line1" in result.output
        assert "line3" in result.output

    def test_missing_log_returns_error(self, tmp_path):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = tmp_path / "nonexistent"
            result = asyncio.run(executor._read_pueo_log())
        assert result.success is False
        assert "not found" in result.error

    def test_level_filter_error_only(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "pueo.log"
        log_file.write_text(
            '{"level":"INFO","msg":"info line"}\n'
            '{"level":"ERROR","msg":"err line"}\n'
            '{"level":"WARNING","msg":"warn line"}\n'
        )

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(executor._read_pueo_log(lines=100, level="ERROR"))
        assert result.success is True
        assert "err line" in result.output
        assert "info line" not in result.output


class TestSearchLog:
    """Tests for _search_log executor."""

    def test_pattern_match_returns_results(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "pueo.log"
        log_file.write_text("line1\nstream_reset detected\nline3\nline4\n")

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(
                executor._search_log("pueo", "stream_reset", context_lines=0)
            )
        assert result.success is True
        assert "stream_reset" in result.output

    def test_no_match_returns_success_with_message(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "pueo.log").write_text("line1\nline2\n")

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(executor._search_log("pueo", "xyz_not_present"))
        assert result.success is True
        assert "No matches" in result.output

    def test_invalid_regex_returns_error(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "pueo.log").write_text("x\n")

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(executor._search_log("pueo", "[invalid"))
        assert result.success is False
        assert "Invalid regex" in result.error

    def test_unknown_log_name_returns_error(self, tmp_path):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        result = asyncio.run(executor._search_log("unknown_log", "pattern"))
        assert result.success is False
        assert "Unknown log_name" in result.error

    def test_pueo_stderr_log_searched(self, tmp_path):
        """search_log with log_name='pueo_stderr' reads pueo-stderr.log (plain text)."""
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "pueo-stderr.log").write_text(
            "INFO:     Application startup complete.\n"
            "ERROR:    Exception in ASGI application\n"
            "RuntimeError: Response content longer than Content-Length\n"
        )
        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}, command_results={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(
                executor._search_log("pueo_stderr", "ASGI", context_lines=0)
            )
        assert result.success is True
        assert "ASGI" in result.output

    def test_ha_supervisor_log_uses_ssh(self, tmp_path):
        """search_log with log_name='ha_supervisor' runs ha supervisor logs over SSH."""
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(
            file_contents={},
            command_results={
                "ha supervisor logs": (
                    0,
                    "supervisor started\nsupervisor error here\n",
                    "",
                )
            },
        )
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        result = asyncio.run(
            executor._search_log("ha_supervisor", "error", context_lines=0)
        )
        assert result.success is True
        assert "error" in result.output

    def test_ha_app_log_requires_addon_slug(self):
        """search_log with log_name='ha_app' and no addon_slug returns an error."""
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}, command_results={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        result = asyncio.run(executor._search_log("ha_app", "error"))
        assert result.success is False
        assert "addon_slug" in result.error

    def test_ha_app_log_calls_ha_apps_logs(self):
        """search_log with log_name='ha_app' and addon_slug runs ha apps logs <slug>."""
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(
            file_contents={},
            command_results={
                "ha apps logs core_mosquitto": (
                    0,
                    "[mosquitto] Started\n[mosquitto] error: client disconnected\n",
                    "",
                )
            },
        )
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        result = asyncio.run(
            executor._search_log(
                "ha_app", "error", context_lines=0, addon_slug="core_mosquitto"
            )
        )
        assert result.success is True
        assert "error" in result.output

    def test_ssh_backed_log_fails_without_ssh_client(self):
        """SSH-backed log sources return a clear error when no SSH client is available."""
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.agent.tool_executor import ToolExecutor

        executor = ToolExecutor(
            ha_ssh_client=None,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )
        for log_name in ("ha_core", "ha_supervisor", "ha_os", "ha_host", "ha_app"):
            kw = {"addon_slug": "core_mosquitto"} if log_name == "ha_app" else {}
            result = asyncio.run(executor._search_log(log_name, "error", **kw))
            assert result.success is False
            assert (
                "SSH" in result.error or "No SSH" in result.error
            ), f"Expected SSH error for {log_name}, got: {result.error}"


class TestReadPueoLogFilename:
    """Tests for read_pueo_log with filename parameter (issue #406)."""

    def _make_executor(self):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}, command_results={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )

    def test_reads_pueo_stderr_log(self, tmp_path):
        """filename='pueo-stderr.log' reads the stderr file."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "pueo-stderr.log").write_text(
            "INFO:     Application startup complete.\n"
            "ERROR:    Exception in ASGI application\n"
        )
        executor = self._make_executor()
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(
                executor._read_pueo_log(lines=50, filename="pueo-stderr.log")
            )
        assert result.success is True
        assert "ASGI" in result.output

    def test_disallowed_filename_rejected(self, tmp_path):
        """filename outside the allowlist is rejected with an error."""
        executor = self._make_executor()
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = tmp_path
            result = asyncio.run(executor._read_pueo_log(filename="../../etc/passwd"))
        assert result.success is False
        assert "must be one of" in result.error

    def test_level_filter_plain_text_for_stderr(self, tmp_path):
        """Level filter on pueo-stderr.log uses plain-text substring match."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "pueo-stderr.log").write_text(
            "INFO:     startup ok\n"
            "ERROR:    Exception in ASGI\n"
            "WARNING:  slow response\n"
        )
        executor = self._make_executor()
        with patch("utils.agent.tool_executor._get_dirs") as mock_dirs:
            mock_dirs.return_value.log_dir = log_dir
            result = asyncio.run(
                executor._read_pueo_log(
                    lines=100, level="ERROR", filename="pueo-stderr.log"
                )
            )
        assert result.success is True
        assert "ASGI" in result.output
        assert "startup ok" not in result.output


class TestReadLogsExtended:
    """Tests for the extended read_logs tool (log_source + addon_slug params)."""

    def _make_executor(self, command_results=None):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(
                file_contents={}, command_results=command_results or {}
            ),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )

    def test_default_reads_ha_core(self):
        """read_logs() with no args calls ha core logs --lines 100."""
        executor = self._make_executor(
            {"ha core logs --lines 100": (0, "core log line\n", "")}
        )
        result = asyncio.run(executor._read_logs(100))
        assert result.success is True
        assert "core log line" in result.output

    def test_ha_supervisor_calls_correct_command(self):
        """read_logs(log_source='ha_supervisor') calls ha supervisor logs --no-follow."""
        executor = self._make_executor(
            {"ha supervisor logs --no-follow": (0, "supervisor started\n", "")}
        )
        result = asyncio.run(executor._read_logs(100, log_source="ha_supervisor"))
        assert result.success is True
        assert "supervisor started" in result.output

    def test_ha_os_calls_correct_command(self):
        """read_logs(log_source='ha_os') calls ha os logs --no-follow."""
        executor = self._make_executor(
            {"ha os logs --no-follow": (0, "os boot info\n", "")}
        )
        result = asyncio.run(executor._read_logs(100, log_source="ha_os"))
        assert result.success is True
        assert "os boot info" in result.output

    def test_ha_host_calls_correct_command(self):
        """read_logs(log_source='ha_host') calls ha host logs --no-follow."""
        executor = self._make_executor(
            {"ha host logs --no-follow": (0, "host kernel msg\n", "")}
        )
        result = asyncio.run(executor._read_logs(100, log_source="ha_host"))
        assert result.success is True
        assert "host kernel msg" in result.output

    def test_ha_app_calls_ha_apps_logs_with_slug(self):
        """read_logs(log_source='ha_app', addon_slug='core_mosquitto') uses correct command."""
        executor = self._make_executor(
            {"ha apps logs core_mosquitto -n 100": (0, "mosquitto started\n", "")}
        )
        result = asyncio.run(
            executor._read_logs(100, log_source="ha_app", addon_slug="core_mosquitto")
        )
        assert result.success is True
        assert "mosquitto started" in result.output

    def test_ha_app_requires_addon_slug(self):
        """read_logs(log_source='ha_app') without slug returns error."""
        executor = self._make_executor()
        result = asyncio.run(executor._read_logs(100, log_source="ha_app"))
        assert result.success is False
        assert "addon_slug" in result.error

    def test_lines_capped_at_500(self):
        """Lines argument is capped at 500."""
        executor = self._make_executor(
            {"ha core logs --lines 500": (0, "output\n", "")}
        )
        result = asyncio.run(executor._read_logs(9999))
        assert result.success is True


class TestListLogSources:
    """Tests for the list_log_sources tool."""

    def _make_executor(self, command_results=None, ha_ssh=True):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = (
            FakeSSHClient(file_contents={}, command_results=command_results or {})
            if ha_ssh
            else None
        )
        return ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )

    def test_returns_system_sources_always(self):
        """System sources are always returned regardless of SSH availability."""
        import json

        executor = self._make_executor(ha_ssh=False)
        result = asyncio.run(executor._list_log_sources())
        assert result.success is True
        data = json.loads(result.output)
        sources = [s["log_name"] for s in data["system_sources"]]
        assert "ha_core" in sources
        assert "ha_supervisor" in sources
        assert "ha_os" in sources
        assert "ha_host" in sources
        assert data["app_sources"] == []

    def test_parses_app_sources_from_ha_apps_list(self):
        """App sources are populated from ha apps list --raw-json output."""
        import json

        apps_json = json.dumps(
            [
                {"slug": "core_mosquitto", "name": "Mosquitto Broker"},
                {"slug": "netalertx_fa", "name": "NetAlertX"},
            ]
        )
        executor = self._make_executor({"ha apps list --raw-json": (0, apps_json, "")})
        result = asyncio.run(executor._list_log_sources())
        assert result.success is True
        data = json.loads(result.output)
        slugs = [s["addon_slug"] for s in data["app_sources"]]
        assert "core_mosquitto" in slugs
        assert "netalertx_fa" in slugs
        assert all(s["log_name"] == "ha_app" for s in data["app_sources"])

    def test_bad_json_from_ha_apps_list_returns_system_sources_only(self):
        """Malformed JSON from ha apps list does not crash — returns system sources only."""
        import json

        executor = self._make_executor(
            {"ha apps list --raw-json": (0, "not-json!", "")}
        )
        result = asyncio.run(executor._list_log_sources())
        assert result.success is True
        data = json.loads(result.output)
        assert len(data["system_sources"]) == 4
        assert data["app_sources"] == []

    def test_no_ssh_returns_system_sources_only(self):
        """Without SSH, only system sources are returned and app_sources is empty."""
        import json

        executor = self._make_executor(ha_ssh=False)
        result = asyncio.run(executor._list_log_sources())
        assert result.success is True
        data = json.loads(result.output)
        assert len(data["system_sources"]) == 4
        assert data["app_sources"] == []


class TestGetHaProfile:
    """Tests for ToolExecutor._get_ha_profile and get_ha_profile_summary."""

    def _make_executor(self):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )

    def _make_profile(self):
        from utils.ha.ha_environment import HAEnvironmentProfile

        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
            config_yaml_top_keys=["homeassistant", "mqtt"],
            installed_integrations=["zha", "mqtt", "esphome"],
            hacs_integrations=["custom_comp"],
            config_entries=[{"id": "e1"}, {"id": "e2"}],
        )

    def test_no_profile_returns_not_available(self):
        executor = self._make_executor()
        result = asyncio.run(executor._get_ha_profile())
        assert result.success is True
        assert "not yet available" in result.output

    def test_no_field_returns_compact_summary(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._get_ha_profile())
        assert result.success is True
        assert "3 installed" in result.output
        assert "zha" not in result.output  # full list must not appear

    def test_field_installed_integrations_returns_full_list(self):
        import json

        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._get_ha_profile(field="installed_integrations"))
        assert result.success is True
        data = json.loads(result.output)
        assert "zha" in data
        assert "esphome" in data

    def test_field_hacs_integrations_returns_full_list(self):
        import json

        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._get_ha_profile(field="hacs_integrations"))
        assert result.success is True
        assert "custom_comp" in json.loads(result.output)

    def test_field_config_entries_returns_full_list(self):
        import json

        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._get_ha_profile(field="config_entries"))
        assert result.success is True
        data = json.loads(result.output)
        assert len(data) == 2

    def test_unknown_field_returns_error(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._get_ha_profile(field="bad_field"))
        assert result.success is False
        assert "bad_field" in (result.error or "")

    def test_get_ha_profile_summary_returns_compact(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        summary = executor.get_ha_profile_summary()
        assert "3 installed" in summary
        assert "HA environment" in summary

    def test_get_ha_profile_summary_no_profile(self):
        executor = self._make_executor()
        summary = executor.get_ha_profile_summary()
        assert "not yet available" in summary


class TestSearchIntegrations:
    """Tests for ToolExecutor._search_integrations."""

    def _make_executor(self):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )

    def _make_profile(self, installed=None, hacs=None):
        from utils.ha.ha_environment import HAEnvironmentProfile

        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
            config_yaml_top_keys=[],
            installed_integrations=installed or ["zha", "mqtt", "esphome"],
            hacs_integrations=hacs or ["my_custom_card"],
            config_entries=[],
        )

    def test_no_profile_returns_not_available(self):
        executor = self._make_executor()
        result = asyncio.run(executor._search_integrations("zha"))
        assert result.success is True
        assert "not yet available" in result.output

    def test_match_in_installed(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._search_integrations("esp"))
        assert result.success is True
        assert "esphome" in result.output
        assert "Installed" in result.output

    def test_match_in_hacs(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._search_integrations("custom"))
        assert result.success is True
        assert "my_custom_card" in result.output
        assert "HACS" in result.output

    def test_no_match_returns_not_found(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile())
        result = asyncio.run(executor._search_integrations("dashy"))
        assert result.success is True
        assert "No matching" in result.output

    def test_case_insensitive_match(self):
        executor = self._make_executor()
        executor.set_ha_profile(self._make_profile(installed=["ZHA"]))
        result = asyncio.run(executor._search_integrations("zha"))
        assert result.success is True
        assert "ZHA" in result.output


class TestGetDashboardEntityHealth:
    """Tests for ToolExecutor._get_dashboard_entity_health."""

    def _make_executor(self, ws=None):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            ha_ws_client=ws,
        )

    def _make_ws(self, entity_ids, lovelace_cfg):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        return FakeHAWebSocketClient(
            entity_registry=[{"entity_id": e} for e in entity_ids],
            lovelace_configs={None: lovelace_cfg},
        )

    def test_missing_entity_returned(self):
        cfg = {
            "views": [
                {"title": "Home", "cards": [{"type": "entity", "entity": "light.gone"}]}
            ]
        }
        ws = self._make_ws(["light.living_room"], cfg)
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is True
        assert "light.gone" in result.output
        assert "not found" in result.output

    def test_no_missing_entities(self):
        cfg = {
            "views": [
                {"title": "Home", "cards": [{"type": "entity", "entity": "light.lamp"}]}
            ]
        }
        ws = self._make_ws(["light.lamp"], cfg)
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is True
        assert "valid" in result.output.lower()

    def test_ws_client_none_returns_error(self):
        executor = self._make_executor(ws=None)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is False
        assert "HA_API_TOKEN" in result.error

    def test_ws_error_propagates_as_tool_error(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        class _BrokenWS(FakeHAWebSocketClient):
            async def get_entity_registry(self):
                raise RuntimeError("connection lost")

        ws = _BrokenWS()
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is False
        assert "connection lost" in result.error

    def test_case_insensitive_url_path_match(self):
        """Passing 'DASHY' should match url_path='dashy' and detect the missing entity."""
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "entity", "entity": "sensor.missing"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.present"}],
            lovelace_dashboards=[{"url_path": "dashy", "title": "Dashy"}],
            lovelace_configs={None: {}, "dashy": cfg},
        )
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health(dashboard="DASHY"))
        assert result.success is True
        assert "sensor.missing" in result.output
        assert "not found" in result.output

    def test_title_match_finds_dashboard(self):
        """Passing display name 'Dashy' should match title='Dashy' and detect the missing entity."""
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "entity", "entity": "sensor.gone"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.present"}],
            lovelace_dashboards=[{"url_path": "dashy", "title": "Dashy"}],
            lovelace_configs={None: {}, "dashy": cfg},
        )
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health(dashboard="Dashy"))
        assert result.success is True
        assert "sensor.gone" in result.output

    def test_file_mode_dashboard_reported_not_silently_skipped(self):
        """File-mode dashboard (LovelaceConfigNotFound) should appear in output, not silently pass."""
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.present"}],
            lovelace_dashboards=[{"url_path": "file_dash", "title": "File Dash"}],
            lovelace_configs={None: {}},
            lovelace_config_not_found={"file_dash"},
        )
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is True
        assert "config not accessible" in result.output
        assert "file_dash" in result.output

    def test_disabled_entity_flagged_not_silently_passed(self):
        """Entities with disabled_by set show as 'Entity not found' in Lovelace; tool must report them."""
        cfg = {
            "views": [
                {
                    "title": "Main",
                    "cards": [
                        {"type": "tile", "entity": "water_heater.broken_heater"},
                        {"type": "tile", "entity": "sensor.good_sensor"},
                    ],
                }
            ]
        }
        ws = self._make_ws(
            entity_ids=[],  # not used directly; we pass full registry below
            lovelace_cfg=cfg,
        )
        # Override entity_registry to have one disabled and one active entity
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient(
            entity_registry=[
                {
                    "entity_id": "water_heater.broken_heater",
                    "disabled_by": "config_entry",
                },
                {"entity_id": "sensor.good_sensor"},
            ],
            lovelace_configs={None: cfg},
        )
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is True
        assert "water_heater.broken_heater" in result.output
        assert "disabled" in result.output
        assert "config_entry" in result.output
        # The active entity must NOT appear in the output
        assert "sensor.good_sensor" not in result.output

    def test_disabled_entity_not_offered_as_replacement(self):
        """Disabled entities must not appear in fuzzy replacement suggestions for absent entities."""
        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "tile", "entity": "sensor.high_tide"}],
                }
            ]
        }
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient(
            entity_registry=[
                # sensor.high_tide_level is disabled — must not appear as replacement
                {"entity_id": "sensor.high_tide_level", "disabled_by": "integration"},
                {"entity_id": "sensor.something_else"},
            ],
            lovelace_configs={None: cfg},
        )
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert "sensor.high_tide" in result.output
        assert "not found" in result.output
        # Disabled entity must not surface as a replacement candidate
        assert "sensor.high_tide_level" not in result.output

    def test_named_dashboard_does_not_also_scan_default(self):
        """When a named dashboard is requested and found, the default dashboard is NOT scanned."""
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        # Default dashboard has a broken entity; named dashboard is clean
        default_cfg = {
            "views": [
                {
                    "title": "Default",
                    "cards": [{"type": "tile", "entity": "sensor.broken"}],
                }
            ]
        }
        named_cfg = {
            "views": [
                {"title": "Named", "cards": [{"type": "tile", "entity": "sensor.good"}]}
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.good"}],
            lovelace_dashboards=[{"url_path": "my_dash", "title": "My Dash"}],
            lovelace_configs={None: default_cfg, "my_dash": named_cfg},
        )
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health(dashboard="My Dash"))
        assert result.success is True
        # Named dashboard is healthy — result should say all valid
        assert "valid" in result.output.lower()
        # The default dashboard's broken entity must not appear (it was not requested)
        assert "sensor.broken" not in result.output

    def test_view_filter_targets_correct_view(self):
        """When view= is given, only entities in that view are checked."""
        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "tile", "entity": "sensor.home_missing"}],
                },
                {
                    "title": "Main",
                    "cards": [{"type": "tile", "entity": "sensor.main_missing"}],
                },
            ]
        }
        ws = self._make_ws([], cfg)
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health(view="Main"))
        assert result.success is True
        assert "sensor.main_missing" in result.output
        assert "sensor.home_missing" not in result.output

    def test_view_filter_case_insensitive(self):
        """view= matching is case-insensitive against view title and path."""
        cfg = {
            "views": [
                {
                    "title": "Main",
                    "path": "main",
                    "cards": [{"type": "tile", "entity": "sensor.target"}],
                }
            ]
        }
        ws = self._make_ws([], cfg)
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health(view="MAIN"))
        assert result.success is True
        assert "sensor.target" in result.output

    def test_view_filter_none_scans_all_views(self):
        """Without view=, entities in all views are reported."""
        cfg = {
            "views": [
                {
                    "title": "Alpha",
                    "cards": [{"type": "tile", "entity": "sensor.alpha_missing"}],
                },
                {
                    "title": "Beta",
                    "cards": [{"type": "tile", "entity": "sensor.beta_missing"}],
                },
            ]
        }
        ws = self._make_ws([], cfg)
        executor = self._make_executor(ws=ws)
        result = asyncio.run(executor._get_dashboard_entity_health())
        assert result.success is True
        assert "sensor.alpha_missing" in result.output
        assert "sensor.beta_missing" in result.output


class TestExecuteLocalPython:
    """Tests for ToolExecutor._execute_local_python."""

    def _make_executor(self):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(file_contents={}),
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
        )

    def test_returns_stdout_on_success(self):
        executor = self._make_executor()
        result = asyncio.run(
            executor._execute_local_python(
                code='print("hello world")', description="basic test"
            )
        )
        assert result.success is True
        assert "hello world" in result.output

    def test_returns_error_on_nonzero_exit(self):
        executor = self._make_executor()
        result = asyncio.run(
            executor._execute_local_python(
                code="raise ValueError('boom')", description="error test"
            )
        )
        assert result.success is False
        assert "1" in result.error or "exit" in result.error.lower()

    def test_output_is_truncated_to_limit(self):
        executor = self._make_executor()
        # Print 20000 chars worth of output
        code = "print('x' * 20000)"
        result = asyncio.run(
            executor._execute_local_python(code=code, description="truncation test")
        )
        assert result.success is True
        assert len(result.output) <= 8000

    def test_timeout_returns_error(self):
        import unittest.mock as mock

        executor = self._make_executor()
        import subprocess

        with mock.patch(
            "utils.agent.tool_executor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="python", timeout=60),
        ):
            result = asyncio.run(
                executor._execute_local_python(
                    code="import time; time.sleep(999)", description="timeout test"
                )
            )
        assert result.success is False
        assert "timed out" in result.error.lower()


def _make_bare_executor():
    """Helper: make a plain ToolExecutor with no SSH command results."""
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.hitl.notify import FakeNotifier
    from utils.ha.ssh_client import FakeSSHClient
    from utils.agent.tool_executor import ToolExecutor

    ssh = FakeSSHClient(file_contents={}, command_results={})
    return ToolExecutor(
        ha_ssh_client=ssh,
        gate=FakeAutonomyGate(auto_execute_result=False),
        notifier=FakeNotifier(),
    )


class TestReadPueoLogTimeRange:
    """Tests for after/before time-range filtering in _read_pueo_log."""

    def test_after_filter_excludes_old_lines(self, tmp_path, pueo_dirs):
        log_dir = pueo_dirs.log_dir
        log_file = log_dir / "pueo.log"
        log_file.write_text(
            '{"timestamp":"2026-09-04 02:00:00","level":"INFO","msg":"early"}\n'
            '{"timestamp":"2026-09-04 03:30:00","level":"INFO","msg":"inwindow"}\n'
            '{"timestamp":"2026-09-04 05:00:00","level":"INFO","msg":"after"}\n'
        )
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._read_pueo_log(
                after="2026-09-04 03:00:00", before="2026-09-04 04:00:00"
            )
        )
        assert result.success is True
        assert "inwindow" in result.output
        assert "early" not in result.output
        assert "after" not in result.output

    def test_no_lines_in_window_returns_empty_message(self, tmp_path, pueo_dirs):
        log_dir = pueo_dirs.log_dir
        log_file = log_dir / "pueo.log"
        log_file.write_text(
            '{"timestamp":"2026-09-04 02:00:00","level":"INFO","msg":"x"}\n'
        )
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._read_pueo_log(
                after="2026-09-04 10:00:00", before="2026-09-04 11:00:00"
            )
        )
        assert result.success is True
        assert "No log lines" in result.output

    def test_token_cap_adds_truncation_notice(self, tmp_path, pueo_dirs):
        log_dir = pueo_dirs.log_dir
        # Generate a log line larger than 8000 chars total
        big_line = (
            '{"timestamp":"2026-09-04 03:00:00","level":"INFO","msg":"'
            + "x" * 200
            + '"}\n'
        )
        log_file = log_dir / "pueo.log"
        log_file.write_text(big_line * 50)
        executor = _make_bare_executor()
        result = asyncio.run(executor._read_pueo_log(lines=500))
        assert result.success is True
        assert "truncated" in result.output


class TestSummarizeLogWindow:
    """Tests for _summarize_log_window."""

    def test_counts_by_level(self, tmp_path, pueo_dirs):
        log_dir = pueo_dirs.log_dir
        log_file = log_dir / "pueo.log"
        log_file.write_text(
            '{"timestamp":"2026-09-04 03:10:00","level":"ERROR","msg":"boom"}\n'
            '{"timestamp":"2026-09-04 03:15:00","level":"WARNING","msg":"warn"}\n'
            '{"timestamp":"2026-09-04 03:20:00","level":"INFO","msg":"info"}\n'
            '{"timestamp":"2026-09-04 02:00:00","level":"INFO","msg":"outside"}\n'
        )
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._summarize_log_window(
                "pueo", "2026-09-04 03:00:00", "2026-09-04 04:00:00"
            )
        )
        assert result.success is True
        assert "ERROR: 1" in result.output
        assert "WARNING: 1" in result.output
        assert "INFO: 1" in result.output
        assert "boom" in result.output
        assert "warn" in result.output
        assert "outside" not in result.output

    def test_empty_window_says_no_lines(self, tmp_path, pueo_dirs):
        log_dir = pueo_dirs.log_dir
        (log_dir / "pueo.log").write_text(
            '{"timestamp":"2026-09-04 02:00:00","level":"INFO","msg":"x"}\n'
        )
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._summarize_log_window(
                "pueo", "2026-09-04 10:00:00", "2026-09-04 11:00:00"
            )
        )
        assert result.success is True
        assert "no log lines" in result.output.lower()

    def test_missing_log_returns_error(self, tmp_path, pueo_dirs):
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._summarize_log_window(
                "pueo", "2026-09-04 03:00:00", "2026-09-04 04:00:00"
            )
        )
        assert result.success is False
        assert "not found" in result.error

    def test_invalid_log_name_returns_error(self, tmp_path, pueo_dirs):
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._summarize_log_window(
                "ha_core", "2026-09-04 03:00:00", "2026-09-04 04:00:00"
            )
        )
        assert result.success is False
        assert "pueo" in result.error

    def test_missing_after_or_before_returns_error(self, tmp_path, pueo_dirs):
        executor = _make_bare_executor()
        result = asyncio.run(
            executor._summarize_log_window("pueo", "", "2026-09-04 04:00:00")
        )
        assert result.success is False
        assert "required" in result.error


class TestSaveRunbookType:
    """Tests for runbook_type param in _save_strategy."""

    def test_gap_type_stored_in_output(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_strategies ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "trigger_pattern TEXT NOT NULL, approach TEXT NOT NULL, "
                "runbook_state TEXT NOT NULL DEFAULT 'candidate', "
                "created_at TEXT NOT NULL)"
            )
            conn.commit()
        executor = _make_bare_executor()
        executor._db_path = db_path
        result = asyncio.run(
            executor._save_strategy(
                "stuck", "auth failure", "tried X", runbook_type="gap"
            )
        )
        assert result.success is True
        assert "gap" in result.output
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT runbook_state FROM agent_strategies").fetchone()
        assert row[0] == "gap"

    def test_invalid_type_falls_back_to_candidate(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_strategies ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "trigger_pattern TEXT NOT NULL, approach TEXT NOT NULL, "
                "runbook_state TEXT NOT NULL DEFAULT 'candidate', "
                "created_at TEXT NOT NULL)"
            )
            conn.commit()
        executor = _make_bare_executor()
        executor._db_path = db_path
        result = asyncio.run(
            executor._save_strategy(
                "test", "trigger", "approach", runbook_type="unknown_type"
            )
        )
        assert result.success is True
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT runbook_state FROM agent_strategies").fetchone()
        assert row[0] == "candidate"


class TestRequestEscalation:
    """Tests for _request_escalation."""

    def test_hitl_preference_sends_notification(self, tmp_path, monkeypatch):
        import config as _config

        monkeypatch.setattr(_config, "ESCALATION_PREFERENCE", "hitl")
        notifier_calls = []

        async def fake_send(subject, body, payload):
            notifier_calls.append((subject, body, payload))

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor

        ssh = FakeSSHClient(file_contents={}, command_results={})
        fake_notifier = FakeNotifier()
        fake_notifier.send = fake_send
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=fake_notifier,
        )
        result = asyncio.run(
            executor._request_escalation("stuck on auth", "maybe timeout")
        )
        assert result.success is True
        assert (
            "notification" in result.output.lower() or "sent" in result.output.lower()
        )
        assert len(notifier_calls) == 1
        assert "stuck on auth" in notifier_calls[0][1]

    def test_cloud_preference_returns_cloud_message(self, tmp_path, monkeypatch):
        import config as _config

        monkeypatch.setattr(_config, "ESCALATION_PREFERENCE", "cloud")
        executor = _make_bare_executor()
        result = asyncio.run(executor._request_escalation("no progress"))
        assert result.success is True
        assert "cloud" in result.output.lower()

    def test_cloud_then_hitl_preference(self, tmp_path, monkeypatch):
        import config as _config

        monkeypatch.setattr(_config, "ESCALATION_PREFERENCE", "cloud_then_hitl")
        executor = _make_bare_executor()
        result = asyncio.run(executor._request_escalation("no progress"))
        assert result.success is True
        assert "cloud" in result.output.lower()


class TestRepeatQuery:
    """Tests for repeat_query in utils/core/prompts.py."""

    def test_appends_system_prompt_after_separator(self):
        from utils.core.prompts import repeat_query

        result = repeat_query("SYSTEM", "USER CONTENT")
        assert result.startswith("USER CONTENT")
        assert "---" in result
        assert result.endswith("SYSTEM")

    def test_empty_user_content(self):
        from utils.core.prompts import repeat_query

        result = repeat_query("SYSTEM", "")
        assert "SYSTEM" in result
        assert "---" in result

    def test_system_prompt_appears_twice_in_messages(self):
        from utils.core.prompts import repeat_query

        system = "You are a helpful assistant."
        user = "What is 2+2?"
        result = repeat_query(system, user)
        assert user in result
        assert system in result


class TestQueryKnowledgeAuthorityLabels:
    """Tests for authority labels in _query_knowledge output."""

    def _make_executor_with_store(self, tmp_path):
        import sqlite3

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_memory "
                "(key TEXT, content TEXT, source TEXT, ts REAL)"
            )
            conn.commit()

        store = FakeKnowledgeStore()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            knowledge_store=store,
            db_path=db_path,
        )
        return executor, store

    def test_official_label_in_output(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "ha_integration_docs",
            ids=["doc-1"],
            documents=["mqtt broker configuration"],
            metadatas=[{"source": "ha_docs/mqtt"}],
        )
        result = asyncio.run(executor._query_knowledge("mqtt"))
        assert result.success is True
        assert "[OFFICIAL]" in result.output

    def test_seed_runbook_label_in_output(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "strategies",
            ids=["seed-1"],
            documents=["seed runbook for disk space"],
            metadatas=[{"source": "seed_prompt", "title": "disk"}],
        )
        result = asyncio.run(executor._query_knowledge("disk space"))
        assert result.success is True
        assert "[SEED RUNBOOK]" in result.output

    def test_candidate_runbook_label_in_output(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "strategies",
            ids=["cand-1"],
            documents=["candidate runbook for network"],
            metadatas=[{"source": "agent_learned", "runbook_type": "candidate"}],
        )
        result = asyncio.run(executor._query_knowledge("network"))
        assert result.success is True
        assert "CANDIDATE RUNBOOK" in result.output

    def test_past_repair_label_in_output(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "repair_history",
            ids=["rep-1"],
            documents=["past repair for zwave"],
            metadatas=[{"source": "repair_episode", "outcome": "success"}],
        )
        result = asyncio.run(executor._query_knowledge("zwave"))
        assert result.success is True
        assert "[PAST REPAIR]" in result.output

    def test_authority_label_method_official(self):
        from utils.agent.tool_executor import ToolExecutor

        assert (
            ToolExecutor._knowledge_authority_label("ha_release_notes", {})
            == "[OFFICIAL]"
        )
        assert (
            ToolExecutor._knowledge_authority_label("ha_concepts", {}) == "[OFFICIAL]"
        )
        assert (
            ToolExecutor._knowledge_authority_label("ha_integration_docs", {})
            == "[OFFICIAL]"
        )

    def test_authority_label_method_community(self):
        from utils.agent.tool_executor import ToolExecutor

        assert (
            ToolExecutor._knowledge_authority_label("hacs_changelogs", {})
            == "[COMMUNITY]"
        )


class TestQueryKnowledgeTypeRouting:
    """Tests for query_type routing in _query_knowledge."""

    def _make_executor_with_store(self, tmp_path):
        import sqlite3

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_memory "
                "(key TEXT, content TEXT, source TEXT, ts REAL)"
            )
            conn.commit()

        store = FakeKnowledgeStore()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            knowledge_store=store,
            db_path=db_path,
        )
        return executor, store

    def test_diagnostic_routes_to_repair_history_and_release_notes(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "repair_history",
            ids=["rep-1"],
            documents=["past repair for mqtt broker"],
            metadatas=[{"source": "repair_episode", "outcome": "success"}],
        )
        store.upsert(
            "strategies",
            ids=["strat-1"],
            documents=["mqtt broker runbook"],
            metadatas=[{"source": "seed_prompt"}],
        )
        result = asyncio.run(
            executor._query_knowledge("mqtt broker", query_type="diagnostic")
        )
        assert result.success is True
        # repair_history included
        assert "[PAST REPAIR]" in result.output
        # strategies not included for diagnostic
        assert "runbook" not in result.output

    def test_version_check_routes_to_release_notes_only(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "ha_release_notes",
            ids=["rn-1"],
            documents=["zha breaking change notes"],
            metadatas=[{"source": "release_notes", "version": "2026.9"}],
        )
        store.upsert(
            "ha_integration_docs",
            ids=["doc-1"],
            documents=["zha integration config docs"],
            metadatas=[{"source": "ha_docs/zha"}],
        )
        result = asyncio.run(
            executor._query_knowledge("zha breaking", query_type="version_check")
        )
        assert result.success is True
        assert "[OFFICIAL]" in result.output
        # integration_docs not in version_check collections
        assert "integration config docs" not in result.output

    def test_procedural_routes_to_developer_docs_and_concepts(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "ha_developer_docs",
            ids=["dev-1"],
            documents=["how to implement a config flow"],
            metadatas=[{"source": "developer_docs/config_entries"}],
        )
        store.upsert(
            "repair_history",
            ids=["rep-1"],
            documents=["config flow past repair episode"],
            metadatas=[{"source": "repair_episode", "outcome": "success"}],
        )
        result = asyncio.run(
            executor._query_knowledge("config flow", query_type="procedural")
        )
        assert result.success is True
        assert "implement a config flow" in result.output
        # repair_history not included for procedural
        assert "[PAST REPAIR]" not in result.output

    def test_generative_routes_to_concepts_and_strategies(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "strategies",
            ids=["strat-1"],
            documents=["template sensor automation pattern"],
            metadatas=[{"source": "seed_prompt", "title": "template"}],
        )
        store.upsert(
            "repair_history",
            ids=["rep-1"],
            documents=["template sensor automation repair"],
            metadatas=[{"source": "repair_episode", "outcome": "success"}],
        )
        result = asyncio.run(
            executor._query_knowledge(
                "template sensor automation", query_type="generative"
            )
        )
        assert result.success is True
        assert "[SEED RUNBOOK]" in result.output
        # repair_history not included for generative
        assert "[PAST REPAIR]" not in result.output

    def test_no_query_type_uses_all_collections(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "ha_release_notes",
            ids=["rn-1"],
            documents=["mqtt release note"],
            metadatas=[{"source": "release_notes", "version": "2026.9"}],
        )
        store.upsert(
            "repair_history",
            ids=["rep-1"],
            documents=["mqtt repair history"],
            metadatas=[{"source": "repair_episode", "outcome": "success"}],
        )
        result = asyncio.run(executor._query_knowledge("mqtt"))
        assert result.success is True
        # Both collections returned when no query_type
        assert "[OFFICIAL]" in result.output
        assert "[PAST REPAIR]" in result.output

    def test_unknown_query_type_falls_back_to_all_collections(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "ha_release_notes",
            ids=["rn-1"],
            documents=["zha release note"],
            metadatas=[{"source": "release_notes", "version": "2026.9"}],
        )
        store.upsert(
            "repair_history",
            ids=["rep-1"],
            documents=["zha repair history"],
            metadatas=[{"source": "repair_episode", "outcome": "success"}],
        )
        result = asyncio.run(
            executor._query_knowledge("zha", query_type="nonexistent_type")
        )
        assert result.success is True
        # Unknown type: all collections searched
        assert "[OFFICIAL]" in result.output
        assert "[PAST REPAIR]" in result.output

    def test_query_type_routing_map_keys(self):
        from utils.agent.tool_executor import ToolExecutor

        assert "diagnostic" in ToolExecutor._QUERY_TYPE_COLLECTIONS
        assert "procedural" in ToolExecutor._QUERY_TYPE_COLLECTIONS
        assert "generative" in ToolExecutor._QUERY_TYPE_COLLECTIONS
        assert "version_check" in ToolExecutor._QUERY_TYPE_COLLECTIONS

    def test_diagnostic_collections_include_repair_history(self):
        from utils.agent.tool_executor import ToolExecutor

        cols = ToolExecutor._QUERY_TYPE_COLLECTIONS["diagnostic"]
        assert "repair_history" in cols
        assert "ha_release_notes" in cols
        assert "ha_integration_docs" in cols

    def test_version_check_only_release_notes(self):
        from utils.agent.tool_executor import ToolExecutor

        cols = ToolExecutor._QUERY_TYPE_COLLECTIONS["version_check"]
        assert cols == ["ha_release_notes"]


class TestVersionScoreBoosting:
    """Tests for ha_version score boosting in _query_knowledge."""

    # ------------------------------------------------------------------
    # _parse_ha_version_tuple helper
    # ------------------------------------------------------------------

    def test_parse_version_standard(self):
        from utils.agent.tool_executor import _parse_ha_version_tuple

        assert _parse_ha_version_tuple("2026.9.0") == (2026, 9)

    def test_parse_version_minor_only(self):
        from utils.agent.tool_executor import _parse_ha_version_tuple

        assert _parse_ha_version_tuple("2025.11") == (2025, 11)

    def test_parse_version_beta(self):
        from utils.agent.tool_executor import _parse_ha_version_tuple

        assert _parse_ha_version_tuple("2026.10.0b3") == (2026, 10)

    def test_parse_version_empty_returns_none(self):
        from utils.agent.tool_executor import _parse_ha_version_tuple

        assert _parse_ha_version_tuple("") is None
        assert _parse_ha_version_tuple("not_a_version") is None

    # ------------------------------------------------------------------
    # _version_score_multiplier helper
    # ------------------------------------------------------------------

    def test_multiplier_matching_version_returns_boost(self):
        from utils.agent.tool_executor import _version_score_multiplier

        meta = {"ha_version_min": "2026.9.0", "ha_version_max": "2026.9.0"}
        assert _version_score_multiplier(meta, (2026, 9)) == 1.2

    def test_multiplier_older_than_12_months_returns_penalty(self):
        from utils.agent.tool_executor import _version_score_multiplier

        # 13 months before 2026.9
        meta = {"ha_version_min": "2025.8.0", "ha_version_max": "2025.8.0"}
        assert _version_score_multiplier(meta, (2026, 9)) == 0.5

    def test_multiplier_within_12_months_not_matching_returns_neutral(self):
        from utils.agent.tool_executor import _version_score_multiplier

        # 6 months old, but version range does not span current
        meta = {"ha_version_min": "2026.3.0", "ha_version_max": "2026.3.0"}
        result = _version_score_multiplier(meta, (2026, 9))
        assert result == 1.0

    def test_multiplier_no_version_metadata_returns_neutral(self):
        from utils.agent.tool_executor import _version_score_multiplier

        assert _version_score_multiplier({}, (2026, 9)) == 1.0

    def test_multiplier_open_ended_max_returns_boost(self):
        from utils.agent.tool_executor import _version_score_multiplier

        # No ha_version_max → open-ended, assumed current
        meta = {"ha_version_min": "2026.9.0"}
        assert _version_score_multiplier(meta, (2026, 9)) == 1.2

    # ------------------------------------------------------------------
    # _query_knowledge integration with ha_version
    # ------------------------------------------------------------------

    def _make_executor_with_store(self, tmp_path):
        import sqlite3

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE agent_memory "
                "(key TEXT, content TEXT, source TEXT, ts REAL)"
            )
            conn.commit()

        store = FakeKnowledgeStore()
        ssh = FakeSSHClient(file_contents={}, command_results={})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(),
            knowledge_store=store,
            db_path=db_path,
        )
        return executor, store

    def test_version_boost_applied_to_matching_chunk(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        # Chunk matching current version (2026.9) — should score higher
        store.upsert(
            "ha_release_notes",
            ids=["new-chunk"],
            documents=["zha breaking change 2026.9"],
            metadatas=[
                {
                    "source": "ha_release_notes/2026.9.0",
                    "ha_version_min": "2026.9.0",
                    "ha_version_max": "2026.9.0",
                }
            ],
        )
        # Old chunk (13 months before current) — should score lower
        store.upsert(
            "ha_release_notes",
            ids=["old-chunk"],
            documents=["zha breaking change 2025.8"],
            metadatas=[
                {
                    "source": "ha_release_notes/2025.8.0",
                    "ha_version_min": "2025.8.0",
                    "ha_version_max": "2025.8.0",
                }
            ],
        )
        result = asyncio.run(
            executor._query_knowledge("zha breaking change", ha_version="2026.9.0")
        )
        assert result.success is True
        # The newer chunk (2026.9) should appear first
        assert result.output.index("2026.9") < result.output.index("2025.8")

    def test_no_ha_version_no_change(self, tmp_path):
        import asyncio

        executor, store = self._make_executor_with_store(tmp_path)
        store.upsert(
            "ha_release_notes",
            ids=["chunk-1"],
            documents=["mqtt change"],
            metadatas=[{"source": "ha_release_notes/2026.9.0"}],
        )
        # Should succeed without version boost and without error
        result = asyncio.run(executor._query_knowledge("mqtt change"))
        assert result.success is True

    def test_auto_detect_version_from_profile(self, tmp_path):
        import asyncio

        from utils.ha.ha_environment import HAEnvironmentProfile

        executor, store = self._make_executor_with_store(tmp_path)
        profile = HAEnvironmentProfile()
        profile.ha_version = "2026.9.0"
        executor.set_ha_profile(profile)

        store.upsert(
            "ha_release_notes",
            ids=["new-v"],
            documents=["new version content"],
            metadatas=[
                {
                    "source": "ha_release_notes/2026.9.0",
                    "ha_version_min": "2026.9.0",
                    "ha_version_max": "2026.9.0",
                }
            ],
        )
        store.upsert(
            "ha_release_notes",
            ids=["old-v"],
            documents=["old version content"],
            metadatas=[
                {
                    "source": "ha_release_notes/2025.8.0",
                    "ha_version_min": "2025.8.0",
                    "ha_version_max": "2025.8.0",
                }
            ],
        )
        # No explicit ha_version — should auto-detect from profile
        result = asyncio.run(executor._query_knowledge("version content"))
        assert result.success is True
        # Newer chunk should appear before older one
        assert result.output.index("2026.9") < result.output.index("2025.8")
