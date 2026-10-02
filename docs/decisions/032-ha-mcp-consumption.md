# ADR 032 — HA Capability Gaps: Native REST/WebSocket Extension

## Status
Accepted (amended 2026-10-02)

## Context

Pueo reaches Home Assistant through three hand-written clients (SSH, REST, WebSocket).
Eight diagnostically useful capabilities are not yet exposed as agent tools:

| Gap | Current workaround |
|-----|--------------------|
| Entity history / logbook | none |
| Jinja2 template rendering | none |
| Service enumeration | `search_integrations` names domains, not services |
| Structured system error log | `read_logs` reads the supervisor journal only |
| Area, floor, and label registry | none |
| Automation / script execution traces | none |
| Config-entry diagnostics and reload | none |
| Event subscription (no polling fallback) | everything polled |

The original Proposed version of this ADR recommended closing five of these gaps via a
read-only MCP client adapter (`utils/ha/mcp_client.py`) wrapping the community package
`mcp-homeassistant`.  Post-approval analysis found two factual errors that make that path
impractical:

1. **The `mcp` SDK v2.2.0 is async-only** (`mcp/client/stdio.py:113`).  Calling it from
   `asyncio.to_thread()` (as the Proposed ADR suggested) raises a `RuntimeError` because
   the SDK creates its own event loop internally.
2. **`mcp-homeassistant` is not installed** and would require `uvx` at runtime and inside
   Docker, adding an uncontrolled external dependency.

Separately, `ToolExecutor` has no REST client at all today, so even the five read gaps that
`mcp-homeassistant` covers would need REST client wiring.  The MCP wrapper saves nothing.

Full candidate research and spike measurements are in
[docs/plan/ha-mcp-evaluation.md](../plan/ha-mcp-evaluation.md).

## Decision (amended 2026-10-02)

**Reject the MCP client adapter.  Close all eight gaps by extending the native REST and
WebSocket clients.**

Specifically:

- **No `MCP_HA_ENABLED` or `MCP_HA_SERVER_CMD` config keys** are added.
- **No `utils/ha/mcp_client.py`** or `MCPClientProtocol` / `FakeMCPClient`.
- **`ToolExecutor` gains an `ha_rest_client` parameter** (S1), wired from `main.py` and every
  sub-agent executor site.
- The eight gaps are closed over ten steps (S1–S10); see
  [docs/plan/ha-capability-gaps.md](../plan/ha-capability-gaps.md).

### REST-based tools (S1–S2)

| REST endpoint | New Pueo tool |
|---------------|---------------|
| `GET /api/history/period/{ts}` | `get_entity_history` |
| `GET /api/logbook/{ts}` | `get_logbook` |
| `POST /api/template` | `render_ha_template` |
| `GET /api/services` | folded into `search_integrations` |

### WebSocket-based tools (S3–S6)

| WS command | New Pueo tool |
|------------|---------------|
| `system_log/list` | `get_system_error_log` |
| `config/area_registry/list` etc. | `get_area_layout` |
| `trace/list` + `trace/get` | `get_automation_traces` |
| config-entry diagnostics (REST) + reload (WS) | `get_integration_diagnostics`, `reload_integration` |

### Gated write tools (S7)

`call_service(domain, service, data, target, reason)` — gated through `CARD_TYPE_SERVICE_CALL`.
Risk is set per domain by `utils/ha/service_policy.py`; a blocklist prevents restart/backup/
shell/update operations from being exposed at all.  Never exposed via MCP.

### Event subscription (S8–S9)

`HAEventSubscriber` — a long-lived WebSocket connection (separate from the per-call
`HAWebSocketClient`) that subscribes to `state_changed`, `repairs_issue_registry_updated`,
`automation_triggered`, and `persistent_notification/subscribe`.  It feeds a ring buffer and
wakes the relevant poll loops via `supervisor.wake(name)`, replacing polling for those events
while the connection is healthy.  Polling stays as a safety-net fallback at
`HA_EVENT_FALLBACK_POLL_MINUTES` (default 60 min).

## Rationale

**No new dependencies.** Every endpoint used is part of the standard HA REST and WebSocket
APIs.  No child process, no `uvx`, no additional `requirements.txt` entries.

**Consistent DI pattern.** Adding `ha_rest_client` to `ToolExecutor` follows the same
`set_ws_client` / `set_ssh_client` pattern already established.

**All eight gaps closed.** The MCP adapter left three gaps open (traces, registries, events).
Native extension closes all eight in one delivery sequence.

**Budget stays safe.** New tool schemas add ≈ 800–1 000 tokens to the session context (chat
+ HA registries).  This is verified by `test_registry_schema_token_budget` (added in S2).

**Writes stay gated.** `call_service` and `reload_integration` flow through the `AutonomyGate`
and `PueoWorkQueue`, preserving ADR 002 and ADR 026.

## Consequences

- `ToolExecutor.__init__` gains `ha_rest_client: Optional[HARestClientProtocol] = None`.
  All sub-agent executor construction sites must pass it (ADR 017 chat-tool parity).
- `HAWebSocketClient` gains a `_call` helper and several new command methods.
- A new `HAEventSubscriber` module is added.  It runs as a supervisor task, gated by
  `HA_EVENT_SUBSCRIBE` (bool, default true when a token is set).
- `call_service` is added to ha and chat registries; **never** to the MCP server.
- `utils/ha/service_policy.py` is a new module; its blocklist is security-critical.
- The token budget is enforced by an automated test (`test_registry_schema_token_budget`).

## Related decisions

- [ADR 001 — Config centralization](001-config-centralization.md): triple-update rule for
  `HA_EVENT_SUBSCRIBE`, `HA_EVENT_BUFFER_SIZE`, `HA_EVENT_FALLBACK_POLL_MINUTES`.
- [ADR 002 — Safety invariant](002-safety-invariant.md): `call_service` blocklist; reload gated.
- [ADR 017 — Chat tool parity](017-chat-tool-parity.md): REST client injected at every
  sub-agent executor site.
- [ADR 025 — Serialized work queue](025-serialized-work-queue.md): card actions dispatch via `WorkItem`.
- [ADR 026 — No concurrent LLM/HA](026-no-concurrent-llm-ha.md): call_service and reload routed through queue.
- [ADR 028 — Pueo MCP server](028-pueo-mcp-server.md): Pueo as MCP *server* (unchanged); `call_service` remains excluded from MCP exposure.
- [ADR 033 — Event-driven HA triggers](033-ha-event-triggers.md): wake semantics, fallback intervals, healthy-subscriber definition.
- [docs/plan/ha-mcp-evaluation.md](../plan/ha-mcp-evaluation.md): original research, candidates, spike results.
- [docs/plan/ha-capability-gaps.md](../plan/ha-capability-gaps.md): step-by-step delivery plan (S1–S10).
