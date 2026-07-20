# Pueo Project Audit — 2026-07-20

Read-only consistency audit across documentation, config, and code. Covers sections 1–10 of `docs/plan/audit.md`.

---

## Summary

The codebase is functionally solid — all 20 implementation plan items are implemented, all CI gates pass, and the core architecture invariants (backup-before-write, structured LLM output, deferred imports, asyncio.to_thread wrapping) are correctly enforced. The top three issues before the next implementation session:

1. **Critical DB schema divergence:** `_MIGRATIONS` in `ha_agent_sandbox_engine.py` stops at v2; `ha_agent_advanced.py` is at v4. A fresh database created by `repair` mode is missing the `netalertx_install_state` and `netalertx_state` tables.
2. **Critical schema drift:** `DiagnosticsReport` is independently defined in three files with divergent `recommended_fix_yaml` field descriptions, causing different JSON schemas to be emitted to Ollama depending on which agent runs.
3. **setup.sh ADR 001 violation:** 22 config keys (8 agent-section, 14 netalertx) are absent from the `setup.sh` heredoc with no documented justification, violating the triple-file rule.

---

## Findings

---

### Section 1 — Config Triple-File Rule (ADR 001)

**config.py ↔ config.yaml.default parity**
✅ Pass — All 34 exported constants have corresponding YAML keys in `config.yaml.default`. No keys missing in either direction.

**config.py ↔ setup.sh parity**
❌ Fail — 8 `agent`-section keys absent from `setup.sh` with no documented justification: `ssh_retry_attempts`, `ssh_retry_base_delay`, `debounce_window_seconds`, `repair_cooldown_seconds`, `max_repairs_per_hour`, `log_level`, `log_file`, `max_prompt_tokens`.
Recommended fix: Add these to the `setup.sh` heredoc as commented defaults (e.g. `# ssh_retry_attempts: 3  # advanced tuning`) or add an explicit "edit config.yaml directly" comment block naming all eight.

❌ Fail — 14 `netalertx`-section keys absent from `setup.sh`: `deployment`, `host`, `api_port`, `ssh_host`, `ssh_user`, `ssh_key_path`, `addon_repository_url`, `addon_slug`, `scan_interface`, `auto_generated_name_patterns`, `max_scan_age_minutes`, `mqtt_subscribe`, `log_container_name`, `max_db_history_rows`. Only `enabled` and `api_token` are written. `netalertx.md` item 10 done-criteria included "all 14 keys in setup.sh" — not met.
Recommended fix: Add a conditional block to `setup.sh` that writes all 14 keys (as commented defaults, or as prompted values when `NAX_ENABLED=true`).

⚠️ Warning — `OLLAMA_ENDPOINT` is hardcoded to `http://localhost:11434` in `setup.sh` without an interactive prompt. Defensible as a machine-only default but undocumented.

**Future config keys in docs/plan not yet in config.py**
✅ Pass — `agent.hitl_timeout_minutes` (mentioned in `autonomy.md`) is correctly absent from `config.py`; it was proposed and then deleted in item 19.5. See Section 10 for the stale documentation issue.

---

### Section 2 — Implementation Plan Accuracy

**All ✅ Done items verified**
✅ Pass — All 20 items have clear code artifacts: `utils/retry.py` (item 2), `utils/rate_limiter.py` (item 3), `utils/logging.py` (items 5, 20), `utils/context.py` (item 6), `utils/yaml_validator.py` (item 7), `interfaces.py` + `utils/ssh_client.py` + `utils/ollama_client.py` (item 8), `utils/notify.py` (item 9), `utils/autonomy.py` (item 9.5), full `netalertx/` package (items 10–19), `web/dashboard.py` (item 19.5).

**Phase header labels out of sync**
❌ Fail — `docs/implementation-plan.md` Phase 3.5 header reads `✅ TODO`; item 9.5 in the Status table directly above is `✅ Done (2026-07-19)`.
❌ Fail — Phase 4 header reads `✅ TODO`; all 10 items (10–19) are `✅ Done`.
Recommended fix: Change both phase headers to `✅ Complete` with the last-item date.

