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

## Amendment (#1199) — action-based BC verdicts

**Context:** `affects: yes/no/unknown` could not distinguish "this requires a config change before
updating" from "names changed cosmetically, nothing to do." This caused false `holds` (Tuya
false positive in the 2026.10.4→2026.11.1 test run) because the LLM had no way to signal
urgency or timing.

**Changes (`finish_bc_assessment` schema + downstream):**
- `affects` replaced by `verdict: action_needed | no_action | user_check`. `not_assessed` is
  code-only (filled by `_normalize_bc_verdicts` for missing BCs; never from the LLM).
- New optional fields: `action` (what to do), `when: before_update | after_update`,
  `user_question` (what the user should check manually).
- Impact derivation: `action_needed + before_update` (or no `when`) → `high`; `action_needed +
  after_update` only → `low`; any `user_check/not_assessed` → `low`; all `no_action` → `none`.
- `_derive_recommendation` produces human-readable text from the verdict set.
- Dashboard and report templates show five groups: **Do before updating** / **Do after updating** /
  **Check yourself** / **Could not assess** (collapsed) / **Not affected** (collapsed).
- Backward compat: old reports with `affects: yes/no/unknown` are normalised to
  `action_needed/no_action/user_check` by `_normalize_bc_verdicts` and `_verdict_symbol`.
- `seed_update_bc.md` rewritten as release-independent rules; no 2026.11-specific examples.

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

## Amendment (#1209) — verdict dispatch, reason-first schema, seed-owned rules

The S5 live re-run showed every BC stored as `no_action`. The `finish_bc_assessment`
dispatch in `ToolExecutor._execute_tool` had not been updated for the #1199 schema: it
passed only the old `affects` field (defaulting to `"unknown"`), so the model's `verdict`,
`action`, `when` and `user_question` were dropped and the `user_check`-without-question
guard then turned every BC into `no_action` — including a real `action_needed`.

- The dispatch passes all schema fields. A call with a missing or unrecognised verdict
  returns `success=False` and stores nothing; `_bc_session_outcome` in
  `ha_update_manager.py` then reports `no_verdict`, so the BC is retried once and
  otherwise recorded as `not_assessed`. A BC nobody judged is never reported as handled.
- `finish_bc_assessment` lists `reason` before `verdict`. With `think=False` the model
  commits to whichever field comes first; with the verdict first it wrote reasons that
  contradicted it ("no light entities" → `action_needed`).
- Verdict rules live only in `seed_update_bc.md` (`task_bc_assessment.md` is procedural).
  The seed decides in order: depends on something the evidence cannot see (browser,
  remote access, firmware) → `user_check`; HAOS + Python-environment BC → `no_action`;
  an evidence line proving use → `action_needed`; otherwise `no_action`.
- `_evidence_lines` adds "No entities of type: …" for zero-count types the BC names.
- BC sessions exclude earlier `update_analysis` / `bc_assessment` sessions from the
  "Previous investigations" block (they share the update entity as `subject_key`).

## Amendment (#1219): slim path keeps its notes state and its own prompt

- `AgentLoop.run()` resets the executor, which clears the notes state set by
  `adopt_core_release_report()`. The slim path has no release-notes tool, so the update
  manager calls `preinject_core_release_report()` instead; `reset()` re-adopts that report.
  A digest fetched by tool stays per-loop state.
- The slim path uses `task_update_slim.md`, which never names `get_update_release_notes`.
  `task_update.md` is the tool-fetch path only.
- A session is never told to call a tool it does not have: `AgentLoop` drops the
  SELF-KNOWLEDGE paragraph of `agent_core.md` when `read_source` is not in its registry,
  and the `hallucinated_tool` notice suggests `read_source` only when it is offered.
- Regression test runs the slim path through a real `AgentLoop` with a fake LLM; tests
  that mock `AgentLoop` cannot see reset-related state loss.

## Amendment (#1221): a re-run reuses stored screening results and unchanged verdicts

- `build_core_release_report` overwrites `core_<version>.json`, so every re-run (retry
  after a failure, restart, forced run) repeated all screening and BC sessions, and a
  verdict could flip on identical input. The reuse branch from #1215 read `bc_verdicts`
  from the fresh report, which is always empty, so it never fired.
- The update manager now reads the stored report with `load_stored_core_report()` before
  the build. It is used only when `version` and `installed_version` both match.
- `carry_over_screening()` copies flags, highlights and batch counts when the stored pass
  was complete and `make_screening_batches()` gives the same text for both reports; the
  screening sessions are then skipped.
