# ADR 034 — Opportunistic HACS Companion Integrations

## Status
Accepted

## Context

Pueo's original design assumes it controls all diagnostic tooling. Two HACS integrations exist
that produce diagnostic signals Pueo can use:

- **[Spook](https://spook.boo)** (by @frenck) — an "unsupported" HA extension that adds repair
  entries for dead-entity references, missing lovelace resources, and broken automation references.
  Its repairs appear in the standard HA repairs registry under `domain="spook"`, queryable via the
  existing `repairs/list_issues` WebSocket command Pueo already calls.

- **[ha-upgrade-advisor](https://github.com/brianegge/ha-upgrade-advisor)** (by @brianegge) —
  a HACS integration that analyses the installed configuration against the breaking changes in a
  target HA version. It exposes `sensor.upgrade_advisor_status` (idle/analyzing/report_ready/error)
  and `sensor.upgrade_advisor_risk` (low/medium/high) via the standard HA REST API.

- **[ai_agent_ha](https://github.com/sbenodiz/ai_agent_ha)** (by @sbenodiz) — a HACS
  integration (`custom_components/ai_agent_ha`) that adds an LLM sidebar chat to HA. It can
  create automations (ids prefixed `ai_agent_auto_`), dashboards, and call services. Its
  `set_entity_state` tool falls back to `hass.states.async_set` for non-standard domains
  (anything other than light, switch, cover, climate, fan), creating **phantom state** that does
  not reflect the underlying device. Pueo detects its presence to surface relevant runbook
  guidance when diagnosing automation or state anomalies.

Neither integration is required for Pueo to function; requiring them would add friction to setup
and force a hard dependency on third-party HACS projects that may become unmaintained or introduce
incompatibilities. However, discarding their signals entirely is wasteful when users have already
installed them.

A prior implementation attempt wired `get_spook_entity_issues()` to a non-existent WebSocket
command (`spook/entities/issues/list`) that always returns `unknown_command`, making the tool
silently do nothing. The ha-upgrade-advisor path read a `recommendation` attribute that does not
exist on the real sensor entities. This ADR formalises the pattern that replaces those dead wires.

## Decision

### 1. Detection via `HAEnvironmentProfile`

`HAEnvironmentProfile` (`utils/ha/ha_environment.py`) gains three boolean fields:
`spook_installed: bool`, `upgrade_advisor_installed: bool`, and `ai_agent_ha_installed: bool`.
Detection checks `installed_integrations` for the component domain (`"spook"`,
`"upgrade_advisor"`, `"ai_agent_ha"`). Detection is best-effort: a missing HA API token or a
REST/WS failure causes each field to default to `False` rather than raising.

Callers check the profile field before making companion-specific API calls. When the field is
`False`, the call is skipped entirely — no `unknown_command` errors, no confusing empty results.

### 2. Spook: `get_spook_issues` via the repairs registry

`get_spook_issues` calls `repairs/list_issues` (the standard repairs WebSocket command that Pueo
already calls for native HA repairs) and filters to `domain="spook"`. It groups results by
`issue_domain` (the domain the broken entity or resource belongs to: `automation`, `script`,
`lovelace`, etc.), outputs `issue_domain`, `translation_key`, `issue_id`, `severity`, and
`placeholders`, and passes the result through `truncate_to_budget`.

The tool is registered in the HA repair, lovelace, and chat registries (read-only). It is also
included in `_MCP_TOOL_NAMES` — Spook's dead-entity data is valuable for read-only consumers such
as the HA Companion App.

### 3. ha-upgrade-advisor: entity registry resolution, `request_advisor_analysis`, and stale-version guard

**Entity IDs.** The integration exposes `sensor.upgrade_advisor_status` and
`sensor.upgrade_advisor_risk_level` (note `_level` suffix — not `_risk`). Both IDs are resolved at
runtime through `config/entity_registry/list` (WebSocket), filtering for `platform == "upgrade_advisor"`
and matching on the entity's `unique_id` suffix (`_status`, `_risk_level`). Hard-coded fallback IDs
(`sensor.upgrade_advisor_status`, `sensor.upgrade_advisor_risk_level`) are used when a WS client is
not available or the registry call fails.

**`request_advisor_analysis(rest, version, component_type, *, ws_client, timeout_seconds)` — trigger and poll.**
When `HAEnvironmentProfile.upgrade_advisor_installed` is `True`, `_run_update_analysis` calls
`request_advisor_analysis` before reading the report. This function:
1. Calls `upgrade_advisor.analyze_version` (Core/HA updates) or `upgrade_advisor.analyze` (HACS
   integrations) via `HARestClient.call_service`, bypassing `service_policy.py` — this is a
   direct infrastructure call from the update manager, not an agent-tool `call_service` call, so it
   does not require user approval.
2. Polls `sensor.upgrade_advisor_status` every `_POLL_INTERVAL_SECONDS` (10 s) until state ≠
   `"analyzing"` or the timeout expires. On timeout, logs a warning and returns — the caller
   proceeds to `read_advisor_report`, which will return `None` for a stale/absent report.
3. Skips add-ons and Supervisor/OS components (upstream does not analyse them).

**`read_advisor_report(rest, target_version, *, ws_client) -> AdvisorReport | None`**
reads the status and risk-level sensors.  A report is used only when **both** conditions are true:
- `state == "report_ready"` — the advisor has finished its analysis
- `available_version == target_version` — the report is for the version Pueo is about to act on

If either condition fails, `read_advisor_report` returns `None` and the caller proceeds without the
advisor data. This prevents applying stale analysis from a previous upgrade cycle.

When a valid report is returned, `_run_update_analysis` in `ha_update_manager.py` injects the
risk level, breaking-change count, and truncated report text into the initial context, labelled
**"Third-party upgrade-advisor report (unverified — treat as advisory only)"**. The label is
mandatory: it reminds the agent that this is not Pueo's own conclusion and should be treated as
supporting evidence.

### 4. Third-party output is supporting evidence, never a decision trigger

Companion-integration output is injected into agent context as supporting evidence, not as a
decision signal. Specifically:

- Pueo never takes an action (repair, apply update, suppress an alert) solely because a companion
  integration says to.
- The agent loop's 6-phase methodology still applies: the agent must gather its own evidence and
  confirm the root cause independently before acting.
- Companion output is labelled in context (`[BEST PRACTICE]` for skills data; plain prose labels
  for Spook and advisor data) so the model can reason about source trust.

### 5. Destructive companion services are permanently blocked

When Spook is installed it registers approximately 120 additional services. Some of these are
destructive or hide problems and have no safe use in an autonomous healing agent:

| Service | Reason blocked |
|---|---|
| `homeassistant.delete_all_orphaned_entities` | Destructive — no undo, removes entities that may be temporarily unavailable |
| `homeassistant.disable_user` / `enable_user` | Destructive account management; out of scope for Pueo |
| `repairs.ignore_all` / `unignore_all` | Suppresses repair visibility; hides problems from Pueo |
| `repairs.remove` | Permanent removal of repair entries before they can be acted on |
| `recorder.import_statistics` | Writes arbitrary historical data; integrity risk |

These are added to the `_BLOCKED` set in `utils/ha/service_policy.py` alongside the existing
core blocks (`homeassistant.restart`, `backup.*`, etc.). They are blocked regardless of autonomy
level — no approval path exists for them.

### 6. Setup optional prompts

`setup.sh` offers optional install prompts for both integrations using the script's `ask` helper.
The Spook prompt opens the HACS deep link; the Upgrade Advisor prompt opens the GitHub repo. Both
prompts gate the `open` call on `uname == Darwin` (macOS only) and report success only when `open`
exits 0; on Linux they print the URL directly. Neither prompt is required — skipping them leaves
Pueo fully functional with no companion signals.

## Rationale

**Opportunistic use is better than ignoring.** Users who have already installed Spook or
ha-upgrade-advisor expect Pueo to leverage those signals. Detecting and using them when present
at zero setup cost delivers meaningful value.

**Graceful absence is the only safe default.** An integration that may be absent must never
produce a detectable failure (error log, empty result, `unknown_command` error) when absent.
Checking the profile field first and skipping the call when the field is `False` satisfies this.

**Stale-version guard on the advisor.** The advisor sensor persists its report across HA restarts.
Without the version guard, Pueo could inject analysis from a three-month-old upgrade cycle into
the context for a current upgrade, potentially flagging already-resolved breaking changes.

**Service blocklist as a companion-aware defense.** Companion integrations widen the attack
surface for prompt-injection attacks that try to call destructive services via the chat endpoint
or an automated pipeline. Blocking them unconditionally at the policy layer is cheaper and more
reliable than relying on the agent to reason about whether each call is safe.

## Consequences

- `HAEnvironmentProfile` gains `spook_installed`, `upgrade_advisor_installed`, and
  `ai_agent_ha_installed` boolean fields. All default `False` when detection fails.
  All existing callers are unaffected.
- `get_spook_issues` replaces the dead `get_spook_entity_issues` in `ha_ws_client.py` and
  `interfaces.HAWebSocketClientProtocol`. `FakeHAWebSocketClient` grows a corresponding method.
- `utils/ha/upgrade_advisor.py` exposes `read_advisor_report`, `request_advisor_analysis`, and
  `read_post_upgrade_report`. Entity IDs are resolved via the entity registry; hard-coded defaults
  are the fallback. The module imports `HARestClientProtocol` and `HAWebSocketClientProtocol` only
  under `TYPE_CHECKING` — no agent logic, testable in isolation.
- Five services are added to `service_policy._BLOCKED`; tests cover each of them.
- `docs/setup-guide.md` Section 7 gains two optional companion prompts (Spook, Upgrade Advisor).
- `README.md` gains an "Optional HA companion integrations" subsection.
- `CLAUDE.md` gains an "Opportunistic HACS companion integrations" key-pattern paragraph.
- `prompts/seed_ai_agent_ha.md` seed runbook documents ai_agent_ha quirks and is registered in
  `_SEED_PROMPTS` so agents surface it via `query_knowledge` when ai_agent_ha is detected.
  Mirrored to `pueo-kb/runbooks/`.
- When `ai_agent_ha_installed` is `True`, the `ha_event_wake_dispatch` task wakes
  `lovelace_poll` on three additional event types: `state_changed` for
  `automation.ai_agent_auto_*` entities, `automation_triggered` for those same entities,
  and any `lovelace_updated` event (added to `_SUB_EVENTS`). This triggers the existing
  lovelace entity-health investigation promptly after ai_agent_ha makes dashboard or
  automation changes, and raises a HITL card when issues are found. The wake is gated on
  the profile flag so installations without the companion see no extra dispatching. See
  ADR 033 for the subscriber → wake map.

## Related decisions
- [ADR 002 — Safety invariant](002-safety-invariant.md): companion output is supporting evidence
  only; the backup-before-write rule is not conditional on companion signals.
- [ADR 017 — Chat tool parity](017-chat-tool-parity.md): `get_spook_issues` is wired identically
  in chat and automated pipelines.
- [ADR 018 — Unified agent methodology](018-unified-agent-methodology.md): the 6-phase cycle
  applies; companion data enters as Phase 3 evidence, not as a Phase 5 action trigger.
- [ADR 028 — Pueo MCP server](028-pueo-mcp-server.md): `get_spook_issues` is added to
  `_MCP_TOOL_NAMES`; `call_service` remains excluded from MCP.
- [ADR 030 — Authority-ranked knowledge](030-authority-ranked-knowledge.md): `ha_best_practices`
  (the skills collection) carries authority score 0.90 and the `[BEST PRACTICE]` label — the same
  tier as `ha_release_notes`, just below official developer docs.
