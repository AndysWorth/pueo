# Automation Authoring — Implementation Spec

**Status:** S10 complete — all sessions done  
**Issue:** [#801](https://github.com/AndysWorth/pueo/issues/801)  
**ADR:** [ADR 037](../decisions/037-automation-authoring.md)

---

## Goal

Allow the chat endpoint to create new HA automations from a natural-language request,
routed through the standard Pueo safety chain: validate → HITL card → backup → write
→ reload → verify.

---

## API surface (research summary)

### WS `validate_config` (pre-approval validation)

Validates the component parts of an automation before the card is raised.
All fields are optional; only fields sent are included in the response.

```json
// request
{ "id": 1, "type": "validate_config",
  "trigger": [...], "condition": [...], "action": [...] }

// response
{ "result": {
    "trigger": {"valid": true,  "error": null},
    "condition": {"valid": false, "error": "Invalid condition specified for data[0]"},
    "action": {"valid": true,  "error": null}
} }
```

Any component returning `"valid": false` is a hard stop — the tool returns the error to
the model, which must fix the YAML before the card is raised.

### REST `POST /api/config/automation/config/{unique_id}` (write)

Creates or updates a single automation entry in HA's config storage
(`.storage/core.automation`). This is the same endpoint the HA UI automation editor uses.

**Payload shape (all fields required on create):**

| Field | Type | Notes |
|---|---|---|
| `alias` | str | Human-readable name |
| `description` | str | Optional; defaults to `""` |
| `mode` | str | `"single"` \| `"restart"` \| `"queued"` \| `"parallel"` |
| `trigger` | list | Automation triggers |
| `condition` | list | Conditions (optional; may be omitted) |
| `action` | list | Actions to execute |

Returns HTTP 200 on success. The `unique_id` in the URL becomes the automation's permanent
identifier — Pueo owns the namespace via the `pueo_auto_` prefix.

### REST `DELETE /api/config/automation/config/{unique_id}` (rollback)

Removes the automation entry. Used in rollback and in potential future `delete_automation`
tooling.

### REST `POST /api/config/core/check_config` (not used)

This endpoint validates `configuration.yaml` on disk. It does **not** validate automation
storage entries written via the automation config API; those bypass `configuration.yaml`
entirely. It is therefore not useful for pre-write validation of automations and is
excluded from the flow.

### `automation.reload` service (post-write activation)

After the REST write, call `call_service(domain="automation", service="reload")`. This
makes HA pick up the new entry from storage. The resulting entity will appear as
`automation.{slug}` in the state machine within a few seconds.

---

## Flow

```
chat tool call: propose_automation(alias, description, mode, trigger, condition, action)
    │
    ├─[1] WS validate_config(trigger, condition, action)
    │         any component invalid → return error to model; stop
    │
    ├─[2] raise CARD_TYPE_AUTOMATION_CREATE via gate.queue_for_approval(risk=MEDIUM)
    │         payload: {alias, description, mode, trigger, condition, action, unique_id}
    │         dedup_key = f"automation_create_{unique_id}"
    │         user sees the YAML and the validate_config result on the card
    │
    └─[on approval] WorkItem(priority=PRIORITY_HIGH, activity_type="card_execution",
                             dedup_key=f"card_{nid}")
          │
          ├─[3] execute_remote_backup()  ← safety invariant (ADR 002)
          ├─[4] record_backup_slug()
          ├─[5] REST POST /api/config/automation/config/{unique_id}
          ├─[6] call_service(automation.reload)
          ├─[7] verify: REST GET entity automation.{slug} appears in states (poll × 3)
          ├─[8] mark_card_resolved(nid)
          └─[9] emit timeline event
```

---

## Key decisions

### ID prefix: `pueo_auto_`

Clearly separates Pueo-authored automations from user automations, HA-managed automations,
and `ai_agent_auto_` entries from ai_agent_ha. Enables filtered listing and audit in the
dashboard.

**Unique ID generation:** `pueo_auto_{sanitized_alias}_{short_uuid8}` where `short_uuid8`
is the first 8 hex characters of a UUID4. Examples:

- Alias "Turn off lights at sunset" → `pueo_auto_turn_off_lights_at_sunset_3f7a9c12`

The alias slug is lowercase, non-alphanumeric characters replaced with `_`, truncated at
40 characters. If the resulting ID would collide with an existing one, append a new UUID8.

### Scope: chat registry only

`propose_automation` is registered in `build_chat_tool_registry` only.
It is never in `build_ha_tool_registry`, `build_netalertx_tool_registry`,
`build_code_proposal_registry`, or `_MCP_TOOL_NAMES`. Autonomous repair and monitoring
agents do not author new automations.

### Risk level: MEDIUM

Matches the risk tier for `automation.*` service calls in `service_policy.py`. MEDIUM
requires explicit approval at autonomy levels 0–2 (REPORT_ONLY, SUGGEST, AUTO_EXECUTE).
At level 3 (AUTONOMOUS) and level 4 (FULL_AUTONOMOUS) it would auto-execute, but
automation creation involves LLM-generated content that could have unintended side
effects; operators should review the autonomy level setting carefully.

### validate_config is the only pre-approval gate

The WS `validate_config` endpoint is specifically designed for this purpose and is what
the HA UI automation editor calls. The REST `check_config` endpoint validates
`configuration.yaml`, not automation storage, so it adds nothing here.

### Rollback path

`DELETE /api/config/automation/config/{unique_id}` removes the entry; the backup slug is
always recorded first so the HA state can be fully restored if needed. The card payload
includes `unique_id` so the executor has it without any database query.

### No `check_config` sandbox step

The repair pipeline's sandbox step (copy to `.agent_sandbox/`, run `ha core check`, revert)
is applicable to raw YAML in `configuration.yaml`. Automation storage entries are not
touched by `ha core check`, so a sandbox run adds no safety. `validate_config` (WS) is
the correct substitute.

---

## New symbols

### Tool

```python
# utils/agent/tool_executor.py
async def _propose_automation(
    self, alias: str, description: str, mode: str,
    trigger: list, condition: list, action: list
) -> str:
    """Validate then raise a HITL card to create a new automation."""
```

### Card type

```python
# utils/hitl/card_types.py
CARD_TYPE_AUTOMATION_CREATE = "automation_create"
```

### Approval executor

```python
# web/dashboard.py
async def _execute_automation_create(self, nid: int, payload: dict) -> None:
    """backup → REST write → automation.reload → verify → mark card resolved."""
```

### WS helper

```python
# utils/ha/ha_ws_client.py
async def validate_automation_config(
    self,
    trigger: list | None = None,
    condition: list | None = None,
    action: list | None = None,
) -> dict[str, dict]:  # {"trigger": {"valid": bool, "error": str | None}, ...}
```

### REST helper

```python
# utils/ha/ha_rest_client.py
async def create_automation(self, unique_id: str, config: dict) -> None:
async def delete_automation(self, unique_id: str) -> None:
```

---

## ToolDefinition (chat registry)

```python
ToolDefinition(
    name="propose_automation",
    description=(
        "Draft a new Home Assistant automation and send it for human approval. "
        "Validates triggers/conditions/actions before raising the card. "
        "On approval, creates a backup, writes the automation, and reloads. "
        "Always include alias, description, mode, trigger, and action. "
        "condition is optional."
    ),
    parameters={
        "alias": {"type": "string", "description": "Human-readable automation name"},
        "description": {"type": "string", "description": "One-sentence purpose"},
        "mode": {
            "type": "string",
            "enum": ["single", "restart", "queued", "parallel"],
            "description": "Execution mode; 'single' is the safe default",
        },
        "trigger": {"type": "array", "description": "List of trigger objects"},
        "condition": {
            "type": "array",
            "description": "List of condition objects (optional)",
        },
        "action": {"type": "array", "description": "List of action objects"},
    },
    required=["alias", "description", "mode", "trigger", "action"],
)
```

---

## index.html changes (S8)

- Add `"automation_create"` to the `is_actionable` tuple (line ~151).
- Add an `elif ct == "automation_create"` branch in the hint-text block:

```jinja
{% elif ct == "automation_create" %}
  {% set hint_approve = "Create HA backup → write automation to HA storage → automation.reload → verify entity exists" %}
  {% set hint_reject = "Discard; no automation written and no reload triggered" %}
```

- Render the YAML payload in a collapsible `<pre>` block (same as `code_proposal`).

---

## Testing plan

| Layer | File | What to test |
|---|---|---|
| Unit — WS client | `test_ha_ws_client.py` | `validate_automation_config` happy path and partial-field call |
| Unit — REST client | `test_ha_rest_client.py` | `create_automation` / `delete_automation` request shape |
| Unit — tool | `test_tool_executor.py` | validate error stops before card raise; happy path raises card |
| Unit — registry | `test_tool_registry.py` | `propose_automation` in chat registry; absent from ha/netalertx/code-proposal registries |
| Unit — dashboard | `test_dashboard.py` | `_execute_automation_create` backup→write→reload→verify sequence; mark_card_resolved called |
| Seam | `tests/integration/` | End-to-end card approval with `FakeHARestClient` + `FakeHAWebSocketClient` |
| Eval | `tests/integration/test_evals.py` | (S10) Chat request → card creation scenario |

---

## Sessions (for reference)

| Session | Scope |
|---|---|
| S7 (this) | Spec + ADR only |
| S8 | `propose_automation` tool + `validate_automation_config` WS helper + `CARD_TYPE_AUTOMATION_CREATE` + card template + REST helpers (create/delete) + stub executor + tests |
| S9 ✅ | `_execute_automation_create` (backup→write→reload→verify) + security review + timeline event — PR #815 |
| S10 ✅ | Seed runbook + eval scenario + end-to-end exercise — PR #817 |
