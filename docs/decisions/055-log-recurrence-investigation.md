# ADR 055 — Recurring Log Errors Start a Read-Only `log:*` Investigation

**Status:** Accepted
**Date:** 2026-11-06
**Issue:** #1132

## Context

`tail_remote_log_stream` already counted recurrences per fingerprint
(`log_recurrence:<fp>` in `user_prefs`, `LOG_RECURRENCE_THRESHOLD = 3` within a
7-day window). Crossing the threshold only forced the line through the one-shot
`analyze_log_line_with_ai` triage. That triage still applied the
`CONFIDENCE_THRESHOLD` (0.7) gate, so the daily noaa_tides `COOPSAPIError` (scored
0.3–0.45, "not actionable") never reached anything that could investigate it.

Lowering the gate would not have helped. Past it, the only paths were:

- a generic "Actionable log event" card whose body is the triage summary; and
- `trigger_remediation_pipeline` → `ha_agent_sandbox_engine.main()`, which only
  sees `configuration.yaml`, never the log error, and runs with no signature.

No code created `log:*` sessions, although `log` is a scoped signature family. The
live signature was also `log:unknown:coopsapierror`: the trigger line is
`Traceback (most recent call last):`, so the logger on the preceding header line
was never read.

Options considered:

1. **Dedicated read-only investigation session** (chosen).
2. Bypass the confidence gate for recurring lines. Rejected: it only leads to the
   generic card or the config-only sandbox engine.
3. Thread the log context into the sandbox engine. Rejected: the sandbox engine is
   a write pipeline; a recurring error usually needs advice, not a config rewrite.

## Decision

### Routing

When a critical block's fingerprint reaches the threshold, `_route_recurring_error`
replaces the triage call:

1. A traceback whose innermost frame is in `custom_components/<domain>/` still
   gets the integration-bug card (unchanged precedence).
2. Otherwise `_log_recurrence_should_investigate(fp)` checks `should_send_card`
   for both `log_recurrence:<fp>` (the card / cooldown row) and
   `log_recurrence_backoff:<fp>` (the 30-minute stuck-loop row).
3. If allowed, a `WorkItem(PRIORITY_HIGH, activity_type="log_recurrence",
   dedup_key="log_recurrence:<fp>")` runs `_run_log_recurrence_investigation`.
   Without a work queue (tests, standalone), it is awaited directly.

Recurring lines skip both the one-shot triage (its verdict is what recurrence
overrides) and the transient-error skip. The seed decides that transient outages
need no card. Non-recurring lines are unchanged.

### Logger resolution

`_resolve_block_logger(trigger, prev, block)` replaces `_extract_logger` at the
call site. A `Traceback` trigger takes the logger from the preceding header line.
A generic core logger (`homeassistant.helpers.*` etc., not
`homeassistant.components.*`) is refined from the innermost
`homeassistant/components/<d>` or `custom_components/<d>` frame. The noaa_tides
error now signs as `log:homeassistant_components_noaa_tides:coopsapierror`.
Recurrence counts recorded under the old empty logger restart.

### Session

`_run_log_recurrence_investigation` mirrors `_run_repair_issue_investigation`:

- `ReadOnlySSHClient` (ADR 043) and the real `AutonomyGate(AUTONOMY_LEVEL)`.
- REST/WS clients, knowledge store and environment profile.
- `sig_log(logger, exc_class)` as the signature and the fingerprint as `subject_key`.
- Initial context: logger, exception, count, window, and the error block
  (≤ 800 tokens).
- Prompt `task_log_recurrence.md`, which carries no verdict rules. The verdicts
  live in `seed_log_recurring.md` (`log:*:*`); the COOPSAPIError rule moved there
  from `seed_integration_error.md`.

On `outcome != "success"`, the session writes `log_recurrence_backoff:<fp>`.

### Terminal tool and card

`finish_log_investigation` is registered in `build_ha_tool_registry`, not MCP. It
reads the pending error that the runner set with `set_pending_log_error()`:

- `requires_hitl=true` → an advise-only `CARD_TYPE_LOG_RECURRENCE` card (stable
  nid from `log_recurrence:<fp>`, `informational`, `verification_question`,
  `signature`), plus `mark_card_sent`. The card is not in `is_actionable`.
  Acknowledge routes to `/acknowledge` (Known Issue), because `/approve` would
  resolve the row and re-fire on the next occurrence. "Pueo was right" / "got
  this wrong" verify the session (ADR 046).
- `requires_hitl=false` → `mark_card_deferred` for `LOG_TRIAGE_COOLDOWN_HOURS`.

So a recurring error gets one session per suppression window, not one per
occurrence.

## Consequences

- Real `log:<logger>:<exc>` sessions now exist and feed the procedural and verdict
  distillation lanes.
- One extra LLM session per recurring fingerprint per cooldown window, serialized
  through `PueoWorkQueue` (ADR 026).
- `event_subscriber` is not threaded from `main.py` into the log monitor: the
  subscriber is created after the log monitor starts. The session works without
  it (no recent-events tool context).
- No config key, migration or dependency change. The threshold stays the module
  constant `LOG_RECURRENCE_THRESHOLD`.
