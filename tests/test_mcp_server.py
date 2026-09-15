"""Tests for Pueo MCP server — dispatch, auth, and config.

All tests avoid importing the `mcp` package; they test `_dispatch` and `_check_auth`
directly, which have no mcp imports, plus the tool inventory constants.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from utils.mcp.pueo_mcp_server import PueoMCPServer, _MCP_TOOL_NAMES, _check_auth


# ---------------------------------------------------------------------------
# TestMCPToolInventory
# ---------------------------------------------------------------------------


class TestMCPToolInventory:
    def test_mcp_tools_are_subset_of_chat_registry(self):
        from utils.agent.tool_registry import build_chat_tool_registry

        chat_names = set(build_chat_tool_registry().names())
        assert _MCP_TOOL_NAMES.issubset(
            chat_names
        ), f"MCP tools not in chat registry: {_MCP_TOOL_NAMES - chat_names}"

    def test_dangerous_tools_excluded(self):
        dangerous = {
            "apply_fix",
            "trigger_backup",
            "run_ha_command",
            "propose_patch",
            "sandbox_code",
            "add_tool",
            "open_pr",
            "execute_local_python",
        }
        assert dangerous.isdisjoint(
            _MCP_TOOL_NAMES
        ), f"Dangerous tools found in MCP set: {dangerous & _MCP_TOOL_NAMES}"

    def test_flow_control_tools_excluded(self):
        flow_control = {
            "finish_chat",
            "finish_repair",
            "finish_investigation",
            "discard_result",
            "resolve_hitl_card",
            "switch_model",
            "request_escalation",
            "save_runbook",
            "fetch_url",
        }
        assert flow_control.isdisjoint(
            _MCP_TOOL_NAMES
        ), f"Flow-control tools found in MCP set: {flow_control & _MCP_TOOL_NAMES}"


# ---------------------------------------------------------------------------
# TestMCPDispatch
# ---------------------------------------------------------------------------


class TestMCPDispatch:
    @pytest.fixture
    def executor(self):
        mock = MagicMock()
        mock.execute = AsyncMock()
        return mock

    @pytest.fixture
    def server(self, executor):
        return PueoMCPServer(executor=executor)

    def test_dispatch_known_tool_returns_output(self, server, executor):
        from utils.agent.tool_registry import ToolResult

        executor.execute.return_value = ToolResult(
            tool_name="query_knowledge", success=True, output="result text"
        )
        out = asyncio.run(server._dispatch("query_knowledge", {"query": "ZHA errors"}))
        assert out == "result text"
        executor.execute.assert_awaited_once()

    def test_dispatch_unknown_tool_returns_error(self, server, executor):
        out = asyncio.run(server._dispatch("apply_fix", {"yaml": ""}))
        assert "not available via MCP" in out
        executor.execute.assert_not_called()

    def test_dispatch_executor_failure_returns_error(self, server, executor):
        from utils.agent.tool_registry import ToolResult

        executor.execute.return_value = ToolResult(
            tool_name="get_disk_usage", success=False, output="", error="SSH timeout"
        )
        out = asyncio.run(server._dispatch("get_disk_usage", {}))
        assert "SSH timeout" in out

    def test_dispatch_executor_exception_returns_error(self, server, executor):
        executor.execute.side_effect = RuntimeError("connection refused")
        out = asyncio.run(server._dispatch("read_logs", {}))
        assert "Error executing read_logs" in out
        assert "connection refused" in out


# ---------------------------------------------------------------------------
# TestMCPAuth
# ---------------------------------------------------------------------------


class TestMCPAuth:
    def test_auth_passes_when_token_empty(self):
        assert _check_auth("", "") is True
        assert _check_auth("Bearer anything", "") is True

    def test_auth_passes_with_correct_token(self):
        assert _check_auth("Bearer secret123", "secret123") is True

    def test_auth_fails_with_wrong_token(self):
        assert _check_auth("Bearer wrong", "secret123") is False

    def test_auth_fails_with_missing_header(self):
        assert _check_auth("", "secret123") is False

    def test_auth_fails_with_malformed_header(self):
        assert _check_auth("Token secret123", "secret123") is False


# ---------------------------------------------------------------------------
# TestMCPConfig
# ---------------------------------------------------------------------------


class TestMCPConfig:
    def test_mcp_disabled_by_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.MCP_ENABLED is False

    def test_mcp_config_section_parsed(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"mcp": {"enabled": True, "port": 9000, "token": "tok"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.MCP_ENABLED is True
        assert config.MCP_PORT == 9000
        assert config.MCP_TOKEN == "tok"

    def test_mcp_port_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.MCP_PORT == 8765
