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
    awaiting_approval: bool = False


class AgentStep(BaseModel):
    step_number: int
    tool_call: ToolCall
    tool_result: ToolResult
    timestamp: float


AgentLoopOutcome = Literal[
    "success", "exhausted", "timeout", "fix_failed", "awaiting_approval"
]


class AgentLoopResult(BaseModel):
    outcome: AgentLoopOutcome
    steps: list[AgentStep] = []
    episode_stub: dict[str, Any] | None = None
    episode_id: str | None = None
    capability_gap: bool = False
    gap_description: str = ""


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
        "Allowed commands: ha core check (takes 45-60s — use only when necessary), "
        "ha core restart, ha core stop, ha host info, ha backups list, "
        "ha apps list, ha os info. "
        "To create a backup use trigger_backup instead."
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

TRIGGER_BACKUP = ToolDefinition(
    name="trigger_backup",
    description=(
        "Create a full HA backup via the Pueo backup chain: "
        "triggers ha backup new with a timestamped name, records the slug in the "
        "local backup registry, SFTP-offloads the .tar to the local backups/ "
        "directory with SHA-256 verification, enforces HA retention, and purges "
        "old local archives. Use this instead of run_ha_command for backups."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
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
        "Call when you are done investigating and/or applying a fix. "
        "Set capability_gap=true and describe the gap when the needed tool or capability "
        "does not exist — this automatically triggers a code-proposal flow."
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
            "capability_gap": {
                "type": "boolean",
                "description": (
                    "Set to true when the agent encountered a failure mode for which "
                    "no existing tool or capability exists. Triggers automatic code-proposal."
                ),
            },
            "gap_description": {
                "type": "string",
                "description": (
                    "Plain-English description of the missing capability: what tool or "
                    "action would be needed to resolve the issue."
                ),
            },
        },
        "required": ["summary", "action_taken"],
    },
)

QUERY_KNOWLEDGE = ToolDefinition(
    name="query_knowledge",
    description=(
        "Query the local RAG knowledge base for HA breaking changes, integration docs, "
        "and HACS changelogs. Pass integration_filter to scope results to specific domains."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query text",
            },
            "integration_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of integration domains to filter results "
                    "(e.g. ['zha', 'mqtt'])"
                ),
            },
        },
        "required": ["query"],
    },
)

# ---------------------------------------------------------------------------
# Conversational agent tools (items 66–71)
# ---------------------------------------------------------------------------

REMEMBER = ToolDefinition(
    name="remember",
    description="Store a named piece of information in persistent agent memory.",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Short label for this memory entry",
            },
            "content": {
                "type": "string",
                "description": "The information to remember",
            },
        },
        "required": ["key", "content"],
    },
)

RECALL = ToolDefinition(
    name="recall",
    description=(
        "Search persistent agent memory by keyword. "
        "Returns matching entries ordered by most recent first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or phrase to search for in stored memories",
            }
        },
        "required": ["query"],
    },
)

GET_HA_PROFILE = ToolDefinition(
    name="get_ha_profile",
    description=(
        "Return the current HA environment profile: installed version, OS version, "
        "installed integrations, HACS components, and top-level config keys."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)

GET_DISK_USAGE = ToolDefinition(
    name="get_disk_usage",
    description=(
        "Fetch the current HA disk usage breakdown: total/free space and per-path "
        "sizes for backups, config & DB, addon data, and shared storage. "
        "Call this first when answering any question about disk space or storage."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)

FINISH_CHAT = ToolDefinition(
    name="finish_chat",
    description=(
        "Signal that the chat response is complete. "
        "Call with a plain-language summary of what you found or did."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Plain-language summary of findings or actions taken",
            }
        },
        "required": ["summary"],
    },
)

SWITCH_MODEL = ToolDefinition(
    name="switch_model",
    description=(
        "Switch the active Ollama model used for reasoning. "
        "Omit model_name to auto-select the best model for this hardware. "
        "Pass model_name to switch to a specific installed model. "
        "The change takes effect immediately and persists to config.yaml."
    ),
    parameters={
        "type": "object",
        "properties": {
            "model_name": {
                "type": "string",
                "description": (
                    "Exact Ollama model name (e.g. 'qwen2.5-coder:32b'). "
                    "Omit to auto-select based on hardware."
                ),
            }
        },
        "required": [],
    },
)