**Cross-check with detail files**
✅ Pass — All detail files for items 1–20 are marked Done and include PR numbers (except `foundation.md` — see Section 10).

---

### Section 3 — Roadmap ↔ Plan Alignment

**Milestone table completeness**
❌ Fail — `docs/roadmap.md` milestone table shows only 5 rows (Read-only diagnostics, Local RAG, Safe execution, Closed-loop healing, Evals). Phases 3.5 (Autonomy), 4 (NetAlertX), 4.5 (HITL UX), and 5 (Status Logging) — representing all 20 completed implementation plan items — have no rows in the table.
Recommended fix: Add milestone rows for Phases 3.5, 4, 4.5, and 5, or add a note clarifying that roadmap milestones are strategic objectives and implementation-plan phases are the tactical backlog.

**Milestones with no plan detail file**
⚠️ Warning — Milestone 2 (Local RAG & Knowledge Layer) is fully described in `roadmap.md` but has no `docs/plan/rag.md` detail file. Acceptable for "Not started" but inconsistent with the convention established for implemented phases.
⚠️ Warning — Milestones 3 and 4 (both ✅ Complete) have no detail files — predates the detail-file convention.

**Evaluation matrix vs ADRs**
✅ Pass — All four evaluation matrix constraints (latency, hallucination, un-backed writes, WAN packets) align with ADRs 002 and 003.
⚠️ Warning — The "un-backed writes 0%" row does not mention that autonomy level 1 also prevents all writes (added in item 9.5). Minor documentation gap.

---

### Section 4 — CLAUDE.md Architecture Patterns in Code

**Safety invariant (ADR 002) — backup slug + finally block**
✅ Pass — `ha_agent_sandbox_engine.py` correctly wraps sandbox swap in `try/finally` that unconditionally reverts. `main()` calls `execute_remote_backup()` → `record_backup_slug()` before any write path. No bypass exists.

**No re-declared constants**
❌ Fail — `_SSH_RETRY` (a derived dict built from `SSH_RETRY_ATTEMPTS` and `SSH_RETRY_BASE_DELAY`) is independently assembled in three files: `ha_agent_core.py` (line 29), `ha_agent_advanced.py` (line 37), `ha_agent_sandbox_engine.py` (line 45). All three are content-identical. No ADR or comment documents this as intentional.
Recommended fix: Export `_SSH_RETRY` (or a `default_ssh_retry_kwargs()` helper) once from `utils/retry.py` and import it in all three agent files.

**Structured LLM output (ADR 003)**
✅ Pass — All `ollama.chat` calls route through `utils/ollama_client.py` `OllamaClient.chat()`, which passes `format=<Model>.model_json_schema()` and `options={"temperature": 0.0}` at every call site. Fully centralized.

**asyncio.to_thread wrapping**
✅ Pass — `utils/ollama_client.py` wraps the synchronous `ollama.Client.chat()` in `asyncio.to_thread()`. All agents call through this wrapper; no direct synchronous Ollama call exists.

**Sandbox path derivation**
✅ Pass — `ha_agent_sandbox_engine.py` lines 52–55 correctly derive `SANDBOX_REMOTE_DIR` and `SANDBOX_REMOTE_FILE` from `CONFIG_REMOTE_PATH`. Not independently hardcoded.

**Deferred agent imports in main.py**
✅ Pass — `main.py` top-level imports are only stdlib (`argparse`, `asyncio`, `os`, `sys`, `pathlib`). All four agent modules are imported inside the `if args.mode` blocks.

**SSH context isolation**
✅ Pass — `utils/ssh_client.py` opens a fresh `asyncssh.connect()` context per method call. No persistent connection stored as an instance attribute.

---

### Section 5 — Code Duplication

