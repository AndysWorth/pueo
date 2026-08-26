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


class FixEnrichment(BaseModel):
    relevant_config_section: str
    explanation: str
    confidence: Literal["high", "medium", "low"]
    suggested_fix_summary: str | None = None


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
    "success", "exhausted", "timeout", "fix_failed", "awaiting_approval", "stuck"
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
    description=(
        "Tail N recent lines from an HA log source over SSH. "
        "log_source: 'ha_core' (default), 'ha_supervisor', 'ha_os', 'ha_host', or 'ha_app'. "
        "When log_source='ha_app', also pass addon_slug (e.g. 'core_mosquitto'). "
        "Use list_log_sources to discover valid addon slugs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "lines": {
                "type": "integer",
                "description": "Number of lines to tail (default 100, max 500)",
            },
            "log_source": {
                "type": "string",
                "enum": ["ha_core", "ha_supervisor", "ha_os", "ha_host", "ha_app"],
                "description": "Which HA log source to read (default 'ha_core')",
            },
            "addon_slug": {
                "type": "string",
                "description": "Required when log_source='ha_app' (e.g. 'core_mosquitto')",
            },
        },
        "required": [],
    },
)

LIST_LOG_SOURCES = ToolDefinition(
    name="list_log_sources",
    description=(
        "List all available HA log sources. Returns always-available system sources "
        "(ha_core, ha_supervisor, ha_os, ha_host) plus per-app sources discovered via "
        "'ha apps list'. Use the returned slugs with search_log or read_logs."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
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
        "Requires approval unless autonomy level permits auto-execution."
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
        "Return HA environment info. With no arguments, returns a compact summary "
        "(version, OS, Supervisor, config key names, and counts of integrations/config entries). "
        "Pass field= to retrieve one large list in full."
    ),
    parameters={
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "enum": [
                    "installed_integrations",
                    "hacs_integrations",
                    "config_entries",
                ],
                "description": (
                    "Optional. Which large list to retrieve in full. "
                    "Omit to get the compact summary."
                ),
            }
        },
        "required": [],
    },
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
        "Queues an approval card; the tool is available after the user approves."
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
        "Queues an approval card; the PR is opened after the user approves."
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

FETCH_HA_DOCS = ToolDefinition(
    name="fetch_ha_docs",
    description=(
        "Fetch a Home Assistant component source file from the local cache "
        "(or live from GitHub in cloud/both mode). "
        "Allowed filenames: __init__.py, manifest.json, config_flow.py, const.py, "
        "strings.json, and any *.md file. "
        "Use this to look up a component's actual implementation when the knowledge "
        "base doesn't have enough detail (e.g. valid config key values in const.py)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "HA integration domain (e.g. 'zha', 'mqtt', 'hue')",
            },
            "filename": {
                "type": "string",
                "description": (
                    "File to fetch: __init__.py, manifest.json, config_flow.py, "
                    "const.py, strings.json, or a *.md filename"
                ),
            },
        },
        "required": ["domain", "filename"],
    },
)

FETCH_URL = ToolDefinition(
    name="fetch_url",
    description=(
        "Perform an HTTP GET to an external URL to verify a diagnosis or confirm that "
        "an external service has recovered. GET only. Blocked for private/loopback "
        "addresses. Response truncated at 8,000 characters. Requires "
        "ALLOW_DIAGNOSTIC_WAN=true (default)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch (https:// or http://)",
            },
        },
        "required": ["url"],
    },
)

INVESTIGATE_DEVICE = ToolDefinition(
    name="investigate_device",
    description=(
        "Enrich a source IP address with all available device context: "
        "reverse DNS hostname, MAC address (from ARP table), OUI vendor name, "
        "randomized-MAC flag, NetAlertX device name, HA device registry name, "
        "and DHCP hostname from the gateway. "
        "Use this when a user asks about a device that attempted to log in or "
        "triggered a security notification."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ip": {
                "type": "string",
                "description": "IPv4 address to investigate (e.g. '192.168.1.42')",
            },
        },
        "required": ["ip"],
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


