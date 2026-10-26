# ADR 049 — Procedural distillation lane

**Status:** Accepted
**Amends:** ADR 044, ADR 046

---

## Context

All 8 post-session distillation runs after S1/S2 returned `no_new_knowledge`.  The
root cause is that verdict-based learning (ADR 044/046) requires explicit user
verification, which has never happened.  Procedural knowledge — which tools returned
evidence, which calls failed, correct command shapes — does not require a verdict and
can be extracted from the trace alone.

---

## Decision

Add a **procedural distillation lane** that fires after any session that reached a
conclusion (`success`, `needs_action`, `monitoring`, `awaiting_approval`).  It is
explicitly excluded from `failed` and `stuck` outcomes, which already produce gaps.

### Scope of a procedural lesson

A procedural lesson describes **how** to gather evidence — not what the outcome was.
Valid lessons:
- "Tool X failed at step N because REST client was not available"
- "Tool Y returned empty output — consider using tool Z instead"
- "The same call was made at steps 3 and 7 with identical arguments (redundant)"

Invalid lessons:
- "The device was offline because of a Wi-Fi drop" (verdict)
- "The fix was to restart the integration" (verdict)

The LLM prompt explicitly forbids verdicts and is constrained to the `ProceduralLessons`
schema (`{lessons: list[{lesson, evidence_step}]}`).

### Grounding

Each lesson must cite a step number.  The same `_fact_is_grounded` check used for
`InstanceFact` is applied against the cited step's args + output + error.  Lessons
that cannot be grounded against any step output are discarded.

### Storage

Grounded lessons are merged into the signature's **candidate** runbook under a
`## Procedure notes` section.  When no live runbook exists, a **procedure-only
candidate** is created with `fix_summary`/`decision_rule` empty.

Notes are deduplicated by normalised text (lowercase, collapsed whitespace) and capped
at 8 per signature.

Procedure-only candidates do not auto-promote to validated (`_auto_validate_if_eligible`
requires `runbook_usage.verification='success'` rows, which are only written after a
verified verdict session).

### Pre-injection

`_fetch_signature_chunks` splits the `## Procedure notes` section from the runbook
body and returns it as a separate `KnowledgeChunk` with `metadata["procedure_note"]=True`.
`_knowledge_authority_label` emits `[PROCEDURE NOTES]` for this chunk.  This keeps the
main runbook label (`[RUNBOOK FOR THIS EXACT SYMPTOM]`) unambiguous and allows the
agent to distinguish procedure guidance from the verdict runbook.

### No `distilled_at` stamp

`distill_procedure` deliberately does **not** stamp `session_outcomes.distilled_at`.
That stamp is reserved for the verdict lane so the 48-hour sweep and startup recovery
can detect sessions that still need verdict distillation.

### Sequencing with verdict lane

When a session auto-verifies (success, no HITL card), both lanes fire:
1. `enqueue_procedural_distillation` — PRIORITY_LOW, `dedup_key=procedure_<sig>_<ep>`
2. `enqueue_distillation_after_verification` — PRIORITY_LOW, `dedup_key=distill_after_verify_<ep>`

Both run through `PueoWorkQueue`, so at most one LLM call is in flight at a time.
The verdict lane stamps `distilled_at`; the procedure lane does not.

---

## Consequences

- Sessions that reached a conclusion now produce at least procedure notes even when no
  user verification ever happens.
- The `## Procedure notes` section in a runbook is never rendered as part of the
  `[RUNBOOK FOR THIS EXACT SYMPTOM]` block — it appears separately as `[PROCEDURE NOTES]`.
- Procedure-only candidates are visible in the `/runbooks` tab and labelled
  `[CANDIDATE RUNBOOK – unreviewed]` until a verified verdict merges a real approach.
- Two additional WorkItems are submitted per qualifying session.  Both are PRIORITY_LOW
  and serialised through the existing queue — no concurrency change.

---

## Amendment (2026-10-26, #991): deterministic base + relaxed grounding

**Context.** In the first week the lane rejected 55 of 60 lessons as ungrounded.
`_fact_is_grounded` was built for instance facts: it requires capitalized words and
content words to appear in the output, and a meta-lesson ("Step 3 was redundant",
"FAILED") never appears in tool output.  The few accepted notes restated log content
("read_logs returned a noaa_coops traceback").  The useful lessons were really tool
bugs.

**Decision.**
1. **Deterministic extractor first.** `extract_procedure_facts(steps)` is pure and
   needs no LLM.  It notes failed calls with a normalised error (httpx URL noise
   removed, config entry ids and UUIDs → `<id>`), a failed call followed by a
   successful retry of the same input with a corrected value (`humidy` →
   `humidifier.humidy`), identical calls repeated in one session (`reason` args
   ignored), and empty results.  It skips approval hand-offs, `finish_*` tools,
   environment errors ("client not available", timeouts) and empty log searches,
   since an absent log line is evidence.  Failures come before repeats and empties
   because the note list is capped.  Its notes are persisted even when the LLM
   call fails, and they're listed in the LLM prompt as "already recorded".
2. **Relaxed LLM grounding.** `_lesson_is_grounded` replaces `_fact_is_grounded`
   for lessons.  The lesson must name a non-terminal tool from the trace.  Every
   distinctive token (digit or `._:/-`) must appear in the args or output of the
   cited step or any step it mentions as "step N".  Tool names, cited step numbers
   and repeat counts ("2x") count as present.  There is no content-word rule.
   Log-reader steps (`read_logs`, `search_log`, `read_pueo_log`,
   `summarize_log_window`) contribute only their call, not their output, so
   restating log content is rejected.  Replayed against the week's 64 rejected
   lessons, 22 now pass.  The rest are verdicts, "no calls were redundant", log
   restatements, or lessons that name no tool.
3. **Recurring quirks.** Each (tool, problem) is counted once per session in a
   `user_prefs` row `procedure_quirk:<hash>` (count, signatures, example call, last
   seen).  No migration is needed.  The `/runbooks` learning-health section lists
   the top 10, so a quirk gets fixed in the tool instead of being relearned per
   signature.

**Consequences.** Procedure notes now accumulate from every conclusive session with
tool problems, whatever the LLM produces.  Note quality depends on tool error text,
so tool errors should say what to do next.  The #991 fixes do this:
`get_integration_diagnostics` 404 → "<domain> does not provide diagnostics", ULID
entry ids resolve, and an unknown id gets a "pass the domain" hint.
`search_integrations` gives a domain hint plus entity-registry matches.
`read_logs(domain=…)` filters the core log to one integration.
`get_update_release_notes` strips the scraper's `STUB:` marker.