**DiagnosticsReport defined three times — with schema drift**
❌ Fail (Critical) — `DiagnosticsReport` is independently defined in `ha_agent_core.py` (line 39), `ha_agent_advanced.py` (line 47), and `ha_agent_sandbox_engine.py` (line 61). The `recommended_fix_yaml` field description diverges: sandbox engine reads "The complete, fully corrected replacement string for configuration.yaml" while the other two read "Corrected YAML block snippet if applicable." Different descriptions → different `model_json_schema()` output → different LLM behavior depending on which agent runs.
Recommended fix: Define once in `ha_agent_core.py` (or `utils/schemas.py`) and import in the other two files.

**_SSH_RETRY defined three times**
❌ Fail — Same content-identical dict in `ha_agent_core.py:29`, `ha_agent_advanced.py:37`, `ha_agent_sandbox_engine.py:45`. Undocumented. See Section 4 for fix.

**_MIGRATIONS lists diverge — critical DB schema bug**
❌ Fail (Critical) — `ha_agent_advanced.py` defines `_MIGRATIONS` with 4 entries (v1–v4); `ha_agent_sandbox_engine.py` defines it with only 2 entries (v1–v2). The sandbox engine is missing `_migrate_v3` and `_migrate_v4` which create the `netalertx_install_state` and `netalertx_state` tables. When `--mode repair` runs against a fresh database, those tables are absent. Neither duplication nor divergence is documented.
Recommended fix: Consolidate both `_MIGRATIONS` lists and all `_migrate_vN` functions into a single shared module (e.g. `utils/db.py`) and import from there. At minimum, add v3 and v4 migrations to `ha_agent_sandbox_engine.py` immediately to restore schema parity.

**None documented as intentional**
❌ Fail — None of the three duplications have any comment, ADR, or CLAUDE.md note documenting them as intentional.

---

### Section 6 — Dead or Unused Config Keys

**OLLAMA_ENDPOINT**
⚠️ Warning — Defined in `config.py`, imported by `utils/ollama_client.py`, actively used. But zero tests cover it in `TestConfigDefaults` — it is the only exported constant with no test.
Recommended fix: Add `test_ollama_endpoint_default` to `TestConfigDefaults`.

**LOG_REMOTE_PATH**
⚠️ Warning — Defined in `config.py`, tested in `test_core.py` (line 83), but imported by no agent file. CLAUDE.md Layer 4 explains why (modern HA uses `ha core logs --follow` instead of a log file), but `LOG_REMOTE_PATH` is not marked deprecated or reserved in `config.py` or any ADR.
Recommended fix: Add a `# Legacy: not currently used — HA logs now consumed via ha core logs --follow` comment in `config.py`, or remove the constant and its test.

**HA_API_TOKEN**
⚠️ Warning — Exported from `config.py`, tested in `TestConfigDefaults`, but imported by no primary agent file. It is consumed by `netalertx/` submodules (installer, ha_name_sync, health) and is not truly dead — but its only callers are in the netalertx package, not the four main agents. No "reserved for REST API" comment exists.
Recommended fix: Add a comment in `config.py` noting it is used by `netalertx/` submodules and HA REST API calls.

---

### Section 7 — Test Coverage Completeness

**Pydantic schemas — 3-test rule**
❌ Fail — `DiagnosticsReport` from `ha_agent_advanced.py` has no dedicated schema test class. `TestDiagnosticsReport` (line 134) tests only the `ha_agent_core` version. There is no `TestAdvancedDiagnosticsReport` covering valid construction, missing-field validation, and JSON round-trip for `ha_agent_advanced.DiagnosticsReport`. (This gap disappears if Section 5.1 consolidation is implemented.)
✅ Pass — `DiagnosticsReport` (core and sandbox), `LogEvaluation`, and all netalertx schemas (`ConfigIssue`, `HealthReport`, `DevicePresenceEvent`, `NetAlertXDiagnostic`, `SyncReport`, `HITLRequest`) each have three-test coverage based on class names in `test_core.py`.

**Config keys in TestConfigDefaults**
❌ Fail — `OLLAMA_ENDPOINT` has zero test coverage anywhere in `test_core.py`. Every other exported constant has at least one `TestConfigDefaults` test method.
Recommended fix: Add `test_ollama_endpoint_default` asserting `config.OLLAMA_ENDPOINT == "http://localhost:11434"`.

