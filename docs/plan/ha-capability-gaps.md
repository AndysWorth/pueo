# HA Capability Gaps — Delivery Plan

**Approved:** 2026-10-02  
**ADR:** [ADR 032 (amended)](../decisions/032-ha-mcp-consumption.md)

Close all 8 HA diagnostic capability gaps using native REST/WebSocket extension.
Each step is one GitHub issue → one branch → one PR.  Stack branches while a PR awaits review.

## Status

| Step | Issue | Title | Status |
|------|-------|-------|--------|
| S0 | [#731](https://github.com/AndysWorth/pueo/issues/731) | Amend ADR 032, save plan, file S1–S10 issues | Done (2026-10-02) PR #742 |
| S1 | [#732](https://github.com/AndysWorth/pueo/issues/732) | Wire REST client into ToolExecutor, add `get_text` | Done (2026-10-02) PR #743 |
| S2 | [#733](https://github.com/AndysWorth/pueo/issues/733) | History, logbook, template, services tools + token budget test | Done (2026-10-02) PR #744 |
| S3 | [#734](https://github.com/AndysWorth/pueo/issues/734) | WS `_call` helper + `get_system_error_log` | Done (2026-10-02) PR #745 |
| S4 | [#735](https://github.com/AndysWorth/pueo/issues/735) | Area/floor/label registry → `get_area_layout` | Done (2026-10-02) — PR #746 |
| S5 | [#736](https://github.com/AndysWorth/pueo/issues/736) | Automation traces → `get_automation_traces` + seed runbook | Done (2026-10-02) PR #747 |
| S6 | [#737](https://github.com/AndysWorth/pueo/issues/737) | Config-entry diagnostics + gated `reload_integration` | Done (2026-10-02) |
| S7 | [#738](https://github.com/AndysWorth/pueo/issues/738) | Gated `call_service` + `service_policy.py` | Done (2026-10-02) PR #749 |
| S8 | [#739](https://github.com/AndysWorth/pueo/issues/739) | `HAEventSubscriber`, ring buffer, `get_recent_events` | TODO |
| S9 | [#740](https://github.com/AndysWorth/pueo/issues/740) | Event-driven triggers: `supervisor.wake()` + ADR 033 | TODO |
| S10 | [#741](https://github.com/AndysWorth/pueo/issues/741) | Docs and prompt wrap-up | TODO |

---

## Gap → step map

| Gap | Step | Mechanism |
|-----|------|-----------|
| Entity history / logbook | S2 | REST `/api/history/period`, `/api/logbook` |
| Jinja2 template rendering | S2 | REST `POST /api/template` |
| Service enumeration | S2 | REST `/api/services` folded into `search_integrations` |
| Structured system error log | S3 | WS `system_log/list` |
| Area, floor, and label registry | S4 | WS `config/{area,floor,label}_registry/list` |
| Automation / script traces | S5 | WS `trace/list`, `trace/get` |
| Config-entry diagnostics + reload | S6 | REST `/api/diagnostics/config_entry/{id}`; reload gated |
| `call_service` as agent tool | S7 | REST `call_service`, gated + WorkItem |
| Event subscription | S8 + S9 | Long-lived WS subscriber; wakes poll loops |

---

## Standard per-step procedure

1. `git checkout main && git pull` — start from a clean base.
2. Create the branch `feat/<N>-<slug>` (issue number from the table above).
3. Implement and write tests in the same session.
4. Update related files (see patterns below).
5. Run the full CI gate as one chained command.
6. Open the PR with `Closes #N`.
7. Mark the step Done (date) in the Status table above and in memory `project-pueo-state.md`.

**Rule for every HA endpoint:** before coding, web-search current HA dev docs to confirm the
command name and payload.  Training data is stale.

---

## Shared patterns

### New client method (interface + real + fake)

1. Add the method signature to the Protocol in `interfaces.py`.
2. Implement it in `utils/ha/ha_rest_client.py` or `utils/ha/ha_ws_client.py`.
3. Add a matching stub to `FakeHARestClient` (`:181`) or `FakeHAWebSocketClient` (`:245`).

### New read tool

1. Define a `ToolDefinition` constant in `utils/agent/tool_registry.py` and register it in
   the relevant `build_*_registry` builders.
2. Add an `if name ==` branch in `ToolExecutor.execute()` (`utils/agent/tool_executor.py:243`).
3. Add a `_method` that returns `ToolResult` and never raises.  Model it on
   `_check_entity_status` (`:2484`).  Return `success=False` when the client is `None`.
4. Compact the output before returning.
5. Add tests in `tests/test_tool_executor.py` (success, no client, client error) and
   membership tests in `tests/test_tool_registry.py`.
6. Add to `_MCP_TOOL_NAMES` and `_build_asgi_app` tool list in `utils/mcp/pueo_mcp_server.py`
   for MCP-eligible tools.

### Gated write tool

1. Call `gate.queue_for_approval(..., risk=...)`, modelled on `_apply_fix`
   (`tool_executor.py:1534`).
2. On a non-approved result, return `awaiting_approval=True`.
3. Add a new `CARD_TYPE_*` in `utils/hitl/card_types.py`.
4. Add a `_CARD_DISPATCH` handler in `web/dashboard.py` (`:1717`) that submits a
   `WorkItem(priority=PRIORITY_HIGH, activity_type="card_execution", dedup_key=f"card_{nid}")`.
5. Register the card template: add the `index.html` is_actionable tuple and the hint text.
6. Make sure `approve()` calls `mark_card_*`.
7. Add the tool to the dangerous set in `tests/test_mcp_server.py:34`.

---

## Step details

### S0 — ADR amendment, saved plan, and issues (docs only)
**Issue:** [#731](https://github.com/AndysWorth/pueo/issues/731)

- Rewrite `docs/decisions/032-ha-mcp-consumption.md`: Status → Accepted (amended 2026-10-02),
  decision → native REST/WS, fix the async-only SDK error, record that `MCP_HA_ENABLED`/
  `MCP_HA_SERVER_CMD` are not added, absorb deferred items as in-scope decisions.
- Add Outcome note to `docs/plan/ha-mcp-evaluation.md`.
- Update `docs/decisions/000-index.md` row 032.
- Save this plan as `docs/plan/ha-capability-gaps.md`.
- File GitHub issues for S1–S10.

### S1 — Wire REST client into ToolExecutor
**Issue:** filed in S0

- Add `ha_rest_client: Optional[HARestClientProtocol] = None` param and `set_rest_client()`
  to `ToolExecutor.__init__` (`:169`) alongside `set_ws_client` (`:228`).
- `main.py` (~`:849`): build `HARestClient(HA_HOST, HA_API_PORT, HA_API_TOKEN)` when a token
  is set; call `_shared_executor.set_rest_client(...)`.
- Chat-parity (ADR 017) audit: pass REST client wherever sub-agent executors are built:
  `agents/ha_log_monitor.py:545`, `ha_lovelace_monitor.py:95`,
  `ha_notification_manager.py:274`, `ha_update_manager.py:595`,
  `utils/agent/investigation_loop.py:249`, `config_analysis.py:89`,
  dashboard chat fallback (~`web/dashboard.py:3320`).
- Add `get_text(path) -> str` to `HARestClient` for text responses (`get_raw` is JSON-only).
- Tests: executor stores and uses the injected client; `get_text` against a mocked httpx transport.

### S2 — History, logbook, template, services tools
**Issue:** filed in S0

- REST methods (new-client-method pattern): `get_history(entity_id, hours)`,
  `get_logbook(entity_id, hours)`, `render_template(template) -> str`, `get_services()`.
- Tools: `get_entity_history` (compacted transitions, capped rows), `get_logbook`,
  `render_ha_template` (reject > 2 KB).
- Extend `search_integrations` to include service names for a matching domain.
- Confirm `ha core check` is in `_HA_COMMAND_ALLOWLIST`; if so, no new tool needed.
- Registries: ha, chat, lovelace_investigation; add MCP-eligible tools to MCP server.
- New test: `test_registry_schema_token_budget` — asserts chat + ha registry schema tokens
  stay under current + 25% headroom ceiling.

### S3 — WS `_call` helper + structured error log
**Issue:** filed in S0

- Refactor `HAWebSocketClient` to add `async def _call(self, type_: str, **payload) -> Any`:
  connect, auth, send, receive, check `success`, close.  Migrate existing simple methods;
  existing tests must stay green.
- Add `get_system_log()` WS method (`system_log/list`) and a `get_system_error_log(level, limit,
  logger_filter)` tool.  The tool groups entries by logger (count, first/last seen, message).
- Registries: ha, chat, notification_investigation, plus MCP.

### S4 — Area, floor, and label registry
**Issue:** filed in S0

- WS methods: `get_area_registry`, `get_floor_registry`, `get_label_registry`.
- Tool: `get_area_layout(area=None)` — joins floors → areas → devices/entities; reuses
  existing `get_device_registry`/`get_entity_registry`; per-area counts or one-area members.
- Registries: ha, chat, lovelace_investigation, plus MCP.

### S5 — Automation and script traces
**Issue:** filed in S0

- WS methods: `list_traces(domain, item_id=None)` and `get_trace(domain, item_id, run_id)`.
- Tool: `get_automation_traces(entity_id, run_id=None)` — without `run_id` lists last N runs;
  with `run_id` returns condensed step path (trigger, conditions, actions, error).
  Strip variables and context blobs to stay within budget.
- Seed runbook `prompts/seed_automation_failure.md` — use traces first for "automation didn't
  fire" problems.  Mirror to pueo-kb.
- Registries: ha, chat, plus MCP.

### S6 — Config-entry diagnostics + gated reload
**Issue:** filed in S0

- REST methods: `get_config_entry_diagnostics(entry_id)`, `reload_config_entry(entry_id)`.
- Read tool: `get_integration_diagnostics(domain_or_entry_id)` — resolves domain via
  `get_all_config_entries`; redacts keys matching `token|password|api_key|secret`.
- Gated write tool: `reload_integration(entry_id, reason)`, risk MEDIUM, new card type
  `CARD_TYPE_CONFIG_ENTRY_RELOAD`.  Dashboard handler dispatches via `WorkItem` (ADR 026).
- Registries: diagnostics in ha, chat, MCP; reload in ha and chat only.
- Run `/security-review` (production write).

### S7 — `call_service` as a gated tool
**Issue:** filed in S0

- Tool `call_service(domain, service, data, target, reason)` in ha and chat registries; **never** MCP.
- New module `utils/ha/service_policy.py` with `classify_service_risk(domain, service) -> RiskLevel | None`.
  - LOW: `light`, `switch`, `fan`, `cover`, `input_*`, `notify`, `persistent_notification`.
  - MEDIUM: `automation.*`, `script.*`, `scene.*`, `homeassistant.reload_*`, `homeassistant.update_entity`.
  - HIGH: everything else.
  - Blocked: `homeassistant.restart`/`stop`, `hassio.*`, `backup.*`, `recorder.purge*`,
    `update.install`, `shell_command.*`, `python_script.*`, `pyscript.*`.
- Flow: check blocklist → `gate.queue_for_approval(risk=...)` → new card `CARD_TYPE_SERVICE_CALL`
  → dashboard handler dispatches `WorkItem` that calls `rest.call_service`.
- Tests: table-driven policy; blocked → `ToolResult` error; LOW at autonomy level 3
  auto-executes; HIGH produces a card.  Add to MCP dangerous set.
- Run `/security-review`.

### S8 — HA event subscriber
**Issue:** filed in S0

- New module `utils/ha/ha_event_subscriber.py` with `HAEventSubscriber(host, port, token)`.
  Persistent connection with reconnect + backoff, separate from per-call `HAWebSocketClient`.
- Subscriptions: `state_changed` (filtered to `update.*` and unavailable/unknown entities),
  `repairs_issue_registry_updated`, `automation_triggered`,
  `persistent_notification/subscribe`.
- Ring buffer: `collections.deque(maxlen=HA_EVENT_BUFFER_SIZE)`; publishes to supervisor bus.
- `FakeHAEventSubscriber` + `HAEventSubscriberProtocol` in `interfaces.py`.
- Supervisor task in `main.py`, gated by `HA_EVENT_SUBSCRIBE` (bool) and
  `HA_EVENT_BUFFER_SIZE` (int, default 500).  Triple-update rule: `config.py`,
  `config.yaml.default`, `setup.sh`, `TestConfigDefaults`.
- Tool: `get_recent_events(event_type, entity_id, limit)` in ha, chat, MCP.
- Tests: message parsing, filter, ring-buffer bound, reconnect — scripted mock socket.

### S9 — Event-driven triggers
**Issue:** filed in S0

- `supervisor.wake(name)` — sets a per-loop `asyncio.Event`; `supervised_sleep` waits on it
  together with the timeout.  Interrupts the sleep only; unlike `run_now`, never cancels a
  running iteration.  A wake mid-iteration stays latched, fires on the next sleep.
- Subscriber → wake map (debounce 5 s):
  - persistent notification change → `notification_poll`
  - `repairs_issue_registry_updated` → `repair_poll`
  - `update.*` state change → `update_check`
- Healthy subscriber: loops sleep `HA_EVENT_FALLBACK_POLL_MINUTES` (default 60).
  On disconnect: revert to normal interval immediately.
- New config key `HA_EVENT_FALLBACK_POLL_MINUTES` (triple-update rule).
- Write ADR 033 and add to ADR index.
- Tests: wake interrupts sleep; latched wake fires on next sleep; interval switches with
  subscriber health.  Test loop logic directly (not infinite loops).

### S10 — Docs and prompt wrap-up
**Issue:** filed in S0

- Update `CLAUDE.md` Key Patterns: REST client in ToolExecutor, event subscriber and wake,
  service policy.
- Update `docs/for-agents.md`: clients table, Protocol/Fake table, tool files.
- Update `prompts/agent_loop.md`: one-line guidance for history, traces, error log, recent
  events, call_service.
- Mark all steps Done in this document.
- Update `project-pueo-state.md` memory.

---

## Verification

- **Per step:** run the full CI gate.
- **Live exercise per step (CLAUDE.md step 12):**
  - S2: "show the history of sun.sun for 6h"; render `{{ states('sun.sun') }}`.
  - S3: "any errors in the HA system log?"
  - S4: "what's in the kitchen?"
  - S5: "why didn't automation X run?"
  - S6/S7: confirm approval card appears; approval executes through the work queue (Activity widget shows `card_execution`).
  - S8/S9: dismiss or create a persistent notification in HA; confirm `notification_poll` wakes within seconds.
- **S9 failover:** kill the subscriber; confirm loops revert to normal intervals.
- **S6/S7:** `/security-review` before the PR.