- `carry_over_bc_verdicts()` keeps a verdict only when everything its session saw is the
  same: BC body, installed/general group, and the evidence lines built in this run.
  `not_assessed` is never kept. Only the remaining BCs get a session.
- Evidence is always rebuilt, so a change on the instance (new entities, a new config
  reference) re-opens exactly the BCs it touches.
- Deleting `update_reports/core_<version>.json` forces a from-scratch run. A stored verdict
  that is wrong but whose evidence has not changed is kept until then.

## Amendment (#1223): `action_needed` must be grounded; slim finish tool asks only for what the model decides

- On identical evidence (0 token hits, no config or dashboard refs) the MQTT BC was judged
  `no_action` in one run and `action_needed` in the next ("28 MQTT entities exist"), against
  the seed's rule that entities existing is never proof. That made a false "fix before
  updating" card, and since #1221 the wrong verdict would also be reused.
- `finish_bc_assessment` now enforces the seed's proof rule (rule 2; the steps were renumbered 0–3, they had skipped 2 since #1216, which is when the verdict flipped). `evidence_proof_lines()` in
  `ha_release_report.py` returns the evidence lines the seed names as proof: "Config refs",
  "Dashboard refs", "Tokens checked (N/M hit)" and "Config keys present". An `action_needed`
  verdict for a BC whose evidence has none of them is rejected and not stored
  (`bc_verdict_ungrounded`). A BC with no evidence object is not checked.