**Pure-logic functions**
✅ Pass — All identified pure-logic functions have tests: `estimate_tokens` → `TestEstimateTokens`; `truncate_to_budget` → `TestTruncateToBudget`; `sliding_window_lines` → `TestSlidingWindowLines`; `CRITICAL_LOG_PATTERN` → `TestLogMonitor`; `_extract_backup_slug` → `TestBackupSlugExtraction`; `validate_proposed_fix` → `TestValidateProposedFix`; `requires_hitl` → `TestRequiresHitl`; sandbox path derivation → `TestSandboxEngine`.

---

### Section 8 — CI Gate Consistency

**bandit command mismatch**
⚠️ Warning — `CLAUDE.md` line 144 documents `bandit -r . -x ./tests`; `.github/workflows/test.yml` line 57 runs `bandit -r . -x ./tests,./.venv`. Local runs may report false positives from the virtual environment.
Recommended fix: Update CLAUDE.md to `bandit -r . -x ./tests,./.venv`.

**All five gates present**
✅ Pass — `black`, `flake8 --select=E9,F63,F7,F82`, `mypy --ignore-missing-imports`, `bandit`, and `pytest --cov` are present in both CLAUDE.md and `test.yml`.

**Coverage floor undocumented**
❌ Fail — CI runs `pytest --cov=./ --cov-report=xml --cov-fail-under=90` (90% floor). CLAUDE.md documents only `pytest --cov` with no mention of the threshold. Developers running the local command from CLAUDE.md will not know their PR must meet 90%.
Recommended fix: Update CLAUDE.md's CI commands to `pytest --cov --cov-fail-under=90`.

**Python versions**
✅ Pass — CI matrix tests 3.12, 3.13, 3.14; CLAUDE.md lists the same three.
⚠️ Warning — CLAUDE.md says CI runs against "`main`/`develop`"; `test.yml` triggers only on `main`. No `develop` branch or trigger exists. Stale reference.
Recommended fix: Remove "`/develop`" from the CLAUDE.md CI description.

---

### Section 9 — Decision Record Completeness

**Patterns documented in CLAUDE.md without ADRs**
⚠️ Warning — SSH `known_hosts=None` decision ("flag in any security review") is documented in CLAUDE.md but has no ADR. A `known_hosts=None` bypass is security-sensitive and warrants recorded rationale.
⚠️ Warning — Deferred import pattern ("Agent imports inside `main.py` must stay deferred") has no ADR. The constraint is subtle and will surprise contributors; ADR 001's Consequences section mentions it but doesn't fully explain it.
⚠️ Warning — Sandbox path derivation invariant (`SANDBOX_REMOTE_DIR` derived from `CONFIG_REMOTE_PATH`) has no ADR. Minor but is an enforced correctness invariant.

**Patterns in code with no CLAUDE.md Key Patterns entry and no ADR**
❌ Fail — **Rate limiter / debouncer** (`utils/rate_limiter.py`: `Debouncer`, `RateLimiter`) governs repair frequency. Not mentioned in Key Patterns, no ADR.
❌ Fail — **Token budget management** (`utils/context.py`: `estimate_tokens`, `truncate_to_budget`) enforces the 8,000-token evaluation matrix constraint. Not in Key Patterns, no ADR.
❌ Fail — **Autonomy gate pattern** (`utils/autonomy.py`: `AutonomyGate`) is the central HITL decision point imported by every module. Not in Key Patterns. CLAUDE.md Layer 3 description (line ~63) still references the deleted `requires_hitl()` function; should read `AutonomyGate.require_approval()`.
⚠️ Warning — **Dependency injection via Protocol interfaces** (`interfaces.py`) mentioned in Phase 1–3 description but has no Key Patterns entry and no ADR.
⚠️ Warning — **Plain-text console formatter** (`_TextFormatter`, item 20) — not mentioned anywhere in CLAUDE.md.

