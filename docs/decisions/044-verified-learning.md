# ADR 044 — Verified learning: distill after verification, per-symptom seeds, card feedback

**Status:** Accepted  
**Supersedes:** Runbook lifecycle sections of ADR 038

---

## Context

After two earlier runbook overhauls (PRs #819–#833, #872–#885), zero runbooks had ever
validated.  A review of live Pueo activity on 2026-10-14 to 2026-10-16 identified three
root causes:

1. **No runbook could validate.** The repair verifier split `issue_key` on `:`, but keys
   use `/` (`hacs/restart_required_…`).  Update, host, notification and lovelace sessions
   had no verifier at all.  Auto-validate only runs when a new usage row is recorded, so
   even a hypothetically-repaired verifier would never retro-grade prior rows.

2. **Distillation wrote candidates before verification.**  A session that concluded "no
   action required" spread its wrong answer into `[PAST REPAIR]` pre-injection before
   any check ran.  The evidence-count gate (≥3 calls) skipped short-but-correct sessions
   and the distillation prompt asked for "the most general level", stripping instance
   facts.

3. **Seeds could never be matched.**  Monolithic per-session-type seeds had no
   `signature:` in their front matter, so `_fetch_exact_signature_chunk` always missed
   them.  When found by semantic retrieval they were cut to 300 tokens, removing the
   `restart_required` section entirely.

---

## Decisions

### 1. Per-symptom seeds with wildcard signatures (S4, PR #905)

Seeds are split into one file per symptom (`seed_repair_restart_required.md`,
`seed_host_rpi_firmware.md`, …).  Each has `signature:` front matter with wildcard
support (`repair:*:restart_required`).  `_fetch_signature_chunks` returns both the
matching seed and the best-matching learned runbook, each uncut at 1 200 tokens.
`agent_memory.signature` (V40 migration) stamps sessions so `remember()` facts are
pre-injected only for the matching symptom.

### 2. Session outcomes table and system verifiers (S2, PR #901)

`session_outcomes` (V39 migration) stores `episode_id`, `signature`, `session_type`,
`subject_key`, `conclusion_json`, `distill_payload_json`, `verification`, `verified_at`,
`verified_by`, `correction`.  The stashed `distill_payload_json` holds the steps,
initial context and episode stub needed for a later distillation LLM call.

System verifiers set `verification='success'` or `'failure'`:
- **Repair:** the reconcile loop marks success when the issue leaves HA's list.
  Reappearance within 24 h → failure.  Resolves by `episode_id`, not signature.
- **Update:** install OK with no new repair/error for 1 h → success; install failed →
  failure.
- **Host/notification/lovelace:** success via card action only (see below).

A 48 h sweep marks unresolved rows `unverified` and clears `distill_payload_json`.

> **Amended (#941, V41):** the sweep holds rows whose HITL card is still open (max 14 days), and it never distills.
> An unverified session is not evidence. `distilled_at` prevents re-distillation on restart.
> Grounded `instance_facts` are written to `agent_memory` even when `no_new_knowledge=true`.

### 3. Card actions as verification (S3, PR #903)

`/approve`, `/acknowledge`, `/apply-fixes` → `verification='success'` (by=user).  
A new **"Pueo got this wrong"** button (`POST /wrong/{nid}`) with an optional
one-line correction → `verification='failure'` with the correction stored.

### 4. Distill only after verification (S5, PR #906)

`distill_and_persist` now accepts `verification` and `correction` parameters.

- Non-success outcome **without** verification → write gap (unchanged).
- Success outcome **without** verification → log INFO `distillation_skipped
  reason=pending_verification` and return.
- `verification='success'` → LLM distillation call.
- `verification='failure'` **with** `correction` → LLM distillation call with
  correction as ground truth.
- `verification='failure'` without correction → log INFO and return.

The `_MIN_EVIDENCE_CALLS` gate is removed.  Every skip is logged at INFO with a reason.

The new **signature-scoped prompt** includes:
- the matched seed text ("baseline for this exact signature")
- the correction when present
- the verified outcome
- asks for the **delta**: `instance_facts`, `decision_rule`, pitfalls not in the seed
- `no_new_knowledge: bool` — when true the LLM found nothing new; only usage is
  recorded, no runbook is written

`enqueue_distillation_after_verification(episode_id, verification, correction, …)` is
called by dashboard card endpoints and system verifiers after resolving a session
outcome.  It reads the stashed payload, fetches the seed text, and submits a PRIORITY_LOW
WorkItem.  Returns False when the queue is absent, the payload was cleared, or there is
no correction for a failure.

### 5. Verified-only repair history embedding

`repair_episode_embedder._load_unembedded_episodes` LEFT JOINs `session_outcomes` and
only embeds episodes with `verification IN ('success', 'failure')`.  Pre-S2 episodes
(no session_outcomes row) embed as before.  Failure episodes carry the correction text
in their embedding document.

---

## Consequences

- Distillation lag: candidates appear only after verification, which may be hours or days
  after a session.  This is the intended behaviour.
- Candidates written before S5 that bypassed verification remain in the DB; discard any
  clearly wrong ones via the `/runbooks` dashboard tab.
- Standalone runs (no supervisor queue) still write gaps for non-success sessions but
  never distill success sessions — the same policy as supervisor mode.