- The check never picks a verdict. The retry session is told why the first answer was
  rejected; if the second answer is also ungrounded the BC is `not_assessed` ("Could not
  assess"), never a blocker and never "not affected".
- Whether a present proof line really shows use (for example a "Config keys present" line
  naming only the component) stays the model's judgment under the seed.
- `FINISH_UPDATE_ANALYSIS_SLIM` drops the fields code derives from the verdicts
  (`safe_to_update`, `breaking_changes`, `instance_impact`, `recommendation`,
  `proposed_config_fixes`, `affected_config_keys`). Asking for them made the model write
  its own versions in a long call that arrived with a corrupted key. On the slim path the
  card's `affected_config_keys` is empty: the main loop judged no BC itself.

## Amendment (#1233): the report scans the full release notes for what Pueo itself depends on

- The first Core 2026.11.0 analysis called the update safe although it removed the
  `hacs/*` WebSocket commands Pueo used. The report kept only the blog's breaking-changes
  and Farewell sections; the news was in blog prose ("a separate HACS installation is no
  longer possible on this version"), so no step ever saw it.
- `build_core_release_report` now keeps the full text of every blog post, the changelog and
  each patch body it fetched, and passes it to `find_pueo_dependency_hits()` with the
  manifest in `utils/ha/pueo_dependencies.py`. The result is stored as
  `CoreReleaseReport.pueo_dependency_hits` (`PueoDependencyHit`: `dependency`, `kind`,
  `source`, `snippet`). No LLM is involved.
- Manifest entries that share a search term (`hacs/repositories/list`, the `hacs`
  integration) are one dependency (`dependency_term_index`). Each non-heading line of the
  text is a candidate passage.
- Noise control: an integration name, a CLI command or a term containing `/` matches
  wherever it appears. Any other term (`repairs`, `lovelace`) matches only on a line that
  also has a change verb (remove, rename, replace, deprecate, drop, no longer, migrate).
  At most 6 passages are kept per dependency, those with a change verb first; a passage is
  cut to 600 characters around the match.
- The scan gathers evidence only; the verdict sessions (Amendment #1235) and the card
  update (Amendment #1237) follow. Release notes can only reveal what they mention.

## Amendment (#1235): pueo_impact verdict sessions

- One one-shot `AgentLoop` session per dependency that has hits, using
  `build_pueo_impact_registry()` with only `finish_pueo_impact`.
- `finish_pueo_impact(verdict, quote, reason)`: `breaks_pueo` | `no_impact` | `dev_check`.
  `breaks_pueo` is rejected unless `quote` is found verbatim in a snippet.
- Verdicts persisted to `CoreReleaseReport.pueo_verdicts` in the report JSON.
- `carry_over_pueo_verdicts` reuses unchanged verdicts across re-runs (excludes `dev_check`).
- Seed `seed_update_pueo_impact.md` (signature `update:core_pueo:*`).

## Amendment (#1237): card and safety verdict from pueo_impact verdicts

- `breaks_pueo` sets `safe_to_update=False` in `_finish_update_analysis`, overriding the BC
  verdict and blocking the level-4 auto-apply path.
- `pueo_command_risks` on the update card is now derived from `pueo_verdicts` (entries with
  `breaks_pueo` or `dev_check`) instead of being model-supplied.
- `pueo_command_risks` added to `_SLIM_CODE_DERIVED_FIELDS`; slim path description updated.
- `task_update_slim.md` and `seed_update_core.md` step 2 (get_pueo_command_catalog) removed
  from Core path; non-Core keeps the catalog tool and existing field.
- Dashboard (`index.html`): "Pueo needs a change first" group (breaks_pueo) above BC groups;
  "Check yourself (Pueo)" group (dev_check) after user_check; each breaks_pueo item links
  to a pre-filled GitHub new-issue URL. Old pueo_command_risks flat list removed.
- Non-Core updates: release_summary scanned with `find_pueo_dependency_hits` and any hits
  are appended to `initial_context` (keep catalog tool + existing field for those paths).

## Amendment (#1249): split-on-failure + per-batch reuse

**Problem:** a single Ollama 500 / garbled tool call during one screening batch forces a
full re-run (the previous `carry_over_screening` check was all-or-nothing), and the retry
was byte-identical so it failed for the same reason.

**Changes:**
- `CoreReleaseReport` gains `screening_batch_results: dict[str, dict]` (keyed by sha256 of
  batch text; value `{flags, highlights}`) and `screening_unscreened_domains: list[str]`.
- `batch_hash(text)` and `batch_domains(text)` helpers in `ha_release_report.py`.
- `carry_over_screening` now operates per-batch: each batch in the new report is looked up by
  hash in `prev.screening_batch_results`; matching batches are reused individually.  Returns
  `True` only when every batch is covered (whole pass reused).  Old reports with
  `screening_complete=True` but no `screening_batch_results` fall back to the old all-or-nothing
  behavior for backward compat.
- Screening loop in `ha_update_manager.py`: on failure after 2 attempts, the batch is split at
  `### Changes:` / `### Patch changes:` boundaries (at most once) and each half is screened
  independently.  On success, the delta flags/highlights are stored in
  `_rpt.screening_batch_results` keyed by the sub-batch hash.  Domains from permanently failed
  halves are added to `_rpt.screening_unscreened_domains`.
- `_screening_gap_prefix` in `tool_executor.py` accepts an optional `unscreened_domains` list;
  when non-empty it names the domains ("Not screened: auth, logbook.") instead of the opaque
  "some changelog entries".
- `_split_batch_at_sections(text)` in `ha_update_manager.py` (module-level helper).

## Amendment (#1253): developer flag classification + custom-component scan

**Problem:** screening produces many developer-facing Python API change flags (removed
arguments, renamed constants) that are only relevant if a custom integration calls that
API.  These appeared as noise on the card (`flag_verdicts: []`).

**Changes:**
- `ScreeningFlag` gains `audience: str = "user"` and `symbol: Optional[str] = None`.
  `"developer"` audience flags carry a `symbol` (the Python identifier being removed).
- `finish_change_screening` tool schema updated to include `audience` (required) and
  `symbol` (optional) on each flagged item.  `task_change_screening.md` prompt updated
  with audience classification guidance.
- `scan_custom_components(symbols, ssh_client)` in `utils/ha/update_bc_evidence.py`:
  runs `grep -rnF <symbol> /config/custom_components` per symbol, restricted to
  plain Python identifiers.  Called at the end of `build_bc_evidence()` for all
  developer flags with a symbol.  Populates `CoreReleaseReport.flag_verdicts` with
  `user_check` (hit) or `no_action` (miss) entries, naming the matching integration.
- `_READ_ONLY_COMMAND_PATTERNS` in `read_only_ssh.py` gains an anchored pattern for
  the grep command (Python identifier regex prevents shell injection).
- Card payload: `flag_verdicts` now populated from the scan results (was always `[]`).
  `screening_flags` on the card suppresses developer flags whose verdict is `no_action`
  (custom-component scan found no caller); developer flags with a hit are still shown.
  User flags are unaffected.
- New helper `_is_dev_flag_no_action(flag, flag_verdicts)` in `tool_executor.py`.

## Amendment — S4: BC evidence completeness (#1255, 2026-11-17)

Addresses three evidence gaps that caused answerable BCs to go to `user_check`:

**Auth/HTTP URL classification:** `build_bc_evidence()` now calls
`rest_client.get_raw("/api/config")` and passes the result to `_build_one_evidence`.
For BCs whose domains include `auth`, `http`, or `frontend`, it classifies
`external_url`/`internal_url` via `_classify_url()` → `BCEvidence.url_classification`
(`{"external_url": {"url": "...", "type": "domain", "host": "..."} }`).  Types: `domain`,
`lan_ip`, `public_ip`, `tailscale_ip` (100.64.0.0/10), `global_ipv6`.  When
`url_classification` is non-empty, `_outside_evidence()` suppresses the "how HA is
reached from outside" entry (we now know the answer).  `_evidence_lines()` renders
each URL as "External Url: domain (neumopho.duckdns.org)".  `seed_update_bc.md` rule
1 exception: `domain` or `lan_ip` → `no_action` immediately; only `public_ip`,
`tailscale_ip`, or `global_ipv6` are affected by that BC.

**Frontend mobile clients:** For BCs with domain `frontend` or `mobile_app`,
`_build_mobile_clients(entity_registry, device_registry)` lists devices linked via
`platform=mobile_app`, stored in `BCEvidence.mobile_clients`.  `_evidence_lines()`
renders "Mobile app clients: …".  `seed_update_bc.md` instructs: use this list
instead of asking the user.

**Multi-part BC (Matter-class): sensor device_class counts.** When any BC body
mentions sensor device_class keywords (TVOC, carbon monoxide, ozone, etc.),
`build_bc_evidence` calls `rest_client.get_states("sensor.")` once and passes the
results to `_build_one_evidence`.  `_device_classes_from_text()` extracts the
device_class values the BC names; sensor state counts (with zero-fill) are stored
in `BCEvidence.sensor_device_class_counts`.  `_evidence_lines()` renders
"Sensor device classes: carbon_monoxide 0, ozone 2 …" and
"No sensors of class: carbon_monoxide".

**Multi-part BC reason coverage:** BCs with repeated domain entries (e.g. Matter ×3)
have their body split by PR-reference markers (`_split_bc_parts()`) and distinctive
tokens extracted per part (`_part_key_tokens()`), stored in
`BCEvidence.bc_part_tokens`.  `finish_bc_assessment` rejects a reason that cites
none of a part's tokens (same `_bc_rejection_note` path as the proof-line check,
#1223), giving the model one retry.  `seed_update_bc.md` documents the multi-part
rule.

## Amendment — S5: OS applicability (#1257, 2026-11-17)

Addresses finding F4: OS 18.5 is an RPi/Yellow-only kernel CVE fix but the card said
`instance_impact: none` and asked the user whether this is a Pi, despite `ha host info`
already showing `cpe …:yellow` and `kernel 6.18.39-haos-raspi`.

**Changes:**

`utils/ha/os_applicability.py` (new):
- `fetch_os_board_context(ssh)` → `{board, kernel, raw_cpe}` via `ha os info --raw-json`
  (board field) + `ha host info --raw-json` (kernel field); best-effort, None on failure.
- `os_release_applicability(notes, board)` pure function: parses `##`/`###` headings for
  board-specific sections using `_BOARD_PATTERNS` alias table (yellow, green, rpi3/4/5,
  odroid-n2/m1, generic-x86-64). Returns `{applies: yes|no|unknown, reason, board_sections,
  other_board_sections}`. "No board sections" → `yes` (all boards). "Other board only" → `no`.

`agents/ha_update_manager.py`:
- For OS updates: calls `fetch_os_board_context(_ssh)` after `sanitize_update_context`,
  injects "Board: yellow · kernel 6.18.39-haos-raspi" into `initial_context`, and calls
  `executor.set_os_board_context(ctx)`.

`utils/agent/tool_executor.py`:
- `ToolExecutor._os_board_ctx`: stores board context dict.
- `set_os_board_context(ctx)`: setter.
- `_derive_os_applicability()`: called by `_finish_update_analysis`; uses `_session_outputs`
  to find `get_update_release_notes` / `fetch_url` text, runs `os_release_applicability`, adds
  `board` and `kernel` keys; returns `{}` when no board context.
- `_finish_update_analysis` payload gains `os_applicability` field.

`prompts/seed_update_os.md`:
- Added: "do not call `ha host info` to re-derive facts already in context";
  board applicability rules (other-board-only → `instance_impact=none`);
  `verification_question` about facts already in context is an error.
- Removed: firmware step (step 3, `ha os boards rpi firmware`); board-confirmation step.

## Amendment — S6: Heads-up tier (#1259, 2026-11-17)

`CoreReleaseReport.heads_up_items` (new field): list of `{heading, note, source, matched_domain}` dicts populated by `match_blog_sections_to_installed()` during report build.  The function scans `##`/`###` headings in blog texts (source="blog:YYYY.M"), skips non-feature sections (Breaking Changes, Farewell, Integrations, etc.), and matches each heading against the installed domain set using the existing `_resolve_domain` and `_find_domain_in_heading` helpers.  HACS/Marketplace special case: any heading containing "marketplace", "community store", or "hacs" matches when "hacs" is installed.  One item per domain (first heading wins).

`finish_bc_assessment` gains a `heads_up` verdict: visible-but-harmless change the user will notice (dashboard labels, automatic migration).  It is accepted without a proof-line check and without `user_question`.  `_derive_instance_impact` and `_derive_recommendation` treat it the same as `no_action`.

`_finish_update_analysis`: `heads_up_items` from the report is included in the card payload.  `verification_question` is suppressed when BC assessment verdicts exist — each `user_check` BC has its own `user_question`, making a top-level digest question redundant filler.

`index.html`: collapsed "👁 What you'll notice" group renders `heads_up` BC verdicts and `heads_up_items` together.

## Amendment — S3: Patch-fixes list on card (#1306, 2026-11-24)

`_finish_update_analysis` now flattens `CoreReleaseReport.patch_changes_by_domain` into `payload["patch_changes"]` — a list of `{domain, text, prs}` dicts, `core` domain first, remaining domains alphabetical, capped at 40 entries. The card payload always includes this key (empty list when no report or no patch changes).

`index.html`: collapsed "🔧 Fixes for your integrations (N)" `<details>` group, rendered outside the BC verdicts block so it appears on patch updates with no breaking changes. Each domain is a subheading; PR numbers link to `github.com/home-assistant/core/pull/<n>`.

`web/templates/update_report.html`: "🔧 Patch Fixes for Your Integrations" section renders `patch_changes_by_domain` from the `CoreReleaseReport` directly (not from the card payload), with `core` first and PR links.

No LLM involvement; the list is deterministic from the parsed patch notes.

## Amendment — S4: "May relate" cross-reference (#1308, 2026-11-24)

`CoreReleaseReport.related_instance_events` (new field, backward-compatible default `{}`): a dict keyed by integration domain, each value a list of `{ts, summary, source}` dicts (newest-first, capped at 3).  Populated after `build_bc_evidence` by `recent_instance_events_for_domains(conn, domains)` in `utils/ha/update_bc_evidence.py`.

Sources — all use **exact domain match** (never substring):
- `session_outcomes`: signatures matching `device:offline:<domain>`, `repair:<domain>:…`, `log:homeassistant_components_<domain>:…`, `lovelace:<domain>:…`, `setup:integration:<domain>` within the last 30 days.
- `ha_repair_history`: unresolved `issue_key` values starting with `<domain>/` within 30 days.
- `timeline_events`: WARN/ERROR rows with `detail_json.domain == domain` within 30 days.

`_finish_update_analysis` adds `payload["related_instance_events"]` (empty dict when no events).  No LLM, no effect on verdicts or `instance_impact`.

`index.html`: inside each domain's fix list, one italic "May relate: `<summary>` (`<date>`)" line per event.  Uses `localtime('date')` per the local-time rule.

Amends: ADR 046 (card payload extended), ADR 049 (procedure only: no distillation path).

## Amendment — S5: Stable patch link + boilerplate RAG filter (#1310, 2026-11-24)

**Stable release link (F4):** `_finish_update_analysis` now derives `payload["blog_url"]` as:
- If `_core_report_blog_url` is set (minor/major Core update): use that blog URL as before.
- If not set and component is `core`/`homeassistant` (patch update — no blog post exists): use `https://github.com/home-assistant/core/releases/tag/<latest_version>`.
- For non-Core components: fall back to `update.release_url` as before.
This prevents the card from ever storing the drifting `/latest-release-notes/` pointer.

**Boilerplate RAG filter (F6):** `chunk_release_notes` in `utils/knowledge/ha_release_notes_scraper.py` now drops any section whose text matches `_BOILERPLATE_RE` (`"Full changelog for Home Assistant Core"`).  The `## All changes` footer that appears at the bottom of every monthly blog post ("Of course, there is a lot more … Full changelog for Home Assistant Core 2026.NN .") carries zero information and was previously injected as `[OFFICIAL]` knowledge before the main update loop.  Filtering at ingest time removes existing stale chunks on the next RAG refresh (the ingest key is index-based so re-chunking a file that now has fewer sections is handled transparently by the pruning pass).
