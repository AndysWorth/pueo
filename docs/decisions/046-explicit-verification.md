# ADR 046 — Explicit verification: "Pueo was right", executors verify, card actions don't

**Status:** Accepted  
**Date:** 2026-10-20  
**Supersedes:** ADR 044 §3 ("Card actions as verification") and the "success via card action only" verifier row of ADR 044 §2
**Amended by:** ADR 050 (prediction checks as system verification; host-health clear no longer counts as success)

---

## Context

ADR 044 made card actions count as verification: `/approve`, `/acknowledge` and `/apply-fixes` marked the session `success` (by=user). After S1 (#941) stopped the sweep from distilling unverified sessions, card actions were the main source of "verified" sessions. The 2026-10-20 review showed that they verify the wrong thing:

- **Acknowledge means "seen", not "correct".** The http-login card (9a05e6c4) raised a CRITICAL verdict based on a missing NetAlertX token. It also stated an owner ("Robbie's phone") that appears in no tool output. Pressing Acknowledge would have marked that session as a verified success and distilled the hallucination into a candidate runbook.
- **Approve marked success before the action ran.** A failed update install or a failed `ha` command still counted as verified.
- **Gate-queued cards were not linked to their session.** `run_ha_command`, `call_service`, `reload_integration`, `propose_automation` and `apply_fix` cards carried no `episode_id`. Their sessions ended as `awaiting_approval`. That wrote a knowledge gap immediately, and nothing ever verified them.
- **Some cards could not be judged.** Informational host-health, lovelace and HA-repair card sessions auto-verified at session end, before the user saw the card. Auto-applied updates did the same, before the install ran.
- **Manual updates were never verified.** A manually approved update card never recorded its install result. Only the auto-apply path did.

## Decision

1. **An explicit user verdict.** `POST /right/{nid}` ("Pueo was right") marks the session `success` (by=user) and enqueues distillation. On informational cards, the form sends `acknowledge=1`, and the card is also acknowledged. On actionable cards, the card stays pending for Approve/Reject, and `payload.user_verdict = "right"` replaces the verdict buttons. `POST /wrong/{nid}` is unchanged. Both buttons also appear on the Notifications tab, where notification cards (such as http-login) render; before this change, that tab had no verdict buttons. A `return_to` form field (allowlisted to `/queue` and `/notifications`) sends the user back to the page they came from.
2. **Card actions verify nothing.** `/approve`, `/acknowledge` and `/apply-fixes` no longer resolve session outcomes. A session that is only acknowledged stays pending. The sweep then clears it once its card is closed (ADR 044 amendment, #941).
3. **Executors are system verifiers** for card types that have no other verifier. `_verify_card_execution(payload, verification)` in `web/dashboard.py` resolves the session by `episode_id` (by=system) and enqueues distillation:
   - `ha_command`, `service_call`, `config_entry_reload`, `automation_create` and the queued repair fix (`apply_fix`) mark `success` when the action completes. An execution error leaves the session pending, because a missing token or an SSH failure says nothing about the analysis.
   - `update` (manual approval) records the install result: `success` or `failure`. This matches the auto-apply path.
   - Repair issues (reconcile loop) and host-health alerts (condition clears) keep their existing verifiers.
4. **Any session that sends a card with its `episode_id` defers verification.** This covers notification, update, host-health (including informational), lovelace, HA-repair and gate-queued cards, plus auto-apply updates. `ToolExecutor._mark_card_queued(nid)` sets `_hitl_card_queued` and `_hitl_card_id`. Sessions that send no card still auto-verify at session end.
5. **`awaiting_approval` is not a failure.** It writes no gap. The session waits for its card's executor or for the user's verdict.

## Consequences

- Fewer sessions get verified, and the ones that do are trustworthy. Informational, lovelace and HA-repair cards teach Pueo only when the user presses "Pueo was right".
- The user's verdict is separate from the action. A user can approve an update without vouching for the analysis, or confirm the analysis without approving the update yet.
- An approved action that fails to execute leaves its session pending. The sweep clears it unverified.
- Rollback: revert the commit. No schema change; `session_outcomes.card_id` and `distilled_at` come from V41.