SAVE_STRATEGY = ToolDefinition(
    name="save_strategy",
    description=(
        "Record a novel investigation or repair approach in the strategies knowledge base "
        "so future sessions can retrieve it via query_knowledge. "
        "Call when you used an approach that worked and was not already in the knowledge base."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short descriptive title (e.g. 'Diagnosing ZHA coordinator crash')",
            },
            "trigger_pattern": {
                "type": "string",
                "description": (
                    "Pattern or symptom that triggers this strategy "
                    "(e.g. 'ZHA integration unavailable after HA restart')"
                ),
            },
            "approach": {
                "type": "string",
                "description": "Step-by-step description of what worked and why",
            },
        },
        "required": ["title", "trigger_pattern", "approach"],
    },
)

READ_PUEO_LOG = ToolDefinition(
    name="read_pueo_log",
    description=(
        "Read recent lines from Pueo's own logs. "
        "Default reads the structured JSON log (loop crashes, stream resets, etc.). "
        "Pass filename='pueo-stderr.log' to read raw uvicorn/FastAPI stderr output "
        "(ASGI exceptions, startup errors)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "lines": {
                "type": "integer",
                "description": "Number of recent lines to return (default 100, max 500)",
            },
            "level": {
                "type": "string",
                "enum": ["ERROR", "WARNING", "INFO"],
                "description": "Filter to only lines at this log level or above",
            },
            "filename": {
                "type": "string",
                "enum": ["pueo.log", "pueo-stderr.log"],
                "description": "Which Pueo log file to read (default 'pueo.log')",
            },
        },
        "required": [],
    },
)

SEARCH_LOG = ToolDefinition(
    name="search_log",
    description=(
        "Search a log for lines matching a regex pattern. Returns matching lines with context. "
        "Sources: 'pueo' (Pueo JSON log), 'pueo_stderr' (Pueo uvicorn/ASGI stderr), "
        "'ha_core' (HA Core journal), 'ha_supervisor' (HA Supervisor daemon), "
        "'ha_os' (HassOS OS log), 'ha_host' (host-level log), "
        "'ha_app' (add-on log — also pass addon_slug, e.g. 'core_mosquitto')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "log_name": {
                "type": "string",
                "enum": [
                    "pueo",
                    "pueo_stderr",
                    "ha_core",
                    "ha_supervisor",
                    "ha_os",
                    "ha_host",
                    "ha_app",
                ],
                "description": "Which log to search",
            },
            "pattern": {
                "type": "string",
                "description": "Regex pattern to match (case-insensitive)",
            },
            "addon_slug": {
                "type": "string",
                "description": "Add-on slug (required when log_name='ha_app', e.g. 'core_mosquitto')",
            },
            "context_lines": {
                "type": "integer",
                "description": "Number of surrounding lines to include with each match (default 2)",
            },
            "max_matches": {
                "type": "integer",
                "description": "Maximum number of matching lines to return (default 20)",
            },
        },
        "required": ["log_name", "pattern"],
    },
)

FINISH_DIAGNOSIS = ToolDefinition(
    name="finish_diagnosis",
    description=(
        "Call when the configuration analysis is complete. "
        "Provide a structured assessment of the configuration validity and issues found."
    ),
    parameters={
        "type": "object",
        "properties": {
            "is_valid": {
                "type": "boolean",
                "description": "True if the config has no structural or deprecated flaws",
            },
            "severity": {
                "type": "string",
                "enum": ["NONE", "LOW", "MEDIUM", "CRITICAL"],
                "description": "Overall severity: NONE, LOW, MEDIUM, or CRITICAL",
            },
            "identified_issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of specific flaws, deprecated formats, or risks found",
            },
            "recommended_fix_yaml": {
                "type": "string",
                "description": "Corrected YAML snippet if applicable, or null",
            },
        },
        "required": ["is_valid", "severity", "identified_issues"],
    },
)

