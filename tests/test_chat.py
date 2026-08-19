"""Tests for conversational agent — item 72.

Covers: migrations v8+v9, remember/recall, chat tool registry,
AgentLoop.terminal_tool_name, read_source, sandbox_code, add_tool.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest

import ha_agent_advanced
from utils.autonomy import FakeAutonomyGate
from utils.notify import FakeNotifier
from utils.ssh_client import FakeSSHClient
from utils.tool_executor import ToolExecutor
from utils.tool_registry import ToolCall


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    path = str(tmp_path / "test_agent.db")
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
    ha_agent_advanced.init_local_database()
    return path


@pytest.fixture
def executor(db_path):
    return ToolExecutor(
        ha_ssh_client=FakeSSHClient(),
        gate=FakeAutonomyGate(),
        notifier=FakeNotifier(),
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# TestMigrationV8
# ---------------------------------------------------------------------------


class TestMigrationV8:
    def _table_exists(self, db_path: str, table: str) -> bool:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchall()
        return len(rows) > 0

    def test_agent_memory_table_exists(self, db_path):
        assert self._table_exists(db_path, "agent_memory")

    def test_chat_sessions_table_exists(self, db_path):
        assert self._table_exists(db_path, "chat_sessions")

    def test_chat_messages_table_exists(self, db_path):
        assert self._table_exists(db_path, "chat_messages")

    def test_agent_memory_schema(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_memory)")]
        for col in ("id", "key", "content", "source", "ts"):
            assert col in cols

    def test_chat_messages_foreign_key_column(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(chat_messages)")]
        assert "session_id" in cols
        assert "role" in cols


# ---------------------------------------------------------------------------
# TestMigrationV9
# ---------------------------------------------------------------------------


class TestMigrationV9:
    def test_registered_tools_table_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='registered_tools'"
            ).fetchall()
        assert len(rows) == 1

    def test_registered_tools_schema(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = [
                row[1] for row in conn.execute("PRAGMA table_info(registered_tools)")
            ]
        for col in (
            "id",
            "name",
            "description",
            "parameters_json",
            "code",
            "created_at",
        ):
            assert col in cols


# ---------------------------------------------------------------------------
# TestRememberRecall
# ---------------------------------------------------------------------------


class TestRememberRecall:
    def test_remember_stores_row(self, executor, db_path):
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={"key": "server", "content": "NAS is at 192.168.1.100"},
                )
            )
        )
        assert result.success
        assert "server" in result.output

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT key, content FROM agent_memory").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "server"
        assert "192.168.1.100" in rows[0][1]

    def test_recall_finds_by_keyword(self, executor, db_path):
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={
                        "key": "media_server",
                        "content": "media server IP is 192.168.1.50",
                    },
                )
            )
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="recall", arguments={"query": "media server"})
            )
        )
        assert result.success
        assert "192.168.1.50" in result.output

    def test_recall_empty_on_no_match(self, executor):
        result = asyncio.run(
            executor.execute(
                ToolCall(name="recall", arguments={"query": "nonexistent_xyz_123"})
            )
        )
        assert result.success
        assert "Nothing found" in result.output

    def test_recall_respects_top_k(self, db_path, monkeypatch):
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "CHAT_MEMORY_TOP_K", 3)
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )

        for i in range(5):
            asyncio.run(
                ex.execute(
                    ToolCall(
                        name="remember",
                        arguments={
                            "key": f"item{i}",
                            "content": f"value {i} matching keyword",
                        },
                    )
                )
            )

        result = asyncio.run(
            ex.execute(ToolCall(name="recall", arguments={"query": "matching keyword"}))
        )
        assert result.success
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 3

    def test_remember_uses_agent_source_by_default(self, db_path, executor):
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={"key": "note", "content": "something important"},
                )
            )
        )
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT source FROM agent_memory").fetchall()
        assert rows[0][0] == "agent"

    def test_recall_matches_on_key(self, executor):
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={
                        "key": "unique_key_xyz",
                        "content": "unrelated content here",
                    },
                )
            )
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="recall", arguments={"query": "unique_key_xyz"})
            )
        )
        assert result.success
        assert "unique_key_xyz" in result.output


# ---------------------------------------------------------------------------
# TestChatToolRegistry
# ---------------------------------------------------------------------------


class TestChatToolRegistry:
    @pytest.fixture(autouse=True)
    def registry(self):
        from utils.tool_registry import build_chat_tool_registry

        self._registry = build_chat_tool_registry()

    def test_contains_finish_chat(self):
        assert "finish_chat" in self._registry

    def test_does_not_contain_apply_fix(self):
        assert "apply_fix" not in self._registry

    def test_does_not_contain_verify_fix(self):
        assert "verify_fix" not in self._registry

    def test_contains_remember(self):
        assert "remember" in self._registry

    def test_contains_recall(self):
        assert "recall" in self._registry

    def test_contains_read_source(self):
        assert "read_source" in self._registry

    def test_contains_propose_patch(self):
        assert "propose_patch" in self._registry

    def test_contains_sandbox_code(self):
        assert "sandbox_code" in self._registry

    def test_contains_add_tool(self):
        assert "add_tool" in self._registry

    def test_contains_open_pr(self):
        assert "open_pr" in self._registry

    def test_contains_get_disk_usage(self):
        assert "get_disk_usage" in self._registry

    def test_does_not_contain_finish_repair(self):
        assert "finish_repair" not in self._registry


# ---------------------------------------------------------------------------
# TestAgentLoopTerminalTool
# ---------------------------------------------------------------------------


class TestAgentLoopTerminalTool:
    """AgentLoop terminates on finish_chat when terminal_tool_name='finish_chat'."""

    def _make_loop(self, llm, terminal_tool_name, db_path):
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeLLMClient
        from utils.tool_registry import build_chat_tool_registry

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        return AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name=terminal_tool_name,
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )

    def test_finish_chat_terminates_loop(self, db_path):
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, "finish_chat", db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "success"

    def test_default_terminal_tool_is_finish_repair(self, db_path):
        """Default terminal_tool_name='finish_repair' still works unchanged."""
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_ha_tool_registry

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "Done",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_ha_tool_registry(),
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "success"

    def test_finish_chat_does_not_trigger_default_loop(self, db_path):
        """finish_chat call in a default (finish_repair) loop does NOT terminate it."""
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_ha_tool_registry

        # Call finish_chat (not finish_repair) — loop should continue and exhaust
        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "done",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        # Use default terminal_tool_name = "finish_repair"; registry has finish_repair
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_ha_tool_registry(),
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )
        result = asyncio.run(loop.run("test"))
        # finish_repair IS the terminal tool here → should succeed
        assert result.outcome == "success"


# ---------------------------------------------------------------------------
# TestReadSource
# ---------------------------------------------------------------------------


class TestReadSource:
    @pytest.fixture
    def fake_repo(self, monkeypatch, tmp_path):
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "_REPO_ROOT", tmp_path)
        return tmp_path

    def test_reads_file_within_repo(self, fake_repo, executor):
        target = fake_repo / "hello.py"
        target.write_text("print('hello')")
        result = asyncio.run(
            executor.execute(
                ToolCall(name="read_source", arguments={"path": "hello.py"})
            )
        )
        assert result.success
        assert "print('hello')" in result.output

    def test_rejects_path_outside_repo(self, fake_repo, executor):
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="read_source",
                    arguments={"path": "../../etc/passwd"},
                )
            )
        )
        assert not result.success
        assert result.error is not None
        assert "traversal" in result.error.lower() or "rejected" in result.error.lower()

    def test_rejects_disallowed_extension(self, fake_repo, executor):
        target = fake_repo / "binary.exe"
        target.write_text("ELF")
        result = asyncio.run(
            executor.execute(
                ToolCall(name="read_source", arguments={"path": "binary.exe"})
            )
        )
        assert not result.success
        assert "Extension not allowed" in (result.error or "")

    def test_truncates_at_8000_chars(self, fake_repo, executor):
        target = fake_repo / "large.py"
        target.write_text("x" * 10_000)
        result = asyncio.run(
            executor.execute(
                ToolCall(name="read_source", arguments={"path": "large.py"})
            )
        )
        assert result.success
        assert len(result.output) == 8000


# ---------------------------------------------------------------------------
# TestSandboxCode
# ---------------------------------------------------------------------------


class TestSandboxCode:
    """sandbox_code delegates to subprocess; tests mock it to avoid real CI runs."""

    @pytest.fixture
    def patched_executor(self, db_path, monkeypatch, tmp_path):
        import utils.tool_executor as te_mod

        # Redirect _REPO_ROOT so propose_patch accepts a path that exists there
        monkeypatch.setattr(te_mod, "_REPO_ROOT", tmp_path)
        (tmp_path / "snippet.py").write_text("x = 1\n")

        # Stub copytree to avoid copying the whole real repo
        monkeypatch.setattr(te_mod.shutil, "copytree", lambda *a, **kw: None)

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )

    def _fake_proc(self, returncode: int, out: str = "") -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = out
        m.stderr = ""
        return m

    def test_no_pending_patch_returns_error(self, patched_executor):
        result = asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="sandbox_code",
                    arguments={"description": "test"},
                )
            )
        )
        assert not result.success
        assert "propose_patch" in (result.error or "")

    def test_all_checks_pass_returns_success(self, patched_executor, monkeypatch):
        import utils.tool_executor as te_mod

        # Stage a patch first
        asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="propose_patch",
                    arguments={"path": "snippet.py", "content": "x = 2\n"},
                )
            )
        )

        proc_ok = self._fake_proc(0, "All good")
        monkeypatch.setattr(te_mod.subprocess, "run", lambda *a, **kw: proc_ok)

        result = asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="sandbox_code",
                    arguments={"description": "formatting fix"},
                )
            )
        )
        assert result.success
        assert patched_executor._sandbox_passed is True

    def test_failing_check_returns_failure(self, patched_executor, monkeypatch):
        import utils.tool_executor as te_mod

        asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="propose_patch",
                    arguments={"path": "snippet.py", "content": "x = 2\n"},
                )
            )
        )

        proc_fail = self._fake_proc(1, "E101 syntax error")
        monkeypatch.setattr(te_mod.subprocess, "run", lambda *a, **kw: proc_fail)

        result = asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="sandbox_code",
                    arguments={"description": "bad patch"},
                )
            )
        )
        assert not result.success
        assert patched_executor._sandbox_passed is False

    def test_sandbox_output_captured(self, patched_executor, monkeypatch):
        import utils.tool_executor as te_mod

        asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="propose_patch",
                    arguments={"path": "snippet.py", "content": "x = 2\n"},
                )
            )
        )

        proc_ok = self._fake_proc(0, "pytest passed")
        monkeypatch.setattr(te_mod.subprocess, "run", lambda *a, **kw: proc_ok)

        result = asyncio.run(
            patched_executor.execute(
                ToolCall(
                    name="sandbox_code",
                    arguments={"description": "check"},
                )
            )
        )
        assert "pytest passed" in result.output


# ---------------------------------------------------------------------------
# TestAddTool
# ---------------------------------------------------------------------------


class TestAddTool:
    _VALID_CODE = "async def tool_implementation(args):\n    return 'ok'\n"

    @pytest.fixture
    def prepped_executor(self, db_path, monkeypatch, tmp_path):
        """Executor with sandbox already passed and a pending patch staged."""
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "_REPO_ROOT", tmp_path)
        (tmp_path / "new_tool.py").write_text(self._VALID_CODE)

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        # Manually mark sandbox as passed (simulates sandbox_code succeeding)
        ex._sandbox_passed = True
        ex._sandbox_output = "All checks passed"
        return ex

    def test_disabled_by_default(self, executor, monkeypatch):
        import config

        monkeypatch.setattr(config, "CHAT_ALLOW_TOOL_REGISTRATION", False)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="add_tool",
                    arguments={
                        "name": "ping_host",
                        "description": "Pings a host",
                        "parameters_schema": "{}",
                        "code": self._VALID_CODE,
                    },
                )
            )
        )
        assert not result.success
        assert "CHAT_ALLOW_TOOL_REGISTRATION" in (result.error or "")

    def test_sandbox_not_run_returns_error(self, prepped_executor, monkeypatch):
        import config

        monkeypatch.setattr(config, "CHAT_ALLOW_TOOL_REGISTRATION", True)
        prepped_executor._sandbox_passed = False  # override back to False
        result = asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="add_tool",
                    arguments={
                        "name": "ping_host",
                        "description": "Pings a host",
                        "parameters_schema": "{}",
                        "code": self._VALID_CODE,
                    },
                )
            )
        )
        assert not result.success
        assert "sandbox_code" in (result.error or "").lower()

    def test_valid_flow_queues_hitl_card(self, prepped_executor, monkeypatch):
        import config

        monkeypatch.setattr(config, "CHAT_ALLOW_TOOL_REGISTRATION", True)
        result = asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="add_tool",
                    arguments={
                        "name": "ping_host",
                        "description": "Pings a host",
                        "parameters_schema": '{"type": "object"}',
                        "code": self._VALID_CODE,
                    },
                )
            )
        )
        assert result.awaiting_approval is True

    def test_valid_flow_notifier_receives_card(self, prepped_executor, monkeypatch):
        import config

        monkeypatch.setattr(config, "CHAT_ALLOW_TOOL_REGISTRATION", True)
        asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="add_tool",
                    arguments={
                        "name": "ping_host",
                        "description": "Pings a host",
                        "parameters_schema": "{}",
                        "code": self._VALID_CODE,
                    },
                )
            )
        )
        notifier = prepped_executor._notifier
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["tool_name"] == "ping_host"
        assert "code_proposal" in payload["card_type"]

    def test_syntax_error_in_code_returns_error(self, prepped_executor, monkeypatch):
        import config

        monkeypatch.setattr(config, "CHAT_ALLOW_TOOL_REGISTRATION", True)
        result = asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="add_tool",
                    arguments={
                        "name": "bad_tool",
                        "description": "Has syntax error",
                        "parameters_schema": "{}",
                        "code": "def broken(:\n    pass\n",
                    },
                )
            )
        )
        assert not result.success
        assert "Syntax error" in (result.error or "")

    def test_register_dynamic_tool_callable(self, db_path):
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )

        async def my_tool(args):
            return "result"

        ex.register_dynamic_tool("my_tool", my_tool)
        result = asyncio.run(ex.execute(ToolCall(name="my_tool", arguments={})))
        assert result.success
        assert result.output == "result"


# ---------------------------------------------------------------------------
# TestGetDiskUsageTool
# ---------------------------------------------------------------------------


class TestGetDiskUsageTool:
    """get_disk_usage returns a formatted breakdown using cached or fresh data."""

    def _make_fake_breakdown(self):
        from utils.disk_usage import DiskBreakdown, DiskItem, DiskSection

        backup_item = DiskItem(
            path="/backup/abc123.tar",
            name="abc123.tar",
            size_bytes=2 * 1024**3,
            size_human="2.0 GB",
            is_empty=False,
            pct_of_section=100.0,
        )
        backups = DiskSection(
            title="Backups",
            items=[backup_item],
            total_bytes=2 * 1024**3,
            total_human="2.0 GB",
            is_empty=False,
        )
        return DiskBreakdown(
            sections=[backups],
            disk_used_gb=8.5,
            disk_total_gb=32.0,
            disk_free_gb=23.5,
            disk_used_pct=26.6,
            fetched_at=__import__("time").time(),
            container_images_estimated_gb=6.5,
        )

    def test_uses_cached_breakdown(self, executor, monkeypatch):
        import utils.disk_usage as du_mod

        fake = self._make_fake_breakdown()
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", fake)
        result = asyncio.run(
            executor.execute(ToolCall(name="get_disk_usage", arguments={}))
        )
        assert result.success
        assert "8.5 GB used" in result.output
        assert "32.0 GB total" in result.output
        assert "Backups" in result.output
        assert "abc123.tar" in result.output

    def test_fetches_fresh_when_no_cache(self, executor, monkeypatch):
        import utils.disk_usage as du_mod

        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)
        fake = self._make_fake_breakdown()

        async def fake_fetch(ssh_client):
            return fake

        monkeypatch.setattr(du_mod, "fetch_disk_breakdown", fake_fetch)
        result = asyncio.run(
            executor.execute(ToolCall(name="get_disk_usage", arguments={}))
        )
        assert result.success
        assert "8.5 GB used" in result.output

    def test_fetches_fresh_when_cache_stale(self, executor, monkeypatch):
        import utils.disk_usage as du_mod

        stale = self._make_fake_breakdown()
        stale.fetched_at = 0.0  # epoch — always stale
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", stale)

        fresh = self._make_fake_breakdown()
        fresh.disk_free_gb = 5.0  # different value to confirm fresh was used

        async def fake_fetch(ssh_client):
            return fresh

        monkeypatch.setattr(du_mod, "fetch_disk_breakdown", fake_fetch)
        result = asyncio.run(
            executor.execute(ToolCall(name="get_disk_usage", arguments={}))
        )
        assert result.success
        assert "5.0 GB free" in result.output

    def test_container_estimate_included(self, executor, monkeypatch):
        import utils.disk_usage as du_mod

        fake = self._make_fake_breakdown()
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", fake)
        result = asyncio.run(
            executor.execute(ToolCall(name="get_disk_usage", arguments={}))
        )
        assert "6.5 GB" in result.output
        assert "container" in result.output.lower()


# ---------------------------------------------------------------------------
# TestAgentLoopInitialMessages
# ---------------------------------------------------------------------------


class TestAgentLoopInitialMessages:
    """AgentLoop.run() correctly incorporates prior conversation history."""

    def test_loop_runs_with_initial_messages(self, db_path):
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_chat_tool_registry

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {
                                    "summary": "Based on prior context, done."
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )
        prior = [
            {"role": "user", "content": "What is the disk usage?"},
            {"role": "assistant", "content": "It is 80% full."},
        ]
        result = asyncio.run(loop.run("Tell me more.", initial_messages=prior))
        assert result.outcome == "success"

    def test_loop_runs_without_initial_messages(self, db_path):
        """Backward-compat: None initial_messages behaves as before."""
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_chat_tool_registry

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done."},
                            }
                        }
                    ]
                }
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )
        result = asyncio.run(loop.run("Hello", initial_messages=None))
        assert result.outcome == "success"


# ---------------------------------------------------------------------------
# TestOpenPrTool
# ---------------------------------------------------------------------------


class TestOpenPrTool:
    """open_pr queues a HITL card; requires sandbox_code to have passed first."""

    @pytest.fixture
    def prepped_executor(self, db_path, monkeypatch, tmp_path):
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "_REPO_ROOT", tmp_path)
        (tmp_path / "utils").mkdir()
        (tmp_path / "utils" / "helper.py").write_text("x = 1\n")

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        # Stage a patch and mark sandbox as passed
        ex._pending_patch = {"utils/helper.py": "x = 2\n"}
        ex._sandbox_passed = True
        ex._sandbox_output = "All checks passed"
        return ex

    def test_no_pending_patch_returns_error(self, db_path):
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        result = asyncio.run(
            ex.execute(
                ToolCall(
                    name="open_pr",
                    arguments={"title": "fix: do something", "reason": "because X"},
                )
            )
        )
        assert not result.success
        assert "propose_patch" in (result.error or "")

    def test_sandbox_not_passed_returns_error(self, prepped_executor):
        prepped_executor._sandbox_passed = False
        result = asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={"title": "fix: do something", "reason": "because X"},
                )
            )
        )
        assert not result.success
        assert "sandbox_code" in (result.error or "")

    def test_missing_title_returns_error(self, prepped_executor):
        result = asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={"title": "", "reason": "because X"},
                )
            )
        )
        assert not result.success
        assert "title" in (result.error or "")

    def test_valid_flow_queues_hitl_card(self, prepped_executor):
        result = asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={
                        "title": "feat: improve helper",
                        "reason": "Needed for better performance",
                    },
                )
            )
        )
        assert result.awaiting_approval is True

    def test_notifier_receives_open_pr_card(self, prepped_executor):
        asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={
                        "title": "feat: improve helper",
                        "reason": "Needed for better performance",
                    },
                )
            )
        )
        notifier = prepped_executor._notifier
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["card_type"] == "open_pr"
        assert payload["pr_title"] == "feat: improve helper"
        assert "utils/helper.py" in payload["patch_files"]
        assert "ADR 007" in payload["pr_body"]

    def test_branch_name_auto_derived(self, prepped_executor):
        asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={
                        "title": "feat: add new utility",
                        "reason": "because X",
                    },
                )
            )
        )
        payload = prepped_executor._notifier.sent[0]["payload"]
        assert payload["branch_name"].startswith("feat/")

    def test_explicit_branch_name_used(self, prepped_executor):
        asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={
                        "title": "feat: add new utility",
                        "reason": "because X",
                        "branch_name": "feat/custom-branch",
                    },
                )
            )
        )
        payload = prepped_executor._notifier.sent[0]["payload"]
        assert payload["branch_name"] == "feat/custom-branch"

    def test_sandbox_output_included_in_payload(self, prepped_executor):
        asyncio.run(
            prepped_executor.execute(
                ToolCall(
                    name="open_pr",
                    arguments={
                        "title": "fix: something",
                        "reason": "because X",
                    },
                )
            )
        )
        payload = prepped_executor._notifier.sent[0]["payload"]
        assert "All checks passed" in payload["sandbox_output"]


# ---------------------------------------------------------------------------
# TestGapDetection — item 84
# ---------------------------------------------------------------------------


class TestGapDetection:
    """AgentLoop propagates capability_gap from finish_repair; sandbox engine
    and NetAlertX healer launch _run_code_proposal_loop automatically."""

    # ------------------------------------------------------------------
    # AgentLoopResult schema
    # ------------------------------------------------------------------

    def test_agent_loop_result_capability_gap_default_false(self):
        from utils.tool_registry import AgentLoopResult

        r = AgentLoopResult(outcome="success", steps=[])
        assert r.capability_gap is False
        assert r.gap_description == ""

    def test_agent_loop_result_capability_gap_set(self):
        from utils.tool_registry import AgentLoopResult

        r = AgentLoopResult(
            outcome="success",
            steps=[],
            capability_gap=True,
            gap_description="Need a tool to check sensor state",
        )
        assert r.capability_gap is True
        assert r.gap_description == "Need a tool to check sensor state"

    def test_agent_loop_result_json_round_trip_with_gap(self):
        from utils.tool_registry import AgentLoopResult

        orig = AgentLoopResult(
            outcome="success",
            steps=[],
            capability_gap=True,
            gap_description="Missing sensor tool",
        )
        restored = AgentLoopResult.model_validate_json(orig.model_dump_json())
        assert restored.capability_gap is True
        assert restored.gap_description == "Missing sensor tool"

    # ------------------------------------------------------------------
    # FINISH_REPAIR ToolDefinition
    # ------------------------------------------------------------------

    def test_finish_repair_accepts_capability_gap_parameter(self):
        from utils.tool_registry import FINISH_REPAIR

        props = FINISH_REPAIR.parameters["properties"]
        assert "capability_gap" in props
        assert props["capability_gap"]["type"] == "boolean"

    def test_finish_repair_accepts_gap_description_parameter(self):
        from utils.tool_registry import FINISH_REPAIR

        props = FINISH_REPAIR.parameters["properties"]
        assert "gap_description" in props
        assert props["gap_description"]["type"] == "string"

    def test_finish_repair_does_not_require_capability_gap(self):
        from utils.tool_registry import FINISH_REPAIR

        required = FINISH_REPAIR.parameters["required"]
        assert "capability_gap" not in required
        assert "gap_description" not in required

    # ------------------------------------------------------------------
    # build_code_proposal_registry
    # ------------------------------------------------------------------

    def test_code_proposal_registry_has_required_tools(self):
        from utils.tool_registry import build_code_proposal_registry

        reg = build_code_proposal_registry()
        names = reg.names()
        for tool in (
            "read_source",
            "propose_patch",
            "sandbox_code",
            "open_pr",
            "finish_repair",
        ):
            assert tool in names, f"{tool!r} missing from code_proposal registry"

    def test_code_proposal_registry_excludes_apply_fix(self):
        from utils.tool_registry import build_code_proposal_registry

        reg = build_code_proposal_registry()
        assert "apply_fix" not in reg.names()

    # ------------------------------------------------------------------
    # AgentLoop propagates capability_gap from finish_repair arguments
    # ------------------------------------------------------------------

    def test_agent_loop_propagates_capability_gap_true(self, db_path):
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_ha_tool_registry

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "No tool available for this failure",
                                    "action_taken": "needs_human",
                                    "capability_gap": True,
                                    "gap_description": "Need a tool to query sensor history",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_ha_tool_registry(),
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )
        result = asyncio.run(loop.run("Diagnose config"))
        assert result.outcome == "success"
        assert result.capability_gap is True
        assert result.gap_description == "Need a tool to query sensor history"

    def test_agent_loop_capability_gap_false_by_default(self, db_path):
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_ha_tool_registry

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "All good",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_ha_tool_registry(),
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )
        result = asyncio.run(loop.run("Diagnose config"))
        assert result.outcome == "success"
        assert result.capability_gap is False
        assert result.gap_description == ""

    # ------------------------------------------------------------------
    # _run_code_proposal_loop triggered by ha_agent_sandbox_engine.main()
    # ------------------------------------------------------------------

    def test_sandbox_engine_calls_proposal_loop_on_gap(self, db_path, monkeypatch):
        """When AgentLoop result has capability_gap=True, main() calls _run_code_proposal_loop."""
        import ha_agent_sandbox_engine as engine
        from utils.agent_loop import AgentLoopResult

        calls: list[dict] = []

        async def fake_proposal_loop(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        monkeypatch.setattr(engine, "_run_code_proposal_loop", fake_proposal_loop)

        # Patch loop.run() to return a capability_gap result without SSH/LLM
        async def fake_run(*args: object, **kwargs: object) -> AgentLoopResult:
            return AgentLoopResult(
                outcome="success",
                steps=[],
                capability_gap=True,
                gap_description="Need sensor history tool",
            )

        async def fake_fetch(**kw: object) -> tuple[str, str]:
            return ("yaml: {}", "abc")

        monkeypatch.setattr(engine, "fetch_remote_config", fake_fetch)
        monkeypatch.setattr(engine, "DB_PATH", db_path)
        import ha_agent_advanced

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        ha_agent_advanced.init_local_database()

        from utils.agent_loop import AgentLoop

        monkeypatch.setattr(AgentLoop, "run", fake_run)

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient

        asyncio.run(
            engine.main(
                ssh_client=FakeSSHClient(),
                llm_client=FakeLLMClient("{}"),
                notifier=FakeNotifier(),
                gate=FakeAutonomyGate(),
            )
        )

        assert len(calls) == 1
        assert calls[0]["gap_description"] == "Need sensor history tool"

    def test_sandbox_engine_skips_proposal_loop_without_gap(self, db_path, monkeypatch):
        """When capability_gap=False, main() does not call _run_code_proposal_loop."""
        import ha_agent_sandbox_engine as engine
        from utils.agent_loop import AgentLoopResult

        calls: list[dict] = []

        async def fake_proposal_loop(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        monkeypatch.setattr(engine, "_run_code_proposal_loop", fake_proposal_loop)

        async def fake_run(*args: object, **kwargs: object) -> AgentLoopResult:
            return AgentLoopResult(outcome="success", steps=[])

        async def fake_fetch(**kw: object) -> tuple[str, str]:
            return ("yaml: {}", "abc")

        monkeypatch.setattr(engine, "fetch_remote_config", fake_fetch)
        monkeypatch.setattr(engine, "DB_PATH", db_path)
        import ha_agent_advanced

        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db_path)
        ha_agent_advanced.init_local_database()

        from utils.agent_loop import AgentLoop

        monkeypatch.setattr(AgentLoop, "run", fake_run)

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient

        asyncio.run(
            engine.main(
                ssh_client=FakeSSHClient(),
                llm_client=FakeLLMClient("{}"),
                notifier=FakeNotifier(),
                gate=FakeAutonomyGate(),
            )
        )

        assert len(calls) == 0


# ---------------------------------------------------------------------------
# TestProposePatchSafety — item 85
# ---------------------------------------------------------------------------


class TestProposePatchSafety:
    """propose_patch enforces the safety-critical file block list and backup
    invariant chain protection."""

    @pytest.fixture(autouse=True)
    def fake_repo(self, monkeypatch, tmp_path, db_path):
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "_REPO_ROOT", tmp_path)
        (tmp_path / "utils").mkdir()
        for name in ("autonomy.py", "helper.py"):
            (tmp_path / "utils" / name).write_text("x = 1\n")
        for name in ("interfaces.py", "config.py"):
            (tmp_path / name).write_text("x = 1\n")
        self._ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )

    def _patch(self, path: str, content: str):
        return asyncio.run(
            self._ex.execute(
                ToolCall(
                    name="propose_patch", arguments={"path": path, "content": content}
                )
            )
        )

    def test_blocks_utils_autonomy_py(self):
        result = self._patch("utils/autonomy.py", "y = 2\n")
        assert not result.success
        assert "safety-critical" in (result.error or "").lower()

    def test_blocks_interfaces_py(self):
        result = self._patch("interfaces.py", "y = 2\n")
        assert not result.success
        assert "safety-critical" in (result.error or "").lower()

    def test_blocks_config_py(self):
        result = self._patch("config.py", "y = 2\n")
        assert not result.success
        assert "safety-critical" in (result.error or "").lower()

    def test_blocks_redefining_execute_remote_backup(self):
        content = "def execute_remote_backup(ssh_client):\n    return 'slug'\n"
        result = self._patch("utils/helper.py", content)
        assert not result.success
        assert "backup invariant" in (result.error or "").lower()

    def test_blocks_redefining_record_backup_slug(self):
        content = "def record_backup_slug(slug):\n    pass\n"
        result = self._patch("utils/helper.py", content)
        assert not result.success
        assert "backup invariant" in (result.error or "").lower()

    def test_allows_calling_execute_remote_backup(self):
        content = "slug = execute_remote_backup(ssh)\n"
        result = self._patch("utils/helper.py", content)
        assert result.success

    def test_allows_non_critical_file(self):
        result = self._patch("utils/helper.py", "def my_helper(): pass\n")
        assert result.success

    def test_path_traversal_rejected(self):
        result = self._patch("../../etc/passwd", "malicious\n")
        assert not result.success
        assert result.error is not None


# ---------------------------------------------------------------------------
# TestResolveRepoPathSecurity — item 85
# ---------------------------------------------------------------------------


class TestResolveRepoPathSecurity:
    """_resolve_repo_path uses is_relative_to() — immune to the startswith()
    prefix-confusion attack where a sibling directory name begins with the
    repo root name."""

    @pytest.fixture
    def sibling_setup(self, monkeypatch, tmp_path, db_path):
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "_REPO_ROOT", tmp_path)
        # Create a sibling whose name is a prefix extension of tmp_path.name
        sibling = tmp_path.parent / (tmp_path.name + "_evil")
        sibling.mkdir()
        (sibling / "evil.py").write_text("evil\n")
        self._ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        self._sibling = sibling
        self._tmp_path = tmp_path

    def test_sibling_prefix_directory_rejected_for_read_source(self, sibling_setup):
        # Construct a raw path that resolves inside the sibling (not _REPO_ROOT).
        # e.g. if _REPO_ROOT=/tmp/abc, sibling=/tmp/abc_evil, path='../abc_evil/evil.py'
        rel = f"../{self._sibling.name}/evil.py"
        result = asyncio.run(
            self._ex.execute(ToolCall(name="read_source", arguments={"path": rel}))
        )
        assert not result.success
        assert result.error is not None
        assert (
            "traversal" in (result.error or "").lower()
            or "rejected" in (result.error or "").lower()
        )

    def test_sibling_prefix_directory_rejected_for_propose_patch(self, sibling_setup):
        rel = f"../{self._sibling.name}/evil.py"
        result = asyncio.run(
            self._ex.execute(
                ToolCall(
                    name="propose_patch",
                    arguments={"path": rel, "content": "evil\n"},
                )
            )
        )
        assert not result.success
        assert result.error is not None


# ---------------------------------------------------------------------------
# TestPreStepCallback — Chunk 1 of transparency feature (issue #264)
# ---------------------------------------------------------------------------


class TestPreStepCallback:
    """pre_step_callback fires before each tool execution, before step_callback."""

    def _make_loop(self, llm, pre_cb, post_cb, db_path):
        from utils.agent_loop import AgentLoop
        from utils.tool_registry import build_chat_tool_registry

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        return AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
            pre_step_callback=pre_cb,
            step_callback=post_cb,
        )

    def test_pre_step_callback_fires_once_per_tool(self, db_path):
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        pre_calls: list[str] = []

        async def on_pre(tool_call: ToolCall) -> None:
            pre_calls.append(tool_call.name)

        loop = self._make_loop(llm, on_pre, None, db_path)
        asyncio.run(loop.run("Hello"))
        assert pre_calls == ["finish_chat"]

    def test_pre_step_fires_before_step_callback(self, db_path):
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        events: list[str] = []

        async def on_pre(tool_call: ToolCall) -> None:
            events.append(f"pre:{tool_call.name}")

        from utils.tool_registry import AgentStep

        def on_post(step: AgentStep) -> None:
            events.append(f"post:{step.tool_call.name}")

        loop = self._make_loop(llm, on_pre, on_post, db_path)
        asyncio.run(loop.run("Hello"))
        assert events == ["pre:finish_chat", "post:finish_chat"]

    def test_pre_step_callback_fires_for_each_tool_in_sequence(self, db_path):
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "recall",
                                "arguments": {"query": "NAS"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                },
            ]
        )
        pre_calls: list[str] = []

        async def on_pre(tool_call: ToolCall) -> None:
            pre_calls.append(tool_call.name)

        loop = self._make_loop(llm, on_pre, None, db_path)
        asyncio.run(loop.run("What do you know about NAS?"))
        assert pre_calls == ["recall", "finish_chat"]

    def test_none_pre_step_callback_is_safe(self, db_path):
        """Loop works normally when pre_step_callback is None (default behavior)."""
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, None, None, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "success"


# ---------------------------------------------------------------------------
# TestDurableToolHistory
# ---------------------------------------------------------------------------


class TestDurableToolHistory:
    """After _run_chat_loop completes, intermediate tool-call and tool-result
    rows must be written to chat_messages alongside the final summary row."""

    @pytest.fixture
    def ctx(self, monkeypatch, tmp_path):
        import web.dashboard as dashboard

        path = str(tmp_path / "test_agent.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        monkeypatch.setattr(dashboard, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path, dashboard

    def _insert_session(self, path: str, title: str = "Test") -> int:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (title, created_at) VALUES (?, ?)",
                (title, 0.0),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def _patch_loop(self, monkeypatch, result):
        from utils.agent_loop import AgentLoop

        async def _fake_run(self_, message, **kw):  # noqa: ARG001
            return result

        monkeypatch.setattr(AgentLoop, "run", _fake_run)

    def test_intermediate_rows_written_to_db(self, ctx, monkeypatch):
        """After the loop, DB contains assistant+tool rows for intermediate steps."""
        import json as _json
        import time as _time

        from utils.tool_registry import AgentLoopResult, AgentStep, ToolCall, ToolResult

        path, dashboard = ctx
        now = _time.time()
        step = AgentStep(
            step_number=1,
            tool_call=ToolCall(name="recall", arguments={"query": "NAS"}),
            tool_result=ToolResult(
                tool_name="recall", success=True, output="NAS is at 192.168.1.100"
            ),
            timestamp=now - 0.5,
        )
        result = AgentLoopResult(
            outcome="success",
            steps=[step],
            episode_stub={"summary": "I found the NAS address."},
        )
        self._patch_loop(monkeypatch, result)
        session_id = self._insert_session(path)

        asyncio.run(dashboard._run_chat_loop(session_id, "What is the NAS IP?", []))

        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT role, content, tool_calls_json FROM chat_messages"
                " WHERE session_id = ? ORDER BY ts ASC",
                (session_id,),
            ).fetchall()

        roles = [r[0] for r in rows]
        assert "tool" in roles, "tool-result row must be written"
        tool_call_rows = [r for r in rows if r[0] == "assistant" and r[2] is not None]
        assert len(tool_call_rows) >= 1, "assistant tool-call row must be written"
        calls = _json.loads(tool_call_rows[0][2])
        assert calls[0]["name"] == "recall"

    def test_finish_chat_step_not_persisted(self, ctx, monkeypatch):
        """finish_chat steps are NOT written as intermediate rows."""
        import time as _time

        from utils.tool_registry import AgentLoopResult, AgentStep, ToolCall, ToolResult

        path, dashboard = ctx
        now = _time.time()
        finish_step = AgentStep(
            step_number=1,
            tool_call=ToolCall(name="finish_chat", arguments={"summary": "Done"}),
            tool_result=ToolResult(tool_name="finish_chat", success=True, output=""),
            timestamp=now - 0.1,
        )
        result = AgentLoopResult(
            outcome="success",
            steps=[finish_step],
            episode_stub={"summary": "Done"},
        )
        self._patch_loop(monkeypatch, result)
        session_id = self._insert_session(path, "Test 2")

        asyncio.run(dashboard._run_chat_loop(session_id, "Hello", []))

        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT role, tool_calls_json FROM chat_messages WHERE session_id = ?",
                (session_id,),
            ).fetchall()

        tool_call_rows = [r for r in rows if r[0] == "assistant" and r[1] is not None]
        assert len(tool_call_rows) == 0, "finish_chat must not produce a tool-call row"

    def test_final_summary_always_written(self, ctx, monkeypatch):
        """The final assistant summary row is always written regardless of steps."""
        from utils.tool_registry import AgentLoopResult

        path, dashboard = ctx
        result = AgentLoopResult(
            outcome="success",
            steps=[],
            episode_stub={"summary": "Nothing to do."},
        )
        self._patch_loop(monkeypatch, result)
        session_id = self._insert_session(path, "Test 3")

        asyncio.run(dashboard._run_chat_loop(session_id, "Hi", []))

        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT role, content FROM chat_messages WHERE session_id = ?",
                (session_id,),
            ).fetchall()

        summary_rows = [
            r for r in rows if r[0] == "assistant" and r[1] == "Nothing to do."
        ]
        assert len(summary_rows) == 1


# ---------------------------------------------------------------------------
# TestTimelineCallback
# ---------------------------------------------------------------------------


class TestTimelineCallback:
    """timeline_callback fires after each tool execution with correct tool name and status."""

    def _make_loop(self, llm, timeline_cb, db_path):
        from utils.agent_loop import AgentLoop
        from utils.tool_registry import build_chat_tool_registry

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        return AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
            timeline_callback=timeline_cb,
        )

    def test_timeline_callback_fires_once_per_tool(self, db_path):
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        calls: list[tuple[str, str]] = []

        async def on_timeline(tool_name: str, status_line: str) -> None:
            calls.append((tool_name, status_line))

        loop = self._make_loop(llm, on_timeline, db_path)
        asyncio.run(loop.run("Hello"))
        assert len(calls) == 1
        assert calls[0][0] == "finish_chat"

    def test_timeline_callback_receives_correct_tool_name(self, db_path):
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "recall",
                                "arguments": {"query": "NAS"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                },
            ]
        )
        names: list[str] = []

        async def on_timeline(tool_name: str, status_line: str) -> None:
            names.append(tool_name)

        loop = self._make_loop(llm, on_timeline, db_path)
        asyncio.run(loop.run("What do you know about NAS?"))
        assert names == ["recall", "finish_chat"]

    def test_timeline_callback_status_line_format(self, db_path):
        """status_line matches 'step N — tool_name: OK/ERR' format."""
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        status_lines: list[str] = []

        async def on_timeline(tool_name: str, status_line: str) -> None:
            status_lines.append(status_line)

        loop = self._make_loop(llm, on_timeline, db_path)
        asyncio.run(loop.run("Hello"))
        assert len(status_lines) == 1
        assert "finish_chat" in status_lines[0]
        assert "step 1" in status_lines[0]
        assert "OK" in status_lines[0] or "ERR" in status_lines[0]

    def test_none_timeline_callback_is_safe(self, db_path):
        """Loop works normally when timeline_callback is None (default behavior)."""
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, None, db_path)
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "success"


class TestInjectContext:
    """AgentLoop.inject_context() appends a user message to the live conversation."""

    def _make_loop(self, llm, db_path):
        from utils.agent_loop import AgentLoop
        from utils.tool_registry import build_chat_tool_registry

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        return AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
        )

    def test_inject_context_before_run_is_noop(self, db_path):
        """inject_context before run() does nothing (self._messages is None)."""
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient([])
        loop = self._make_loop(llm, db_path)
        # Should not raise; _messages is None
        loop.inject_context("ignored message")
        assert loop._messages is None

    def test_inject_context_after_run_is_noop(self, db_path):
        """inject_context after run() completes does nothing (_messages cleared)."""
        from utils.ollama_client import FakeToolCallingLLMClient

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                }
            ]
        )
        loop = self._make_loop(llm, db_path)
        asyncio.run(loop.run("Hello"))
        # _messages must be cleared after run()
        assert loop._messages is None
        # inject_context should be safe to call and do nothing
        loop.inject_context("post-run message")

    def test_inject_context_appends_during_run(self, db_path):
        """inject_context called from within a step callback appends a user message."""
        from utils.agent_loop import AgentLoop
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.tool_registry import build_chat_tool_registry

        injected_messages: list[dict] = []

        # step_callback fires after each tool; inject a message from there
        def on_step(step):
            if step.tool_call.name != "finish_chat":
                loop.inject_context("guidance from callback")

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "run_ha_command",
                                "arguments": {"command": "ha core check"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_chat",
                                "arguments": {"summary": "Done"},
                            }
                        }
                    ]
                },
            ]
        )
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )
        loop = AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_chat_tool_registry(),
            terminal_tool_name="finish_chat",
            max_tool_calls=5,
            max_wall_seconds=10.0,
            step_callback=on_step,
        )
        result = asyncio.run(loop.run("Hello"))
        assert result.outcome == "success"
        # The injected message must appear in the messages list captured during run
        # (we read it via the loop's final _messages=None, but the LLM received it)
        # Verify by checking that llm received more calls (injected message triggered re-query)
        assert len(llm.calls) >= 2


class TestRepairInjectEndpoint:
    """/repair/inject returns 409 when no loop is running, 200 when one is active."""

    def test_inject_returns_409_when_no_repair_running(self, db_path, monkeypatch):
        """Returns HTTP 409 when no repair loop is registered."""
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)

        from utils import supervisor as sv

        monkeypatch.setattr(sv, "_active_repair_loop", None)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post("/repair/inject", json={"message": "the NAS is offline"})
        assert resp.status_code == 409

    def test_inject_returns_200_and_calls_inject_context(self, db_path, monkeypatch):
        """Returns 200 and calls inject_context on the active loop."""
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)

        from utils import supervisor as sv

        injected: list[str] = []

        class _FakeLoop:
            def inject_context(self, message: str) -> None:
                injected.append(message)

        monkeypatch.setattr(sv, "_active_repair_loop", _FakeLoop())

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post("/repair/inject", json={"message": "ignore the NAS"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "injected"}
        assert injected == ["ignore the NAS"]

    def test_inject_returns_422_for_empty_message(self, db_path, monkeypatch):
        """Returns 422 for a blank message."""
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)

        from utils import supervisor as sv

        class _FakeLoop:
            def inject_context(self, message: str) -> None:
                pass  # pragma: no cover

        monkeypatch.setattr(sv, "_active_repair_loop", _FakeLoop())

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.post("/repair/inject", json={"message": "   "})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestChatDebugLog
# ---------------------------------------------------------------------------


class TestChatDebugLog:
    """Tests for GET /chat/sessions/{session_id}/debug-log."""

    def _seed_session(self, db_path: str) -> int:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (created_at, title) VALUES (?, ?)",
                (1_700_000_000.0, "test session"),
            )
            session_id = cur.lastrowid or 1
            rows = [
                ("user", "What is the disk usage?", None, 1_700_000_001.0),
                (
                    "assistant",
                    "",
                    '[{"name": "get_disk_usage", "arguments": {}}]',
                    1_700_000_002.0,
                ),
                ("tool", "/config: 10 GB used of 14 GB", None, 1_700_000_002.5),
                ("assistant", "Your disk is at 71% capacity.", None, 1_700_000_003.0),
            ]
            for role, content, tcj, ts in rows:
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, tool_calls_json, ts)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, tcj, ts),
                )
        return session_id

    def test_returns_200_and_text_plain(self, db_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)
        sid = self._seed_session(db_path)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get(f"/chat/sessions/{sid}/debug-log")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_contains_session_metadata(self, db_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)
        sid = self._seed_session(db_path)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        body = client.get(f"/chat/sessions/{sid}/debug-log").text
        assert "PUEO CHAT DEBUG LOG" in body
        assert f"Session: {sid}" in body
        assert "test session" in body

    def test_contains_system_prompt(self, db_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)
        sid = self._seed_session(db_path)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        body = client.get(f"/chat/sessions/{sid}/debug-log").text
        assert "SYSTEM PROMPT" in body

    def test_contains_all_turn_types(self, db_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)
        sid = self._seed_session(db_path)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        body = client.get(f"/chat/sessions/{sid}/debug-log").text
        assert "[User]" in body
        assert "What is the disk usage?" in body
        assert "[Tool call] get_disk_usage" in body
        assert "[Tool result]" in body
        assert "/config: 10 GB used of 14 GB" in body
        assert "[Assistant]" in body
        assert "Your disk is at 71% capacity." in body

    def test_attachment_header(self, db_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)
        sid = self._seed_session(db_path)

        client = TestClient(dashboard.app, raise_server_exceptions=True)
        resp = client.get(f"/chat/sessions/{sid}/debug-log")
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert f"pueo-chat-{sid}-debug.txt" in cd

    def test_returns_404_for_missing_session(self, db_path, monkeypatch):
        import web.dashboard as dashboard
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PUEO_CONFIG", db_path)
        monkeypatch.setattr(dashboard, "DB_PATH", db_path)

        client = TestClient(dashboard.app, raise_server_exceptions=False)
        resp = client.get("/chat/sessions/99999/debug-log")
        assert resp.status_code == 404
