# ADR 026 — No concurrent LLM use; no concurrent HA interaction

## Status
Accepted

## Context

ADR 025 introduced `PueoWorkQueue` to serialize `AgentLoop` submissions from background monitoring loops — log triage, notification analysis, update analysis, lovelace investigation, NetAlertX diagnosis, disk recovery, and chat. The queue's single consumer guarantees that only one AgentLoop runs at a time.

However, card handlers in `web/dashboard.py` were dispatched via bare `asyncio.create_task()` outside the queue:

- `POST /approve/{nid}` dispatched `_execute_queued_fix`, `_execute_queued_update`, `_execute_queued_ha_repair`, `_execute_cloud_escalation`, `_execute_disk_recovery`, `_execute_resource_action`, `_execute_code_proposal`, `_execute_dashboard_entity_fix`, and `_execute_unregistered_entity` — all as unmanaged fire-and-forget tasks.
- `POST /apply-fixes/{nid}` dispatched `_execute_config_fixes` the same way.

This left two hard requirements unenforced during supervised operation:

1. **No concurrent LLM use**: `_execute_cloud_escalation` runs a full `AgentLoop` with the Anthropic API. Approving an escalation card while an autonomous repair was running started a second concurrent LLM session — doubling inference load and producing inconsistent tool-call interleaving.

2. **No concurrent HA interaction**: handlers like `_execute_queued_fix` (backup → sandbox → atomic swap), `_execute_queued_update` (HA Core/OS update + restart), `_execute_queued_ha_repair` (reboot/restart), and `_execute_disk_recovery` (SSH commands + REST purge) perform the same classes of HA operations as monitoring-loop-triggered repair pipelines. Running them concurrently with an active repair WorkItem could produce: two simultaneous `ha backup new` commands (undefined behavior on HA), two concurrent writes to `/config/.agent_sandbox/configuration.yaml`, or one handler calling `ha core restart` while another is mid-SSH-interaction.

Three secondary bugs:

- **Double-approval race**: `_status()` did not check for `.in_progress` files, so two rapid POSTs to `/approve/{nid}` before `.approved`/`.rejected` landed both saw "PENDING" and each spawned a separate handler task for the same card.
- **Orphaned `.in_progress` files**: `_execute_queued_fix`, `_execute_cloud_escalation`, and `_execute_code_proposal` had no `finally` block to clean up the `.in_progress` sentinel on success or failure, permanently marking the system as "executing" in the UI status pill.

## Decision

### Hard constraint 1 — No concurrent LLM

During supervised operation (when `PueoWorkQueue` is initialized): only one `AgentLoop.run()` or judgment-call LLM use may execute at a time. Enforced by `PueoWorkQueue`'s single consumer — if an item is running, all other submitted items wait.

### Hard constraint 2 — No concurrent HA interaction

During supervised operation: only one operation that writes to or restarts Home Assistant (SSH config write, `ha backup new`, sandbox test + swap, `ha core restart`, `ha os update`, HA REST write) may execute at a time. Enforced by the same `PueoWorkQueue` serial consumer. All card handlers are subject to this rule, not just those involving LLM.

### Implementation

**Card handler dispatch**: Both dispatch paths in `approve()` (CARD_TYPE_REPAIR special case and general `_CARD_DISPATCH` handlers) now submit a `WorkItem` to `PueoWorkQueue` instead of calling `asyncio.create_task`. The `apply_fixes()` endpoint does the same for `_execute_config_fixes`.

WorkItem parameters for all card handlers:
- `priority=PRIORITY_HIGH` — user-approved actions run before background analysis (NORMAL=30) but after live-triggered repairs (CRITICAL=10)
- `activity_type="card_execution"`
- `dedup_key=f"card_{nid}"` (or `f"config_fixes_{nid}"` for config fixes)
- `suppress_while_running=frozenset()` — no additional suppression needed; serial execution already prevents overlap

**Double-approval prevention**: `dedup_key=f"card_{nid}"` drops a second submission for the same card if the first is already running or pending. An additional early-exit guard at the top of `approve()` redirects immediately if `{nid}.in_progress` already exists, closing the narrow race window before the first `submit()` call.

**Fallback**: When `get_work_queue_or_none()` returns `None` (standalone scripts, unit tests), the original `asyncio.create_task` path is used — no behavioral change in non-supervised contexts.

**`finally` cleanup**: `_execute_queued_fix`, `_execute_cloud_escalation`, and `_execute_code_proposal` gain outer `try/finally` blocks that call `(watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)` on all exit paths, consistent with the other eight card handlers.

## Priority rationale

| Priority | Value | Rationale |
|---|---|---|
| CRITICAL | 10 | Live log-triggered repair — active HA failure, time-sensitive |
| HIGH | 20 | User-approved card execution, notification analysis, disk recovery |
| NORMAL | 30 | Background analysis, chat, update analysis |
| LOW | 40 | Routine / deferrable |

`PRIORITY_HIGH` for card handlers reflects that the user has explicitly approved an action and is waiting for feedback, but a live HA failure (CRITICAL) takes precedence.

## Consequences

- All card handler executions are strictly serial with every other HA/LLM operation in the queue. A user approving a card while a repair is running waits for the repair to complete, then the card executes.
- Double-approval for the same card is impossible during supervised operation: the dedup key drops the second submission.
- `.in_progress` files are always cleaned up on exit, regardless of the exit path.
- Existing unit tests are unaffected: `get_work_queue_or_none()` returns `None` in test context (queue never initialized), so all handlers fall through to the original `asyncio.create_task` fallback path.

## Related decisions

- [ADR 002 — Backup-before-write safety invariant](002-safety-invariant.md): the no-concurrent-HA constraint is an extension of the same principle — a second concurrent HA write is as dangerous as a write without a backup.
- [ADR 025 — PueoWorkQueue](025-serialized-work-queue.md): the enforcement mechanism; card handlers are added to the callers table.