FINISH_IMPACT_ANALYSIS = ToolDefinition(
    name="finish_impact_analysis",
    description=(
        "Call when the breaking-change impact analysis is complete. "
        "Provide a structured assessment of which breaking changes affect this installation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "affected_changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "applies": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "config_fix_yaml": {"type": "string"},
                        "fix_description": {"type": "string"},
                    },
                    "required": ["description", "applies", "reason"],
                },
                "description": "Breaking changes with their applicability assessment",
            },
            "instance_impact": {
                "type": "string",
                "enum": ["none", "low", "high"],
                "description": "'none', 'low', or 'high'",
            },
            "effective_safe_to_update": {
                "type": "boolean",
                "description": "True if the update can proceed safely given this config",
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary of impact on this installation",
            },
        },
        "required": [
            "affected_changes",
            "instance_impact",
            "effective_safe_to_update",
            "summary",
        ],
    },
)

FINISH_INSTALLER_DIAGNOSIS = ToolDefinition(
    name="finish_installer_diagnosis",
    description=(
        "Call this when the installer failure diagnosis is complete. "
        "Provide a structured analysis of the root cause and recommended action."
    ),
    parameters={
        "type": "object",
        "properties": {
            "primary_hypothesis": {
                "type": "string",
                "description": "Most likely cause in plain English",
            },
            "confidence": {
                "type": "number",
                "description": "Certainty 0.0–1.0. Below 0.6 means evidence is insufficient.",
            },
            "supporting_evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific observations that support the hypothesis. Cite exact output.",
            },
            "alternative_hypotheses": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Other possible causes not ruled out by the evidence.",
            },
            "recommended_action": {
                "type": "string",
                "description": "Concrete, specific action to resolve the issue.",
            },
            "can_auto_fix": {
                "type": "boolean",
                "description": "True only if the fix is a single SSH command with no side effects.",
            },
            "auto_fix_command": {
                "type": "string",
                "description": "The exact SSH command to run if can_auto_fix is True.",
            },
            "verification_command": {
                "type": "string",
                "description": "SSH command to run after the fix to confirm it worked.",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence summary of what was found.",
            },
        },
        "required": [
            "primary_hypothesis",
            "confidence",
            "supporting_evidence",
            "alternative_hypotheses",
            "recommended_action",
            "can_auto_fix",
            "summary",
        ],
    },
)

FINISH_HEALTH_DIAGNOSIS = ToolDefinition(
    name="finish_health_diagnosis",
    description=(
        "Call this when the NetAlertX health diagnosis is complete. "
        "Provide a structured analysis of the identified problem."
    ),
    parameters={
        "type": "object",
        "properties": {
            "issue": {
                "type": "string",
                "description": "Short description of the identified problem.",
            },
            "severity": {
                "type": "string",
                "description": "LOW | MEDIUM | HIGH | CRITICAL",
            },
            "category": {
                "type": "string",
                "description": "networking | mqtt | database | version | ha_integration",
            },
            "recommended_fix": {
                "type": "string",
                "description": "Concrete remediation steps, including relevant commands or config changes.",
            },
            "affected_netalertx_version": {
                "type": "string",
                "description": "NetAlertX version from the health report, or 'unknown'.",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence summary of the diagnosis.",
            },
        },
        "required": [
            "issue",
            "severity",
            "category",
            "recommended_fix",
            "affected_netalertx_version",
            "summary",
        ],
    },
)


def build_ha_tool_registry() -> ToolRegistry:
    """HA repair registry.

    QUERY_NETALERTX is intentionally excluded: the sandbox engine executor
    is not constructed with a NetAlertX API client, so the tool would always
    return an error. The chat and NetAlertX registries include it where the
    client is guaranteed to be present.
    """
    reg = ToolRegistry()
    for tool in (
        READ_CONFIG,
        READ_LOGS,
        RUN_HA_COMMAND,
        READ_FILE,
        TRIGGER_BACKUP,
        APPLY_FIX,
        VERIFY_FIX,
        FINISH_REPAIR,
        QUERY_KNOWLEDGE,
        READ_SOURCE,
        FETCH_HA_DOCS,
        FETCH_URL,
        INVESTIGATE_DEVICE,
        SAVE_STRATEGY,
        READ_PUEO_LOG,
        SEARCH_LOG,
        LIST_LOG_SOURCES,
        GET_HA_PROFILE,
    ):
        reg.register(tool)
    return reg