**ADR cross-references**
✅ Pass — ADR 001 cross-references ADR 002 in its Consequences section.
❌ Fail — ADR 002 has no cross-references to ADR 001 (config governs backup path) or ADR 003 (structured output triggers the backup chain).
❌ Fail — ADR 003 has no cross-references to ADR 001 or ADR 002.
Recommended fix: Add a "Related decisions" section to ADR 002 and ADR 003.

**asyncio over LangGraph/CrewAI**
⚠️ Warning — Consequential architectural decision currently buried in `docs/roadmap.md` as a footnote. Should be promoted to a dedicated ADR (ADR 004 or 005) before the roadmap is restructured.

---

### Section 10 — Plan Detail File Internal Consistency

**foundation.md (items 1–9)**
✅ Pass — Items clearly identified; config keys declared per item; "Done when:" criteria present for all 9.
⚠️ Warning — No explicit "Depends on:" per item (unlike netalertx.md, autonomy.md).
❌ Fail — No PR/commit references for any of items 1–9, unlike every other detail file.
Recommended fix: Add PR numbers from git log (items 1–9 were merged 2026-07-15 as part of early PRs).

**autonomy.md (item 9.5)**
✅ Pass — Item identified; dependencies declared; config keys listed; completion criteria comprehensive; PR #17 present.
❌ Fail — Line 70 lists `agent.hitl_timeout_minutes` as a "Config key to add." This key was proposed and then deleted in item 19.5. It does not exist in `config.py`.
Recommended fix: Annotate the line: `~~agent.hitl_timeout_minutes~~  *(Deleted in item 19.5 — see hitl-dashboard.md)*`

