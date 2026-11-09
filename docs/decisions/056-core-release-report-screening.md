# ADR 056 — Core release report + screening pass

**Status:** Accepted  
**Closes:** #1142

## Context

Before this ADR, `get_update_release_notes` for HA Core updates fetched the blog post
via `fetch_url`, which is capped at 8 000 chars.  For a typical Core release the blog is
~127 k chars; the "Backward-incompatible changes" section starts at ~100 k.  This meant
the main update-analysis AgentLoop never saw the BC list.

S1 (#1139) fixed the blog-URL regex and BC domain mapping.  S2 (#1141) added
`build_core_release_report()` — a deterministic, no-LLM step that fetches the blog and
changelog once, parses them into a structured `CoreReleaseReport` JSON, and persists it.

This ADR adds S3: a **screening pass** (one or more lightweight AgentLoop sessions, each
handling a batch of changelog entries) followed by a **compact digest** returned to the
main analysis loop.

## Decision

### Pipeline (Core updates only)

```
update_analysis WorkItem — all stages run sequentially inside one item
  1. build_core_release_report()   [deterministic — ADR S2]
  2. change_screening AgentLoop × N batches   [LLM — this ADR]
  3. main update AgentLoop   [LLM — existing]
```

### Screening sessions

- Each batch is cut at integration boundaries to ≤ `_SCREENING_BATCH_CHARS` (6 000 chars).
- Registry: `read_config`, `query_knowledge`, `finish_change_screening`.
- Prompt: `task_change_screening.md` + `seed_update_screening.md`.
- Sessions write **no** `session_outcomes` / distillation rows — they are mechanics, not
  judgments (the main loop's `finish_update_analysis` owns the verdict).
- Results (`screening_flags`, `screening_highlights`) are accumulated on `ToolExecutor`
  via `_finish_change_screening` / `clear_screening_state` / `get_screening_results`.
- Persisted to the report JSON after all batches finish.

### Compact digest

`_get_update_release_notes` now short-circuits for Core updates when a report JSON
exists: it calls `format_core_release_digest()` and returns at most 16 000 chars
containing installed BCs, general BCs, flagged changes, highlights, and Farewell.
`_release_notes_completeness` comes from the deterministic report's `completeness`
only (amended #1148 — see below).

### `seed_update_core.md` changes

- Step 1 is updated to describe the digest; `fetch_url` instructions for the blog/
  changelog are removed.
- Step 6 (new) requires the main loop to state explicitly whether each installed-
  integration BC affects this instance.

### ADR 018 amendment

`change_screening` AgentLoop sessions are added to the _one-shot / secondary session_
catalogue in ADR 018.  They are not one-shots (they use the full tool-calling loop) but
they are exempt from distillation because they evaluate mechanics, not diagnosis.

## Consequences

- Core update cards will consistently include the full BC list for installed integrations.
- Each Core update analysis runs 1–N additional LLM calls (screening batches); on typical
  hardware this adds seconds to minutes before the main loop starts.
- Completeness degrades gracefully: a failed batch marks the report
  `screening_complete=False` (with `screening_batches_done/total`), which still forces a
  card and caps confidence at 0.5 (see amendment).
- Non-Core updates (add-ons, OS, Supervisor) are unaffected.

## Amendment (#1148) — screening status is separate from notes completeness

Originally a stuck or failed screening batch downgraded `report.completeness` to
`incomplete`. That made the card say "Release notes could not be retrieved" when the
notes were complete and only the all-changes screening was partial (2026.11: 6 of 7
batches). Now:

- `completeness` is the deterministic report status only. The screening pass records
  `screening_complete`, `screening_batches_total` and `screening_batches_done` (both
  optional, so older report JSON still loads).
- `_get_update_release_notes` sets `_release_notes_completeness` from `completeness`
  and stores `ToolExecutor._screening_gap = (done, total)` when screening was partial.
- `_finish_update_analysis` treats a screening gap as unverified (forces a card, caps
  confidence at 0.5, `effective_safe_to_update=false`). It uses the prefix "All-changes
  screening incomplete (N of M batches) — some changelog entries were not reviewed."
  The "Release notes could not be retrieved" prefix is kept for missing notes only.
- When the model passes no `highlights`, the card falls back to the first 5 report
  `screening_highlights` (`"<domain>: <feature>"`). `seed_update_core.md` step 7 asks
  for 2–5 highlights.
