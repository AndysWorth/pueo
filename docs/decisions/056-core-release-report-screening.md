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

- Batches contain **changelog sections only** (no BC entries). BC entries are delivered
  verbatim to the main analysis loop via the digest; re-screening them added 2 of 7
  batches for 2026.11 and triggered false flags.  Amended #1150.
- Each batch is cut at integration boundaries to ≤ `_SCREENING_BATCH_CHARS` (6 000 chars).
- Registry: `finish_change_screening` only (no `read_config`, no `query_knowledge`).
  Those tools caused ~17 LLM calls per batch and enabled instance-fact hallucination.
  Amended #1150.
- `max_tool_calls=3` per batch session. Amended #1150.
- `think=False` per batch session — overrides `OLLAMA_THINK_MODE`/`DEBUG_LEVEL` so high
  debug levels do not cause the model to generate thousands of thinking tokens per batch
  (observed 100 k–230 k chars at `debug_level: 3`, turning a 32-second pass into 84 min).
  Amended #1166.
- Prompt: `task_change_screening.md` + `seed_update_screening.md`.
  Seed carries PR-title vocabulary rules (never flag "Clarify", "Move … classes",
  "Mark … final", "leftovers", "Bump"; `high` only for removal of a documented user-
  facing service or YAML option; never mention specific automations or entities).
  Amended #1150.
- Sessions write **no** `session_outcomes` / distillation rows — they are mechanics, not
  judgments (the main loop's `finish_update_analysis` owns the verdict).
- Results (`screening_flags`, `screening_highlights`) are accumulated on `ToolExecutor`
  via `_finish_change_screening` / `clear_screening_state` / `get_screening_results`.
  Flags are deduped on `(domain, change.lower())` across batches.  Amended #1150.
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

## Amendment (#1158) — BC evidence builder

After screening completes, `build_bc_evidence()` in `utils/ha/update_bc_evidence.py`
runs against all BCs in `bc_installed + bc_general`. It gathers:
- Entity counts by domain from the WS entity registry (capped at 15 examples)
- Multi-channel device detection (devices with >1 same-domain entity ending in a digit)
- Searchable tokens extracted from each BC body (service calls, YAML keys, quoted values,
  card type names, template function names)
- Token matches in lovelace dashboard JSON and automation/script YAML (via SSH grep)

Results are stored as `CoreReleaseReport.bc_evidence` (dict keyed by BC name) and
rendered under each BC in `format_core_release_digest()` as an Evidence block. The LLM
receives grounded entity facts and can return `bc_verdicts` in S4 without needing to
grep for them. All clients may be `None`; each data source degrades gracefully.

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

## Amendment (#1160) — installed components, bc_verdicts, evidence-based card

Three additions across S1–S4 of the Core update analysis v3 plan:

**S1 — Report coverage:**
- `discover_loaded_components()` replaces `discover_installed_integrations()` as the BC
  domain list for `build_core_release_report()`. `_TRIVIAL_DOMAINS` no longer filters BCs.
- Changelog parser walks heading levels: `#####` = integration, `#### Other changes` =
  `core`, `### Frontend` = `frontend` (always "installed"). Section bodies end at any
  heading.
- PR refs retained in `ChangeEntry.prs`; lines whose PR appears in a BC body are skipped
  in screening. `_is_mechanical_change` removes dependency/test/refactor lines.

**S3 — BC evidence builder:**
- `build_bc_evidence(report, ws, rest, ssh)` gathers entity counts, automation/dashboard
  hits, and token matches per BC. Stored in `CoreReleaseReport.bc_evidence`. Rendered in
  the digest under each BC as an `Evidence:` block.

**S4 — bc_verdicts + per-item card:**
- `finish_update_analysis` accepts `bc_verdicts: [{bc, affects, reason}]`. Code normalises
  against report BC names (fuzzy match), fills missing BCs as `unknown`, and derives
  `instance_impact` from verdicts: any `yes` → `high`; only `unknown` → `low`; all `no`
  → `none`.
- `CoreReleaseReport.bc_verdicts` is populated after the analysis and persisted to the
  report JSON so the report page can display the three groups.
- HITL card shows three groups: **Affects this install** / **Check manually** /
  **Not affected** (collapsed). Legacy `breaking_changes` list is kept for backward compat.
- `seed_update_core.md` requires `bc_verdicts`, forbids `read_source` / `get_ha_profile
  config_entries` / broad single-word greps, and limits `check_config_against_breaking_change`
  to cases where the Evidence block is insufficient.

## Amendment (#1173) — version-range-aware report

**Context:** When upgrading across a patch/month boundary (e.g. 2026.10.4 → 2026.11.1),
`build_core_release_report` previously fetched the target patch tag's GitHub body
(a 14 k-char fix list, not a blog stub), so `len(stub) < 500` failed → `blog_url=None` →
0 BCs extracted → `completeness="complete"` despite missing BC data. Patch notes and
multi-month BC coverage were also absent.

**Changes to `build_core_release_report`:**

- Accepts a new `installed_version: str = ""` keyword argument.
- `release_span(installed, target)` → `(months, patches)`: pure helper that computes
  which monthly releases and patch versions to fetch when upgrading between two HA Core
  versions. `months` = monthly releases after the installed month up to and including
  the target month. `patches` = individual patch versions within those months.
- **Multi-month blog fetch:** for each `YYYY.M` in `months`, fetch the `.0` stub → extract
  blog URL → fetch blog as HTML → run `extract_breaking_changes()` with a `release` label.
  `BCEntry` gains a `release: str` field (shown in digest only when multiple months are covered).
- **Patch notes:** `parse_patch_notes(body, installed_set, name_to_domain)` parses flat
  GitHub patch body lines into `ChangeEntry` rows keyed by HA integration domain.
  Stored in `CoreReleaseReport.patch_changes_by_domain`. Included as `### Patch changes: {domain}`
  screening batches for domains not already in `changes_by_domain`.
- **Same-month patch path:** when `installed_version` is set and `months=[]`, no blog or
  changelog is fetched; `changes_by_domain` stays empty.
- **Legacy path** (no `installed_version`): behavior unchanged — fetches the target version
  stub and changelog as before; changelog is now also fetched for this path.
- **Fail-closed completeness:** `CoreReleaseReport.missing` list tracks each
  fetch/parse failure (`"blog:YYYY.M"`, `"bc_section:YYYY.M"`, `"patch:X.Y.Z"`,
  `"changelog:YYYY.M"`). `completeness="complete"` only when `missing` is empty.
- `CoreReleaseReport` gains `installed_version: str`, `blog_urls: dict[str, str]`,
  `patch_changes_by_domain: dict[str, list[ChangeEntry]]`, `missing: list[str]`.
- `format_core_release_digest` emits the BC section when `installed_version` is set, with
  appropriate empty-BC messages: "patch update within X.Y" (same-month), "could not be
  read for: ..." (missing entries), "No backward-incompatible changes found" (complete, 0 BCs).

**Stale report detection in `tool_executor.py`:** invalidates a cached `CoreReleaseReport`
when the report's `installed_version` differs from the pending update's `installed_version`.

**`ha_update_manager.py`:** passes `installed_version=update.installed_version or ""`
to `build_core_release_report`.