# Code sandbox tools — ToolDefinitions defined here; executor methods in items 70–71.

READ_SOURCE = ToolDefinition(
    name="read_source",
    description=(
        "Read a source file from the Pueo repository. "
        "Allowed extensions: .py, .yaml, .md, .toml, .txt. "
        "Returns up to 8000 characters."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative path to read (e.g. utils/tool_registry.py)",
            }
        },
        "required": ["path"],
    },
)

PROPOSE_PATCH = ToolDefinition(
    name="propose_patch",
    description=(
        "Stage a proposed change to a Pueo source file. "
        "The patch is not applied to the live tree until sandbox_code passes "
        "and add_tool is called and approved."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative path to modify",
            },
            "content": {
                "type": "string",
                "description": "Complete new content for the file",
            },
        },
        "required": ["path", "content"],
    },
)

SANDBOX_CODE = ToolDefinition(
    name="sandbox_code",
    description=(
        "Run the CI gate (black, flake8, mypy, pytest) against the pending patch "
        "in a temporary copy of the repo. Must be called before add_tool. "
        "Returns combined output and pass/fail status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Brief description of what the patch does",
            }
        },
        "required": ["description"],
    },
)

ADD_TOOL = ToolDefinition(
    name="add_tool",
    description=(
        "Register a new tool from the pending patch. "
        "Requires sandbox_code to have passed and CHAT_ALLOW_TOOL_REGISTRATION=true. "
        "Queues a HITL approval card; the tool is available after the user approves."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Tool name (snake_case, unique)",
            },
            "description": {
                "type": "string",
                "description": "One-sentence description of what the tool does",
            },
            "parameters_schema": {
                "type": "string",
                "description": "JSON schema string for the tool's parameters",
            },
            "code": {
                "type": "string",
                "description": "Python source for the tool implementation",
            },
        },
        "required": ["name", "description", "parameters_schema", "code"],
    },
)

OPEN_PR = ToolDefinition(
    name="open_pr",
    description=(
        "Open a GitHub pull request for the pending patch after sandbox CI has passed. "
        "Requires sandbox_code to have passed first. "
        "Queues a HITL approval card; the PR is opened after the user approves."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "PR title (one short sentence)",
            },
            "reason": {
                "type": "string",
                "description": "Why this change is needed (the 'why', not the 'what')",
            },
            "branch_name": {
                "type": "string",
                "description": (
                    "Git branch name to create (e.g. feat/add-foo-tool). "
                    "Auto-derived from title if omitted."
                ),
            },
        },
        "required": ["title", "reason"],
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
        TRIGGER_BACKUP,
        QUERY_NETALERTX,
        APPLY_FIX,
        VERIFY_FIX,
        FINISH_REPAIR,
        QUERY_KNOWLEDGE,
    ):
        reg.register(tool)
    return reg


def build_code_proposal_registry() -> ToolRegistry:
    """Registry for the autonomous code-proposal loop (item 84).

    Used when a repair loop finishes with capability_gap=True.  The loop
    reads relevant source, proposes a patch, validates it in the sandbox,
    and queues an open_pr HITL card for human review.
    """
    reg = ToolRegistry()
    for tool in (
        READ_SOURCE,
        PROPOSE_PATCH,
        SANDBOX_CODE,
        OPEN_PR,
        FINISH_REPAIR,
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


def build_chat_tool_registry() -> ToolRegistry:
    """Conversational agent registry.

    Excludes apply_fix and verify_fix (chat sessions do not write to HA config).
    Includes memory, code-sandbox, and dynamic-tool-registration tools.
    """
    reg = ToolRegistry()
    for tool in (
        READ_CONFIG,
        READ_LOGS,
        RUN_HA_COMMAND,
        READ_FILE,
        TRIGGER_BACKUP,
        QUERY_KNOWLEDGE,
        QUERY_NETALERTX,
        REMEMBER,
        RECALL,
        GET_HA_PROFILE,
        GET_DISK_USAGE,
        READ_SOURCE,
        PROPOSE_PATCH,
        SANDBOX_CODE,
        ADD_TOOL,
        OPEN_PR,
        SWITCH_MODEL,
        FINISH_CHAT,
    ):
        reg.register(tool)
    return reg
