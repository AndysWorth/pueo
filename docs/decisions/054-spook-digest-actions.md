# ADR 054 — Spook Digest Actions: Repair-Flow Driver, Ignore vs Fix

**Status:** Accepted  
**Date:** 2026-11-04

## Context

After S1–S3 landed, the Spook digest card shows per-item cleanup items with a
label and a "Spook can fix" / "manual action needed" badge. Users had no way to
act on individual items from the card.

HA exposes two paths for acting on a repair issue:

1. **Fix flow** — `POST /api/repairs/issues/fix` starts a flow; subsequent
   `POST /api/repairs/issues/fix/{flow_id}` advances it. Most Spook cleanup
   flows are a single confirm step and complete immediately.
2. **Ignore** — WS `repairs/ignore_issue` with `{"domain": ..., "issue_id": ...,
   "ignore": true}` suppresses the issue in HA without deleting it. The issue
   remains in HA's registry but is hidden from the user. It becomes visible again
   if the integration re-creates it.

The old dismiss path (`DELETE /api/repairs/issues/{domain}/{issue_id}`) was not a
real HA route; it returned 404s and was never successful (finding #6 in the plan).

## Decision

### Per-item card actions

Each item in a `CARD_TYPE_SPOOK_DIGEST` card gets two optional buttons:

- **Apply fix** (shown only when `is_fixable=True`): fallible preconditions
  (issue still listed, `is_fixable`) → `execute_remote_backup()` → `drive_repair_flow`
  → update card's `_resolved` list → close card when all items resolved.
- **Ignore in HA** (shown for all items): WS `repairs/ignore_issue` →
  update `_resolved` → close card when all items resolved.

### No standard Approve/Reject for spook_digest

The standard Approve/Reject buttons are not shown for `spook_digest` cards.
Actions happen per-item via the new buttons. The card closes automatically once
all items are resolved.

### drive_repair_flow

`drive_repair_flow(rest, domain, issue_id, *, menu_choice=None, max_steps)` in
`utils/ha/config_flow_driver.py` mirrors `drive_config_flow` but uses the
repairs fix-flow endpoints:
- `POST /api/repairs/issues/fix` to start
- `POST /api/repairs/issues/fix/{flow_id}` to advance (empty body for confirm
  steps; `{"next_step_id": menu_choice}` for menu steps)
- `DELETE /api/repairs/issues/fix/{flow_id}` to abort

Returns `FlowResult(success=True)` on `create_entry`; aborts cleanly on errors
or unexpected step types. When a `menu` step is encountered without a valid
`menu_choice`, the flow is aborted immediately and returns `abort_reason=
"menu_unhandled"` — it never submits `{}` to a menu step (which causes an
infinite loop).

Each step is logged at DEBUG as `repair_flow_step` with `step_id`, `type`, and
`menu_options`.

### Per-key fix action metadata

Each item in a `CARD_TYPE_SPOOK_DIGEST` card now carries:
- `fix_label` — human-readable button label (e.g. "Delete area", "Clear statistics")
- `fix_effect` — one sentence describing what the fix does, shown under the item label
- `menu_choice` — the `next_step_id` to submit when the driver encounters a menu step

Defined in `_FIX_ACTION` in `utils/ha/spook_digest.py`. `_execute_spook_item`
reads `menu_choice` from the item and passes it to `drive_repair_flow`.

### Per-item error tracking

Fix failures no longer reject the whole card. Instead:
- `payload["_errors"][issue_id]` is set with the error message
- The card stays PENDING so the user can retry or ignore the remaining items
- After a successful fix for the same item, its error is cleared
- After `create_entry`, the issue list is re-queried to confirm the issue is gone;
  if it is still present, that also sets `payload["_errors"][issue_id]`

### ignore_repair_issue

Added to `HAWebSocketClientProtocol`, `HAWebSocketClient`, and
`FakeHAWebSocketClient`. WS type: `repairs/ignore_issue`.

### Repair-flow REST methods on HARestClient

`start_repair_flow(domain, issue_id)`, `submit_repair_flow(flow_id, data)`,
`abort_repair_flow(flow_id)` added to `HARestClient`, `HARestClientProtocol`,
and `FakeHARestClient`.

### Existing CARD_TYPE_HA_REPAIR dismiss

The existing dismiss path for `CARD_TYPE_HA_REPAIR` cards (action="dismiss") now
uses WS `ignore_repair_issue` instead of REST DELETE. The REST DELETE endpoint
does not exist in modern HA.

### WorkItem routing

Both `/spook/{nid}/{issue_id}/fix` and `/spook/{nid}/{issue_id}/ignore` are
queued as `WorkItem(PRIORITY_HIGH, activity_type="card_execution",
dedup_key=f"card_{nid}:{issue_id}")`, which serialises concurrent HA writes
(ADR 026).

The fix path is MEDIUM risk (requires backup). The ignore path makes no config
change and needs no backup.

### Resolved state persistence

Resolved issue IDs are stored in `payload["_resolved"]` on the card's JSON file
so the template can mark each item as done across page reloads without keeping
in-memory state.

## Consequences

- Each Spook cleanup item can be acted on independently.
- Fix and ignore both use HA's supported APIs; no more 404-generating REST DELETEs.
- The card stays open with a progress view until every item is handled.
- `drive_repair_flow` can be reused for any HA integration that exposes a repair flow.
- Fix failures are shown per-item in the card; the whole card is no longer silently
  rejected and hidden from the queue on a single-item failure.
- Spook registry-cleanup flows (empty_areas, unused_labels, unused_blueprints,
  orphaned_statistics) work correctly because the driver submits the `remove` menu
  choice instead of looping on empty input.
