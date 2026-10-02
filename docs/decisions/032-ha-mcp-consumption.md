# ADR 032 — HA MCP Consumption

## Status
Proposed

## Context

Pueo reaches Home Assistant through three hand-written clients (SSH, REST, WebSocket).
Several diagnostically useful capabilities — entity history, logbook, template rendering,
structured error log, automation traces, area registries — are not yet exposed as agent tools.

The question is whether consuming an HA MCP server is the right path to fill these gaps, or
whether extending the native clients is better.  Three candidates were evaluated; see
[docs/plan/ha-mcp-evaluation.md](../plan/ha-mcp-evaluation.md) for the full capability
matrix, constraint scorecard, and spike measurements.

The candidates were:

1. **Official `mcp_server` integration** (`/api/mcp`) — Assist-pipeline only, ~15 intent
   tools, entity-exposure-gated.  Covers zero of the identified diagnostic gaps.
2. **`homeassistant-ai/ha-mcp`** — 87 tools, broad coverage, but the full tool schema
   (~5,200 tokens) plus existing Pueo tool schemas (~2,400 tokens) would exceed the 8k
   token budget on small local models, leaving < 400 tokens for context.  A curated subset
   is possible but requires per-session filtering or a maintained fork.
3. **`mcp-homeassistant`** (`daedalus/mcp-homeassistant`, already in `.mcp.json`) — 19 tools
   via HA REST API, stdio transport, MIT licence, Python ≥ 3.11.  Schema token cost ~740
   tokens (measured in spike).  A curated read-only subset of 6 tools fills the five
   highest-priority diagnostic gaps.

## Decision

Add a read-only MCP client adapter for `mcp-homeassistant` that exposes a fixed allowlist
of 6 read tools as standard Pueo agent tools.

Specifically:

- Add `utils/ha/mcp_client.py` — wraps the `mcp` SDK `Client` (stdio transport) against the
  `mcp-homeassistant` server process.  Runs synchronously inside `asyncio.to_thread()`,
  consistent with the Ollama client pattern.
- Add `MCPClientProtocol` to `interfaces.py` and a `FakeMCPClient` for unit tests.
- Register 6 new `ToolDefinition` entries (see below) in `build_ha_tool_registry` and
  `build_chat_tool_registry`.
- **Writes stay native.** No MCP write tools are exposed.  `call_service`, `fire_event`,
  and `set_state` are never registered as agent tools via this path.

### Exposed MCP tools (6 read-only)

| MCP tool | New Pueo tool | Purpose |
|----------|---------------|---------|
| `get_history` | `get_entity_history` | State history for a single entity (default 24 h) |
| `get_logbook` | `get_logbook` | Human-readable event log for an entity |
| `get_error_log` | `get_system_error_log` | Structured HA error log (supplements SSH journal) |
| `render_template` | `render_ha_template` | Evaluate a Jinja2 template expression |
| `get_services` | *(inlined into `search_integrations`)* | Service domain enumeration |
| `check_config` | `check_ha_config` | Config validation (second-opinion, no SSH required) |

### Deferred to native WebSocket extension

The following gaps are better served by extending `HAWebSocketClient`.  Implementation is
deferred to separate issues:

- Automation / script execution traces
- Area, floor, and label registry reads
- Config-entry reload and diagnostics
- Event subscription

`call_service` as a gated write tool is also deferred and requires its own issue
(AutonomyGate + WorkItem + risk review).

## Rationale

**Token budget.**  The 8k context limit is the decisive constraint.  `mcp-homeassistant`'s
19-tool schema costs ~740 tokens; a 6-tool curated slice costs ~420 tokens.  `ha-mcp`'s
87-tool schema (~5,200 tokens) would crowd out context on every small-model session.

**Reuse of existing `mcp` SDK.**  The `mcp` package is already a `requirements.txt`
dependency (ADR 028).  Adding `mcp-homeassistant` adds no new transitive dependencies.

**Consistency with Protocol/Fake pattern.**  `MCPClientProtocol` + `FakeMCPClient` keeps
the unit test suite offline, consistent with ADR 007 and `interfaces.py`.

**Writes stay gated.**  The allowlist approach means no MCP write tool can reach HA state
through Pueo, even if the underlying server process exposes them.  This preserves ADR 002
(safety invariant) and ADR 026 (no concurrent HA writes).

**Official candidate excluded.**  The built-in `mcp_server` covers zero diagnostic gaps.

**`ha-mcp` excluded for now.**  Its richer read surface (traces, registries) is valuable,
but the token cost of its full schema is prohibitive.  If per-session tool filtering is
added upstream, it can be revisited.

## Consequences

- `requirements-dev.txt` gains no new entry (`mcp` is already present).
- `config.py`, `config.yaml.default`, and `setup.sh` gain `MCP_HA_ENABLED` (bool, default
  false) and `MCP_HA_SERVER_CMD` (string, default `"uvx mcp-homeassistant"`) following the
  triple-update rule (ADR 001).
- The `mcp-homeassistant` server process must be reachable at startup; if it is absent, the
  6 tools fail gracefully with a `ToolError` on first call (consistent with the offline
  behaviour of `fetch_ha_docs` in local mode).
- Token budget impact: +420 tokens of tool schemas per agent session when enabled.
- Six new agent tools appear in the chat UI and the live trace automatically.

## Related decisions

- [ADR 001 — Config centralization](001-config-centralization.md): triple-update rule applies to new config keys.
- [ADR 002 — Safety invariant](002-safety-invariant.md): writes stay native; MCP adapter is read-only.
- [ADR 026 — No concurrent LLM/HA](026-no-concurrent-llm-ha.md): MCP read tools do not need WorkQueue routing; `call_service` would if it were added later.
- [ADR 028 — Pueo MCP server](028-pueo-mcp-server.md): Pueo as MCP *server*; this ADR covers Pueo as MCP *client*.
- [docs/plan/ha-mcp-evaluation.md](../plan/ha-mcp-evaluation.md): full candidate research, spike measurements, and capability matrix.