def build_code_proposal_registry() -> ToolRegistry:
    """Registry for the autonomous code-proposal loop (item 84).

    Used when a repair loop finishes with capability_gap=True.  The loop
    reads relevant source, proposes a patch, validates it in the sandbox,
    and queues an open_pr approval card for human review.
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
        QUERY_KNOWLEDGE,
        READ_SOURCE,
        FETCH_URL,
        INVESTIGATE_DEVICE,
        SAVE_STRATEGY,
        READ_PUEO_LOG,
        SEARCH_LOG,
        LIST_LOG_SOURCES,
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
        FETCH_HA_DOCS,
        PROPOSE_PATCH,
        SANDBOX_CODE,
        ADD_TOOL,
        OPEN_PR,
        SWITCH_MODEL,
        FETCH_URL,
        INVESTIGATE_DEVICE,
        RESTART_NETALERTX,
        REWRITE_NETALERTX_CONF,
        SAVE_STRATEGY,
        READ_PUEO_LOG,
        SEARCH_LOG,
        LIST_LOG_SOURCES,
        FINISH_CHAT,
    ):
        reg.register(tool)
    return reg


def build_installer_diagnosis_registry() -> ToolRegistry:
    """Focused registry for NetAlertX installer failure diagnosis.

    Contains only knowledge retrieval, log reading, strategy saving, and the
    terminal tool. SSH evidence is gathered by the caller before the loop starts
    and passed as initial context; the loop uses these tools to reason adaptively.
    """
    reg = ToolRegistry()
    for tool in (
        QUERY_KNOWLEDGE,
        READ_LOGS,
        SEARCH_LOG,
        READ_PUEO_LOG,
        LIST_LOG_SOURCES,
        SAVE_STRATEGY,
        FINISH_INSTALLER_DIAGNOSIS,
    ):
        reg.register(tool)
    return reg


def build_health_diagnosis_registry() -> ToolRegistry:
    """Focused registry for NetAlertX health diagnosis.

    The caller passes pre-assembled HealthReport context; the loop can search
    logs for additional evidence before calling finish_health_diagnosis.
    """
    reg = ToolRegistry()
    for tool in (
        QUERY_KNOWLEDGE,
        READ_LOGS,
        SEARCH_LOG,
        READ_PUEO_LOG,
        LIST_LOG_SOURCES,
        SAVE_STRATEGY,
        FINISH_HEALTH_DIAGNOSIS,
    ):
        reg.register(tool)
    return reg


def build_config_analysis_registry() -> ToolRegistry:
    """Focused registry for HA configuration analysis.

    The caller passes the main config YAML as initial context; the loop can
    follow !include directives with read_file, validate with ha core check,
    and query the knowledge base before calling finish_diagnosis.
    """
    reg = ToolRegistry()
    for tool in (
        READ_FILE,
        RUN_HA_COMMAND,
        QUERY_KNOWLEDGE,
        SAVE_STRATEGY,
        FINISH_DIAGNOSIS,
    ):
        reg.register(tool)
    return reg


def build_impact_analysis_registry() -> ToolRegistry:
    """Focused registry for HA breaking-change impact analysis.

    The caller passes breaking changes, installed integrations, and a config
    snippet as initial context; the loop can read config files, query HA docs,
    verify installed apps, and search the knowledge base before calling
    finish_impact_analysis.
    """
    reg = ToolRegistry()
    for tool in (
        READ_FILE,
        RUN_HA_COMMAND,
        FETCH_HA_DOCS,
        QUERY_KNOWLEDGE,
        SAVE_STRATEGY,
        FINISH_IMPACT_ANALYSIS,
    ):
        reg.register(tool)
    return reg
