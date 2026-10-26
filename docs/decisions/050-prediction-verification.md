# ADR 050 — Verification without waiting on the user: prediction checks, daily digest, supersede

**Status:** Accepted
**Date:** 2026-10-26
**Amends:** ADR 046

---

## Context

ADR 046 made verification explicit: only "Pueo was right"/"Pueo got this wrong" and system verifiers (card executors, repair reconcile, update install result) close a session. The 2026-10-26 review showed that this leaves most sessions unverified:

- All 13 device-availability sessions stayed `pending` because nobody answered their cards. They were swept to `unverified` and taught Pueo nothing.
- A device card's nid is stable per config entry (`stable_nid("device_offline:<entry>")`), so each new session overwrote the card's `episode_id`. The older session rows could never be answered, but the open card held them for 14 days and they inflated the "pending" counts.
- Host-health marked a session `success`/system whenever its finding cleared. An `unexpected_reboot` finding clears when the reboot ages out of its window, so the 10-26 host-health session (which guessed "brownout") was recorded as verified. That is a fake verification.

---

## Decision

### 1. Prediction checks (system verification)

`finish_device_availability` and `finish_host_health` take an optional `prediction`:

```json
{"kind": "recovers_without_action" | "recovers_after_reload" | "stays_offline", "within_minutes": N}
```

`AgentLoop` validates it (`normalize_prediction`, capped at 7 days) and stores it in the episode stub, so it lands in `session_outcomes.conclusion_json`. No migration is needed. The card shows "Pueo predicts this will … and will check that itself."

The poll that watches the condition judges it with `judge_prediction()` (`utils/knowledge/session_verification.py`):

| kind | Cleared on time | Cleared late | Still present at deadline |
|---|---|---|---|
| `recovers_without_action` | success, or undecided if anyone acted | failure | failure |
| `recovers_after_reload` | success if a reload ran, else failure | failure | failure |
| `stays_offline` | failure | success | success |

A verdict marks the session `success`/`failure` with `verified_by='system'`. A failure gets an automatic correction ("Predicted it would recover on its own within 30 min; it was still not recovered after 75 min."). Then `enqueue_distillation_after_verification` runs.

- **Devices:** `poll_device_availability` calls `check_device_predictions` after each evaluation that wasn't skipped (states inside the settle window prove nothing). A card counts as recovered only when none of its devices is still alerted. Sessions are matched by `card_id`. "Actioned" means the card was actioned or the session made a successful `reload_integration`/`call_service`/`run_ha_command` call.
- **Host health:** `ResourcePoller._check_host_health` calls `verify_host_health_sessions` with the cleared and still-active finding keys.

This doesn't contradict ADR 046's "recovery is not verification". An unpredicted recovery still verifies nothing. A prediction is a falsifiable claim made before the outcome was known, and the system checks it against the outcome.

### 2. Host-health clear is not success

The unconditional `resolve_session_outcome(key, "success", "system")` on clear is removed. A cleared finding verifies a session only through its prediction, or as success when the session itself took an action. `unexpected_reboot` (`_TRANSIENT_HOST_KINDS`) is never verified this way, prediction or not, because it always clears on its own.

### 3. Daily question digest

Once a day, at or after `VERIFICATION_DIGEST_HOUR` local time (default 9; -1 turns it off), the `backup_sync` loop calls `maybe_send_verification_digest`. It sends one `verification_digest` card that lists every open card with a `verification_question` whose session is still pending. Each row has Yes / No (with a correction) and, for device cards, Off on purpose. They post to the existing `/right/{nid}`, `/wrong/{nid}` and `/off/{nid}` handlers. Yes on a non-actionable card also acknowledges it, using the same rule as the queue template (`ACTIONABLE_CARD_TYPES` mirrors the template tuple, and a test keeps them equal). `user_prefs` `verification_digest:last_date` records the day, and an empty day still counts as sent. When the queue renders, rows whose card was answered are dropped. A digest with no rows left is hidden.

### 4. Supersede

When `write_session_outcome` stores a row with a `card_id`, older pending rows with the same `card_id` become `verification='superseded'`. Their `distill_payload_json` is cleared and their `runbook_usage` rows are marked `superseded` too. Superseded rows are no longer pending, so they drop out of the 14-day open-card hold. The learning-health table excludes them.

---

## Consequences

- Device sessions can verify themselves within minutes or hours instead of expiring unverified. Their failures carry a correction, so the verdict lane learns from both outcomes.
- A model that always predicts `stays_offline` will only be "verified" when devices really stay off, which is the case the post-restart sweep is meant to recognize. The prediction is optional and the tool description asks for one only when the evidence supports it.
- The digest adds at most one card a day.
- Verification states are now `pending`, `success`, `failure`, `unverified` and `superseded`.
