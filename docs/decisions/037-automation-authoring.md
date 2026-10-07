# ADR 037 — Automation Authoring Safety Chain

**Status:** Accepted  
**Date:** 2026-10-07

## Context

Pueo can diagnose and repair *existing* Home Assistant automations, but it cannot author
new ones. The chat endpoint is the natural authoring surface: a user describes what they
want, the LLM drafts the trigger/condition/action YAML, and Pueo handles the write safely.

ai_agent_ha (ADR 034) already does this — `create_automation` writes `automations.yaml`
and calls `automation.reload` without any pre-write validation or backup. The gap Pueo
fills is the safety chain: validate → HITL card → backup → REST write → reload → verify.

## Decision

### 1. New chat tool: `propose_automation`

Registered in `build_chat_tool_registry` only. Never in the ha, netalertx,
code-proposal, or MCP registries. Autonomous repair and monitoring agents do not author
automations; this is strictly an interactive feature.

### 2. Validation before the card is raised: WS `validate_config`

Before raising the HITL card, call the HA WebSocket `validate_config` command with the
proposed `trigger`, `condition`, and `action` fields. Any component that returns
`"valid": false` is returned to the model as an error — the card is not raised until
all present components are valid.

`validate_config` is the endpoint the HA UI automation editor calls for exactly this
purpose. It validates the component parts in isolation without touching storage.

The REST `POST /api/config/core/check_config` endpoint validates `configuration.yaml`
on disk and does **not** validate automation storage entries; it is excluded from this
flow. The repair pipeline's sandbox step (write to `.agent_sandbox/`, run
`ha core check`, revert) is also inapplicable here for the same reason.

### 3. New card type: `CARD_TYPE_AUTOMATION_CREATE` (MEDIUM risk)

Risk tier matches `automation.*` service calls in `service_policy.py`. The card payload
carries the full YAML and the `validate_config` result so the user can review both.

MEDIUM risk means explicit approval is required at autonomy levels 0–2. At level 3–4 the
gate would auto-execute; operators who enable those levels accept that Pueo may create
automations from chat without prompting.

### 4. Approval executor: backup → REST write → reload → verify

On card approval, a `WorkItem(priority=PRIORITY_HIGH, activity_type="card_execution",
dedup_key=f"card_{nid}")` runs these steps in order:

1. `execute_remote_backup()` — mandatory; raises on failure (ADR 002)
2. `record_backup_slug()` — backup slug is stored before any write
3. REST `POST /api/config/automation/config/{unique_id}` — writes to HA's config storage
4. `call_service(domain="automation", service="reload")` — activates the new automation
5. Poll REST states for `automation.{slug}` up to 3 times (1-second intervals) — verifies
   the entity appeared; failure is logged but does not roll back (the write succeeded)
6. `mark_card_resolved(nid)` — closes the HITL card
7. Emit a timeline event

### 5. ID prefix and unique ID scheme

All Pueo-authored automations use the prefix `pueo_auto_`. This separates them from
user automations, HA-managed automations, and `ai_agent_auto_` entries from ai_agent_ha
(ADR 034).

Unique ID: `pueo_auto_{alias_slug}_{uuid8}` where `alias_slug` is the alias lowercased
with non-alphanumeric characters replaced by `_` and truncated to 40 characters, and
`uuid8` is the first 8 hex characters of a UUID4. The ID is generated at tool-call time
and included in the card payload so the executor has it without a database query.

### 6. Rollback

`DELETE /api/config/automation/config/{unique_id}` removes the automation entry from HA
storage cleanly. The backup slug is always recorded before the write, providing a
full-state rollback path if needed. Both the unique ID and backup slug are stored in the
HITL card row so the user can roll back from the dashboard.

## Consequences

- `HAWebSocketClient` gains `validate_automation_config(trigger, condition, action)`.
- `HARestClient` gains `create_automation(unique_id, config)` and
  `delete_automation(unique_id)`.
- `CARD_TYPE_AUTOMATION_CREATE = "automation_create"` is added to `card_types.py`.
- `index.html` registers `"automation_create"` in the `is_actionable` tuple and adds
  the hint-text branch.
- `_execute_automation_create` in `dashboard.py` implements the approval executor.
- `propose_automation` in `tool_executor.py` implements the tool.
- Autonomous pipelines are unchanged; the tool is chat-only.
- No new dependency.

## Related decisions

- [ADR 002 — Safety invariant](002-safety-invariant.md): backup-before-write ordering
  is unchanged; this flow is a new caller, not an exception.
- [ADR 025 — Serialized work queue](025-serialized-work-queue.md): the approval executor
  runs as a `WorkItem` exactly as `_execute_service_call` does.
- [ADR 026 — No concurrent LLM/HA](026-no-concurrent-llm-ha.md): the REST write and
  `automation.reload` are HA writes and must go through `PueoWorkQueue`.
- [ADR 034 — Opportunistic HACS companion integrations](034-opportunistic-hacs-companion-integrations.md):
  ai_agent_ha's `create_automation` is the prior art; Pueo adds the validation and
  backup safety chain that ai_agent_ha omits.
- [ADR 035 — Full-autonomous level](035-full-autonomous-confidence-gate.md): MEDIUM-risk
  automation creation auto-executes at levels 3–4; no LLM confidence gate is applied
  (unlike CRITICAL updates), because the user explicitly initiated the request.