**netalertx.md (items 10–19)**
✅ Pass — All 10 items identified; per-item dependencies declared; config keys and SQL schema changes documented; "Done when:" criteria; PR references (PRs #18–#31); version-pinned references marked.
❌ Fail — Item 10 dependency line references `hitl_timeout_minutes config key` — stale (key never existed in final code).
Recommended fix: Remove `hitl_timeout_minutes config key` from the item 10 dependency statement.
❌ Fail — Step 7 description (line ~150) references `agent.hitl_timeout_minutes` for polling timeout. Key does not exist; polling is now indefinite.
Recommended fix: Replace with "polls indefinitely until user approves or rejects via the HITL dashboard."
❌ Fail — No cross-reference to `autonomy.md` for the `netalertx.mode` deprecation. The shim in `config.py` (lines 84–94) maps `netalertx.mode` → `agent.autonomy_level` but neither the item 10 preamble nor any netalertx.md section mentions it.
Recommended fix: Add a note in item 10's preamble: "`netalertx.mode` is deprecated and mapped to `agent.autonomy_level` via a shim in `config.py` — see `autonomy.md` for the migration rationale."

**evals.md (Milestone 5)**
❌ Fail — No implementation-plan item number. Other detail files have "Part of the [Implementation Plan](../implementation-plan.md)" header lines; evals.md has none.
❌ Fail — No "Depends on:" section. Evals depend on items 1 (prompts must exist), 6 (token management), 7 (YAML validator).
⚠️ Warning — No Ollama model version note ("current at time of writing").
✅ Pass — "Done when:" criteria and task list are comprehensive.
Recommended fix: Either assign item 21 in `implementation-plan.md` with a "Milestone 5" cross-reference, or add a header note clarifying it is a roadmap milestone not a numbered plan item; add "Depends on:" and a model version note.

**hitl-dashboard.md (item 19.5)**
✅ Pass — Item identified; config key (`DASHBOARD_PORT`) documented; completion criteria comprehensive; PR #33 present; deletion of `HITL_TIMEOUT_MINUTES` correctly noted.
⚠️ Warning — No "Depends on:" section.
⚠️ Warning — No SQL schema changes section header (item doesn't add schema, but the omission is structurally inconsistent).

**status-logging.md (item 20)**
✅ Pass — Problem statement precise; code changes specified per file; test cases listed; verification section present; PR #36 present.
❌ Fail — No "Part of the Implementation Plan" header link.
❌ Fail — No "Depends on:" section. Item 20 depends on item 5 (structured logging infrastructure) and items 11–12 (the installer whose output it makes visible).
Recommended fix: Add standard header link and "Depends on: items 5, 11–12" section.

---

## Quick-Fix List

Estimated < 30 minutes each, ordered by impact:

- **Fix `_MIGRATIONS` in `ha_agent_sandbox_engine.py`** — add `_migrate_v3` and `_migrate_v4` (copy from `ha_agent_advanced.py`) to restore schema parity. Prevents missing-table errors in `repair` mode. (Critical)
- **Fix Phase 3.5 and Phase 4 headers** in `docs/implementation-plan.md` — change `✅ TODO` → `✅ Complete` with dates.
- **Add `OLLAMA_ENDPOINT` test** — one method in `TestConfigDefaults` asserting the default value `"http://localhost:11434"`.
- **Update CLAUDE.md CI commands** — `bandit -r . -x ./tests,./.venv` and `pytest --cov --cov-fail-under=90`; remove stale `/develop` branch reference.
- **Annotate `hitl_timeout_minutes` in `autonomy.md`** — strike-through with "Deleted in item 19.5" note.
- **Fix `netalertx.md`** — remove `hitl_timeout_minutes` from item 10 dependency line; fix Step 7 polling description; add `netalertx.mode` cross-reference to `autonomy.md`.
- **Add `status-logging.md` header link + "Depends on:" section** — two-line addition.
- **Add "Related decisions" to ADR 002 and ADR 003** — cross-reference each other and ADR 001.
- **Update CLAUDE.md Layer 3 description** — replace `requires_hitl()` with `AutonomyGate.require_approval()`.
- **Add `LOG_REMOTE_PATH` deprecation comment** in `config.py` — one comment line.

---

## Technical Debt Register

Larger issues (> 30 min) requiring design decisions:

- **Consolidate `DiagnosticsReport`** — Three independent definitions with divergent field descriptions affect the JSON schema sent to Ollama. Correct fix: single definition in `ha_agent_core.py` or a new `utils/schemas.py` imported by `ha_agent_advanced` and `ha_agent_sandbox_engine`. Risk: breaking test isolation for schema-specific tests. Belongs in a new plan item or ad-hoc PR; update ADR 003 with a note about schema consolidation.

- **Consolidate `_SSH_RETRY` and `_MIGRATIONS`** — Both are duplicated across agent files. A shared `utils/db.py` (for `_MIGRATIONS`) and updated `utils/retry.py` (for `_SSH_RETRY`) would eliminate the duplication. `_MIGRATIONS` consolidation is higher priority due to the v2/v4 parity bug. Needs a new plan item; document the decision in CLAUDE.md Key Patterns.

- **setup.sh ADR 001 gap** — 22 keys (8 agent, 14 netalertx) are absent from the `setup.sh` heredoc. Full fix requires deciding whether to add interactive prompts, commented defaults, or an "advanced config" documentation block. This is a meaningful UX decision for the setup wizard. Candidate for a short plan item; update ADR 001 Consequences to document the "machine-only defaults" exception policy.

- **Roadmap milestone table** — Phases 3.5, 4, 4.5, and 5 have no rows. Updating the table is straightforward writing, but it also warrants a decision about whether the roadmap should track implementation plan phases or only strategic milestones. Low code risk but documentation architecture decision. Update `docs/roadmap.md` directly; no ADR needed.

- **Missing ADRs** — At least four decisions lack records: (a) SSH `known_hosts=None` — security-sensitive, warrants ADR 004; (b) asyncio over LangGraph/CrewAI — strategic architectural choice, warrants ADR 005; (c) rate limiter/debouncer behavioral contract; (d) autonomy gate as the single HITL decision point. These could be batched into a "write missing ADRs" session. Add Key Patterns entries to CLAUDE.md for each once their ADRs exist.

- **CLAUDE.md Key Patterns gaps** — Rate limiter, token budget management, autonomy gate, DI via Protocol, plain-text console formatter are all undocumented patterns. Updating Key Patterns is a pure documentation task but touches CLAUDE.md which auto-loads in every session; quality matters. Bundle with the ADR writing session.
