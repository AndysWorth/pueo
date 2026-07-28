"""Tool registry for the agent tool-calling loop (Phase 14 / items 42–43).

Pydantic schemas: ToolDefinition, ToolCall, ToolResult, AgentStep, AgentLoopResult.
ToolRegistry: maintains tool definitions and produces Ollama-compatible JSON schemas.
Standard tool definitions for both HA and NetAlertX pipelines.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Pydantic schemas — item 42
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    output: str
    error: str | None = None


class AgentStep(BaseModel):
    step_number: int
    tool_call: ToolCall
    tool_result: ToolResult
    timestamp: float


AgentLoopOutcome = Literal["success", "exhausted", "timeout", "fix_failed"]


class AgentLoopResult(BaseModel):
    outcome: AgentLoopOutcome
    steps: list[AgentStep] = []
    episode_stub: dict[str, Any] | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Maintains tool definitions and produces Ollama-compatible schemas."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get_ollama_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ---------------------------------------------------------------------------
# Tool definitions — item 42
# ---------------------------------------------------------------------------

READ_CONFIG = ToolDefinition(
    name="read_config",
    description="Fetch a Home Assistant config file via SFTP.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Remote path to the config file (e.g. /config/configuration.yaml)",
            }
        },
        "required": ["path"],
    },
)

READ_LOGS = ToolDefinition(
    name="read_logs",
    description="Tail N lines from the HA supervisor journal over SSH.",
    parameters={
        "type": "object",
        "properties": {
            "lines": {
                "type": "integer",
                "description": "Number of lines to tail (default 100)",
            }
        },
        "required": [],
    },
)

RUN_HA_COMMAND = ToolDefinition(
    name="run_ha_command",
    description=(
        "Run an allowlisted HA CLI subcommand and return stdout. "
        "Allowed commands: ha core check, ha core restart, ha core stop, "
        "ha host info, ha backups list, ha backups new, ha apps list, ha os info."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The full ha CLI command to run",
            }
        },
        "required": ["command"],
    },
)

READ_FILE = ToolDefinition(
    name="read_file",
    description=(
        "Read an arbitrary remote file. " "Allowed directories: /config/, /backup/."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Remote file path (must be under /config/ or /backup/)",
            }
        },
        "required": ["path"],
    },
)

QUERY_NETALERTX = ToolDefinition(
    name="query_netalertx",
    description="Fetch NetAlertX health, device, or event data via the API.",
    parameters={
        "type": "object",
        "properties": {
            "query_type": {
                "type": "string",
                "enum": ["health", "devices", "events"],
                "description": "What data to fetch from NetAlertX",
            }
        },
        "required": ["query_type"],
    },
)

APPLY_FIX = ToolDefinition(
    name="apply_fix",
    description=(
        "Write proposed YAML to the HA config sandbox, validate via 'ha core check', "
        "then atomically swap to production. Always triggers a backup first. "
        "May be called at most once per loop run. "
        "Requires HITL approval unless autonomy level permits auto-execution."
    ),
    parameters={
        "type": "object",
        "properties": {
            "yaml_content": {
                "type": "string",
                "description": "The complete corrected YAML to write to production",
            },
            "description": {
                "type": "string",
                "description": "Brief description of what this fix changes",
            },
        },
        "required": ["yaml_content", "description"],
    },
)

VERIFY_FIX = ToolDefinition(
    name="verify_fix",
    description="Run 'ha core check' and return pass/fail status.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

FINISH_REPAIR = ToolDefinition(
    name="finish_repair",
    description=(
        "Signal that the repair session is complete. "
        "Call when you are done investigating and/or applying a fix."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Summary of what was found and what action was taken",
            },
            "action_taken": {
                "type": "string",
                "enum": ["fixed", "no_fix_needed", "fix_failed", "needs_human"],
                "description": "Outcome of the repair attempt",
            },
        },
        "required": ["summary", "action_taken"],
    },
)

QUERY_KNOWLEDGE = ToolDefinition(
    name="query_knowledge",
    description="Query the local RAG knowledge base for HA breaking changes and HACS changelogs.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for the knowledge base",
            }
        },
        "required": ["query"],
    },
)

# NetAlertX-specific tools used by the NetAlertX healer loop
RESTART_NETALERTX = ToolDefinition(
    name="restart_netalertx",
    description="Restart the NetAlertX Docker container and trigger a network rescan.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

REWRITE_NETALERTX_CONF = ToolDefinition(
    name="rewrite_netalertx_conf",
    description=(
        "Apply KEY=VALUE overrides to the NetAlertX app.conf file. "
        "Validates the result before writing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "overrides": {
                "type": "object",
                "description": "Dict of KEY: value pairs to set in app.conf",
                "additionalProperties": {"type": "string"},
            }
        },
        "required": ["overrides"],
    },
)


def build_ha_tool_registry() -> ToolRegistry:
    """Standard HA repair registry: all tools except NetAlertX-specific ones."""
    reg = ToolRegistry()
    for tool in (
        READ_CONFIG,
        READ_LOGS,
        RUN_HA_COMMAND,
        READ_FILE,
        QUERY_NETALERTX,
        APPLY_FIX,
        VERIFY_FIX,
        FINISH_REPAIR,
        QUERY_KNOWLEDGE,
    ):
        reg.register(tool)
    return reg


def build_netalertx_tool_registry() -> ToolRegistry:
    """NetAlertX healer registry: investigation + NetAlertX-specific fix tools."""
    reg = ToolRegistry()
    for tool in (
        READ_LOGS,
        QUERY_NETALERTX,
        APPLY_FIX,
        RESTART_NETALERTX,
        REWRITE_NETALERTX_CONF,
        FINISH_REPAIR,
    ):
        reg.register(tool)
    return reg
