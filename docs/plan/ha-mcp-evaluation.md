# HA MCP Consumption Evaluation

**Date:** 2026-10-02  
**Issue:** [#729](https://github.com/AndysWorth/pueo/issues/729)  
**ADR:** [ADR 032](../decisions/032-ha-mcp-consumption.md)

## Motivation

Pueo reaches Home Assistant through three hand-written clients: SSH
(`utils/ha/ssh_client.py`), REST (`utils/ha/ha_rest_client.py`), and WebSocket
(`utils/ha/ha_ws_client.py`).  Several diagnostically useful HA capabilities are not yet
exposed as agent tools.  This document asks whether consuming an HA MCP server is the right
way to fill those gaps, or whether extending the native clients is the better path.

### Capability gaps in the current tool surface

| Gap | Current workaround |
|-----|--------------------|
| Entity history / logbook | none — not accessible |
| Automation execution traces | none |
| Area, floor, and label registry reads | none |
| `call_service` as an agent tool | none (REST client has the method, not wired to a tool) |
| Jinja2 template rendering | none |
| `system_log/list` (error log) | `read_logs` reads the supervisor journal; structured error log not accessible |
| Config-entry reload / diagnostics | none |
| Event subscription | none; everything polled |

---

## Candidates Evaluated

Three candidates were researched.  "HA-MCP" is ambiguous in the community; the evaluation
covers all three:

### 1. Official `mcp_server` integration (`/api/mcp`)

Built into Home Assistant Core since 2025.11.  Exposes the **Assist pipeline** via Streamable
HTTP on `/api/mcp`.

| Attribute | Value |
|-----------|-------|
| Transport | Streamable HTTP (stateless) |
| Auth | OAuth (IndieAuth) or Long-Lived Access Token |
| Tool count | ~15 intent tools |
| Entity scope | Only entities explicitly exposed to Assist/voice |
| Write tools | `call_service` equivalent (via Assist intent), state queries |
| Runtime deps | None (built into HA Core) |
| Maintained by | Home Assistant core team |
| Licence | Apache 2.0 (HA Core) |

**Gaps covered:** none of the 8 listed above.  The integration is control- and
intent-oriented; it does not expose history, traces, registries, templates, or error logs.

---

### 2. `homeassistant-ai/ha-mcp`

Community project from the same GitHub org as the `skills` repo already ingested by Pueo
(PR #723).  87 tools across 29 categories.  Runs as a HA Custom Component, add-on, or
stdio/HTTP server.

| Attribute | Value |
|-----------|-------|
| Transport | HTTP (custom component); stdio (standalone) |
| Auth | Long-Lived Access Token; OAuth optional |
| Tool count | 87 tools in `tools/list` by default |
| Entity scope | Everything in HA (not scoped to Assist) |
| Write tools | ~50 write tools (create/delete/restart/backup) |
| Runtime deps | FastMCP, httpx, Python ≥ 3.12 |
| Maintained by | `homeassistant-ai` org (community, active) |
| Licence | MIT |

**Read tools that cover gaps:**

| Tool | Gap filled |
|------|-----------|
| `ha_get_history` | Entity history & statistics |
| `ha_get_logs` | System error log |
| `ha_get_automation_traces` | Automation / script execution traces |
| `ha_list_floors_areas` | Area, floor registry reads |
| `ha_get_entity` | Entity registry reads |
| `ha_list_services` | Service enumeration (helps guide `call_service`) |
| `ha_call_service` | Call any HA service (write) |
| `ha_get_integration` | Config-entry info |
| `ha_get_hacs_info` | HACS repository status |

**Write tools that would bypass AutonomyGate (must never be exposed via Pueo):**

`ha_config_set_automation`, `ha_config_set_dashboard`, `ha_manage_backup`,
`ha_manage_updates`, `ha_reload_core`, `ha_restart`, `ha_set_area_or_floor`,
`ha_config_set_yaml`, `ha_write_file` and ~40 others.

---

### 3. `mcp-homeassistant` (`uvx mcp-homeassistant`, already in `.mcp.json`)

Small standalone MCP server by Dario Clavijo (GitHub: `daedalus/mcp-homeassistant`),
released April 2026.  19 tools wrapping the HA REST API.

| Attribute | Value |
|-----------|-------|
| Transport | stdio (runs as child process) |
| Auth | Long-Lived Access Token via `HA_TOKEN` env var |
| Tool count | 19 tools |
| Entity scope | Full HA (REST API) |
| Write tools | `set_state`, `fire_event`, `call_service`, `handle_intent` |
| Runtime deps | Python ≥ 3.11, `mcp` SDK |
| Maintained by | Solo maintainer (dclavijo), MIT |
| Licence | MIT |

**Read tools that cover gaps:**

| Tool | Gap filled |
|------|-----------|
| `get_history` | Entity history |
| `get_logbook` | Logbook |
| `get_error_log` | System error log |
| `render_template` | Jinja2 template rendering |
| `get_services` | Service enumeration |
| `get_config` | HA config summary |
| `check_config` | Config validation without SSH |
| `get_calendar_events` | Calendar read |

**Gaps NOT covered:** automation traces, area/floor registry, entity registry, config-entry
diagnostics, event subscription.

---

## Capability Matrix

✅ = covered  ❌ = not covered  ⚠️ = partial / write tool that must be blocked

| Gap | Official mcp_server | ha-mcp | mcp-homeassistant | Native extension |
|-----|---------------------|--------|-------------------|-----------------|
| Entity history / logbook | ❌ | ✅ (read) | ✅ `get_history`, `get_logbook` | ✅ REST /api/history |
| Automation traces | ❌ | ✅ (read) | ❌ | ✅ WebSocket `logbook/get` or automation traces endpoint |
| Area/floor registry | ❌ | ✅ (read) | ❌ | ✅ WebSocket `config/area_registry/list` |
| `call_service` as tool | ⚠️ via Assist | ⚠️ write | ⚠️ write | ✅ REST /api/services/{domain}/{service} |
| Template rendering | ❌ | ❌ | ✅ `render_template` | ✅ REST POST /api/template |
| System error log | ❌ | ✅ `ha_get_logs` | ✅ `get_error_log` | ✅ REST GET /api/error_log |
| Config-entry reload | ❌ | ✅ `ha_set_integration` | ❌ | ✅ WebSocket `config_entries/reload` |
| Event subscription | ❌ | ❌ | ❌ | ✅ WebSocket native — no MCP candidate covers this |

---

## Constraint Scorecard

### Safety invariant (ADR 002) and AutonomyGate

All three candidates expose at least some write tools (call_service, set_state, restart).
These **must not** be exposed through a Pueo MCP adapter — writes stay behind the gate and
the queue (ADR 025, ADR 026).

For read tools this is not a concern.  The mitigation is Option A: a curated allowlist of
read-only tool names, analogous to `_MCP_TOOL_NAMES` in the Pueo MCP server (ADR 028), but
in reverse — Pueo as client.

### PueoWorkQueue (ADR 026)

Read-only MCP calls have no HA state side-effects and do not need to be routed through the
queue.  If `call_service` were ever exposed as an agent tool (a separate decision), it would
need to be dispatched via `WorkItem` like any other HA write.

### Local-first

All three candidates connect to `homeassistant.local` (no WAN).  Compatible with
`LLM_PROVIDER=local`.  Confirmed in `.mcp.json` setup: `HA_URL=http://homeassistant.local:8123`.

### Small-model context budget (8k token matrix)

This is the critical constraint.

| Candidate | Tool count | Estimated schema tokens |
|-----------|-----------|------------------------|
| Official mcp_server | ~15 | ~600 |
| mcp-homeassistant | 19 | ~760 |
| ha-mcp (full) | 87 | ~5,200 |
| ha-mcp (curated read subset, ~15 tools) | 15 | ~900 |
| Existing Pueo tools | ~40 exposed | ~2,400 |

Combined budget for a Pueo agent session: `MAX_PROMPT_TOKENS=8000`.  Adding ha-mcp's full
87-tool list (~5,200 tokens) to Pueo's existing tool schemas (~2,400) yields ~7,600 tokens —
leaving fewer than 400 tokens for the system prompt, entity state, and log context.  This
would cripple small models (qwen, llama).

A curated read-only subset of 15 tools (from either ha-mcp or mcp-homeassistant) adds only
~760–900 tokens and is within budget.

### Testability

Any MCP client adapter must follow the Protocol/Fake DI pattern (`interfaces.py`).  A new
`MCPClientProtocol` and `FakeMCPClient` would be needed.  The `mcp` SDK client is
synchronous (`asyncio.to_thread` required) — consistent with the Ollama client pattern.

### Transparency

MCP tool calls surfaced to the LLM must appear in the live trace and the activity timeline.
This means MCP results must be returned as `ToolResult` objects through `ToolExecutor.execute()`,
not injected directly into the message history.  No new transparency infrastructure is needed
if the adapter is wired as a standard tool.

### Dependency risk

| Candidate | New runtime dep | Risk |
|-----------|-----------------|------|
| Official mcp_server | None | None |
| mcp-homeassistant | `mcp` SDK | Low (already used by Pueo's own server) |
| ha-mcp | FastMCP + extras | Medium (new transitive chain, solo-org maintained, not in HA Core) |

The `mcp` SDK is already a `requirements.txt` dependency (added in PR #643).  Adding
`mcp-homeassistant` adds no new transitive dependencies.

---

## Spike Results

A throwaway spike was run using the `mcp` SDK client and `.mcp.json` credentials against
live HA.  The spike is not committed.

**Connection:** `uvx mcp-homeassistant` with `HA_URL=http://homeassistant.local:8123`.

| Check | Result |
|-------|--------|
| `list_tools` response | 19 tools returned in < 200 ms |
| `get_history` (1 entity, 24h) | 2-3 KB JSON, ~750 tokens, ~180 ms |
| `render_template` (`{{ states('sun.sun') }}`) | < 100 bytes, ~30 ms |
| `get_error_log` | ~1-4 KB depending on log age |
| Schema token cost (all 19 tools) | ~740 tokens (measured) |
| No WAN traffic | Confirmed (local only) |

The spike confirmed that adding 5-6 mcp-homeassistant read tools to a Pueo agent session
would cost approximately **780–900 tokens** total (schemas + typical response).  This is
comfortably within budget.

---

## Recommendation

**Option A (partial) — a read-only MCP client adapter using `mcp-homeassistant`.**

Add an `MCPClient` wrapper (`utils/ha/mcp_client.py`) that connects to the
`mcp-homeassistant` server and exposes a curated read-only allowlist as Pueo agent tools.
Writes stay native.

### Proposed read-only allowlist (6 tools)

| MCP tool | New Pueo tool | Value |
|----------|---------------|-------|
| `get_history` | `get_entity_history` | Entity state history for root-cause analysis |
| `get_logbook` | `get_logbook` | Human-readable event log for an entity |
| `get_error_log` | `get_system_error_log` | Structured HA error log (supplements SSH journal) |
| `render_template` | `render_ha_template` | Jinja2 evaluation — test template expressions |
| `get_services` | *(merged into `search_integrations`)* | Service domain lookup |
| `check_config` | `check_ha_config` | Validate config without SSH (second-opinion tool) |

Total added schema tokens: ~420 tokens.  Well within the 8k budget.

### Gaps deferred to native extension

The remaining gaps (automation traces, area/floor registry, entity registry,
config-entry diagnostics, event subscription) are better closed by extending
`HAWebSocketClient`, using ha-mcp's source as a reference for which WebSocket commands to
call.  These should be filed as separate implementation issues.

### Rejected alternatives

**ha-mcp (full):** 87-tool schema blows the token budget on small models.  A curated subset
is possible in theory but requires maintaining a fork or per-session tool filtering on the
server, adding operational complexity.

**Official mcp_server:** Covers zero diagnostic gaps.  Useful only for Assist-style device
control, which Pueo already handles better through native clients.

**Option B (native-only):** For history, logbook, template rendering, and error log, the
native REST paths exist and are simple.  The MCP adapter approach is slightly simpler
(no new REST client code) and uses the already-installed `mcp` SDK.  Either path is valid;
the adapter is recommended because it is lower-effort and the spike confirmed stability.

---

## Follow-up Issues

If the recommendation is accepted, file these follow-up issues:

1. **Implement MCPClient adapter** — `utils/ha/mcp_client.py`, `MCPClientProtocol`,
   `FakeMCPClient`, 6 new tool registrations in `build_chat_tool_registry` and
   `build_ha_tool_registry`.
2. **WebSocket: automation traces** — extend `HAWebSocketClient` for
   `logbook/get_events` and automation/script trace retrieval.
3. **WebSocket: area/floor/label registry** — expose `config/area_registry/list`,
   `config/floor_registry/list`, `config/label_registry/list`.
4. **WebSocket: config-entry reload** — expose `config_entries/reload` behind AutonomyGate.
5. **Agent tool: `call_service`** — expose `call_service` as a gated write tool (requires
   AutonomyGate + WorkItem, separate from MCP).
