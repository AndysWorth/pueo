# ADR 028 — Pueo MCP Server

## Status
Accepted

## Context
Home Assistant's `mcp` client integration (shipped 2025) lets HA's own conversation agents
(Claude, OpenAI, etc.) call external MCP servers over SSE. Pueo already has a rich set of
diagnostic tools — KB query, disk usage, config/log readers, entity health — that would be
directly useful to HA Assist. Without an MCP server, users have to leave the HA assistant
and open the Pueo dashboard to get this information.

Three options were evaluated:

1. **MCP server exposing Pueo's tools** — allows HA Assist to call Pueo directly, no UI change needed
2. **Persistent HAWebSocketClient connection** — reduces SSH overhead for `build_environment_profile()`, but overhead is ~50ms per call and the function runs at most every 24 hours; dropped as not worth the complexity
3. **No action** — leaves a capability gap for users who want HA Assist integration

Option 1 was chosen.

## Decision
Add a `PueoMCPServer` class (`utils/mcp/pueo_mcp_server.py`) that:

- Runs as a standalone Starlette ASGI app on `MCP_PORT` (default 8765), separate from the
  dashboard which stays on `127.0.0.1`. The MCP server binds to `0.0.0.0` so HA can reach
  it over the LAN.
- Is started by `LoopSupervisor` alongside the dashboard when `MCP_ENABLED=true`.
- Exposes a curated read-heavy subset of 13 tools from the existing chat tool registry
  (see `_MCP_TOOL_NAMES`).
- Supports an optional Bearer token auth gate (`MCP_TOKEN`).
- Defers all `mcp` SDK imports into method bodies so Pueo starts normally when the package
  is not installed.

### Exposed tools (13)

| Tool | Purpose |
|---|---|
| `query_knowledge` | KB search — breaking changes, runbooks, strategies |
| `read_config` | Read HA configuration files |
| `read_logs` | Read HA log lines |
| `get_ha_profile` | HA version, installed integrations, OS version |
| `get_disk_usage` | HA filesystem disk usage |
| `check_entity_status` | Fetch the current state of one or more entities |
| `read_pueo_log` | Read Pueo's own log |
| `search_log` | Search Pueo log by pattern |
| `fetch_ha_docs` | HA component source/docs lookup (cache-first) |
| `remember` | Store a note in Pueo's memory store |
| `recall` | Retrieve notes from Pueo's memory store |
| `search_integrations` | Search installed integrations by name |
| `get_dashboard_entity_health` | Check Lovelace dashboards for missing/disabled entities |

### Excluded tools (never exposed via MCP)

- **HA state-changing**: `apply_fix`, `trigger_backup`, `run_ha_command`
- **Code modification**: `propose_patch`, `sandbox_code`, `add_tool`, `open_pr`
- **Arbitrary execution**: `execute_local_python`
- **Internal flow control**: `finish_*`, `discard_result`, `resolve_hitl_card`
- **Internal Pueo control plane**: `switch_model`, `request_escalation`, `save_runbook`
- **Unnecessary WAN exposure**: `fetch_url`

## Rationale

**Safety through read-only exposure.** The MCP server is a new ingress path — any HA
conversation agent that can reach the endpoint can call these tools. Restricting to the
read-heavy subset ensures no HA state can be changed through the MCP path, even if the token
is compromised or the endpoint is misconfigured.

**Reuse of `ToolExecutor.execute()`.** The MCP dispatch chain calls the same public
`ToolExecutor.execute(ToolCall(...))` method used by `AgentLoop`. There is no new execution
path — only a new transport layer. This means MCP tools get the same dependency injection,
auth gates, and error handling as agent tools.

**`_dispatch()` and `_check_auth()` are testable without the `mcp` package.** These two
functions contain all the interesting logic (tool allowlist, auth check, error surface) and
have no `mcp` imports. All 13 unit tests exercise them directly; no test needs `mcp` installed.

**Deferred imports prevent startup failures.** A missing `mcp` package produces a startup
warning, not a crash. The feature degrades gracefully when the optional dependency is absent.

## Consequences

- `requirements.txt` gains `mcp`; this installs `sse-starlette` and related transitive deps
- `config.py`, `config.yaml.default`, and `setup.sh` gain `MCP_ENABLED`, `MCP_PORT`,
  `MCP_TOKEN` (triple-update rule)
- `MCP_PORT` (8765) must be firewalled from the internet but reachable from the HA host
- An empty `MCP_TOKEN` means no authentication — appropriate for a trusted home LAN, but
  the setup wizard warns users to set a token if the port is exposed beyond the LAN
- `host="0.0.0.0"` is intentional and documented with `# nosec B104`

## Related decisions
- [ADR 010 — Agent self-awareness](010-agent-self-awareness.md): tools registered in the
  agent loop are the source of truth; MCP exposes a subset of the chat registry.
- [ADR 017 — Chat tool parity](017-chat-tool-parity.md): the MCP server calls
  `ToolExecutor.execute()` with the same client injection pattern as chat tools — any client
  available to chat is available to MCP.
- [ADR 011 — HA live lookup](011-ha-live-lookup.md): `fetch_ha_docs` enforces the same
  local-mode WAN gate when called via MCP as when called via agent loop.
