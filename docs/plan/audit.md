Audit the Pueo project at the current working directory for organizational consistency, documentation accuracy, and code hygiene. Read only — do not modify any files.

## What Pueo is

Pueo is a local, privacy-first agentic AI system that monitors and self-heals a Home Assistant instance. Key architectural invariants live in CLAUDE.md and docs/decisions/. The implementation backlog is in docs/implementation-plan.md, with full specs in docs/plan/. Three ADRs cover the core design choices.

## Files to read before auditing

Read all of these in full before writing your report:
- CLAUDE.md
- docs/implementation-plan.md
- docs/roadmap.md
- docs/plan/foundation.md
- docs/plan/autonomy.md
- docs/plan/netalertx.md
- docs/plan/evals.md
- docs/decisions/001-config-centralization.md
- docs/decisions/002-safety-invariant.md
- docs/decisions/003-structured-llm-output.md
- config.py
- config.yaml.default
- setup.sh
- interfaces.py
- main.py
- tests/CLAUDE.md
- tests/conftest.py

Also read the first 100 lines of each agent script:
- ha_agent_core.py
- ha_agent_advanced.py
- ha_agent_sandbox_engine.py
- ha_log_monitor.py

And scan tests/test_core.py for test class names and method signatures only (do not read every line of every test body).

## Audit checklist

For each item below, report: ✅ Pass · ⚠️ Warning · ❌ Fail — with a specific finding and a concrete recommended fix where applicable.

---

### 1. Config triple-file rule (ADR 001)

ADR 001 requires every config key to live in exactly three places: `config.py`, `config.yaml.default`, and `setup.sh`.

- Are all keys in `config.py` present in `config.yaml.default`? Are all keys in `config.yaml.default` present in `config.py`? Flag any that are missing in either direction.
- Is every key present in `setup.sh`? If a key is absent from `setup.sh`, is that absence intentional and documented (e.g., a legacy key or a key with a safe machine-only default)? Flag undocumented omissions.
- List any config keys planned in `docs/plan/*.md` (future items) that do not yet exist in `config.py` or `config.yaml.default` — note which plan item introduces them and confirm they are correctly absent until that item is implemented.

---

### 2. Implementation plan accuracy

- For each item marked ✅ Done in `docs/implementation-plan.md`: verify the feature exists in code (look for the key function, class, or decorator named in the corresponding detail file in `docs/plan/`).
- For each item marked ✅ TODO: verify the feature does NOT yet exist in code (no phantom early implementations).
- Cross-check: does the status in `implementation-plan.md` match any completion markers in the detail files (`docs/plan/*.md`)?
- Are there any detail file sections marked "Done" or containing implementation notes that contradict the TODO status in the main plan?

---

### 3. Roadmap ↔ plan alignment

- Do the milestones in `docs/roadmap.md` accurately reflect the current state of `docs/implementation-plan.md`?
- Does `roadmap.md` cover all active development phases (Phase 3.5 — Autonomy; Phase 4 — NetAlertX)?
- Are there milestones in `roadmap.md` with no corresponding plan detail file in `docs/plan/`? (Flag as documentation gaps that need a detail file or explicit "not planned" note.)
- Does the evaluation matrix in `roadmap.md` (latency, hallucination, un-backed writes, WAN packets) still describe all active safety constraints, and do the constraints align with the ADRs?

---

### 4. CLAUDE.md architecture patterns in code

Verify each documented pattern against actual code:

**Safety invariant (ADR 002):** In `ha_agent_sandbox_engine.py`, does every production write path confirm a backup slug before executing? Is the `finally` block present to always revert sandbox changes? Is there any code path that writes without first confirming backup success?

**No re-declared constants:** In each agent script (`ha_agent_core.py`, `ha_agent_advanced.py`, `ha_agent_sandbox_engine.py`, `ha_log_monitor.py`): do any of them redeclare a constant that `config.py` already exports (e.g., hardcoded host, path, model name, threshold)? Only `config.py` should be the source.

**Structured LLM output (ADR 003):** Find every `ollama.chat` call across all files. Does each one use `format=<PydanticModel>.model_json_schema()` and `temperature=0.0`? Flag any that deviate.

**asyncio.to_thread wrapping:** Every `ollama.chat` call must be wrapped in `asyncio.to_thread()`. Verify.

**Sandbox path derivation:** `SANDBOX_REMOTE_DIR` and `SANDBOX_REMOTE_FILE` in `ha_agent_sandbox_engine.py` must be derived from `CONFIG_REMOTE_PATH`, not independently hardcoded. Verify.

**Deferred agent imports in main.py:** Agent modules must be imported inside the `if args.mode` blocks, not at the top of `main.py`. Verify that the top of `main.py` does not import any of `ha_agent_core`, `ha_agent_advanced`, `ha_agent_sandbox_engine`, or `ha_log_monitor`.

**SSH context isolation:** No function should hold a persistent `asyncssh` connection across multiple operations. Each function opens its own `asyncssh.connect()` context manager. Flag any that don't.

---

### 5. Code duplication

- Is `DiagnosticsReport` (the Pydantic model with fields: `is_valid`, `severity`, `identified_issues`, `recommended_fix_yaml`) defined in more than one file? If so, note every file that defines it and flag schema-drift risk.
- Is `_SSH_RETRY` (the retry-config dict built from `SSH_RETRY_ATTEMPTS` and `SSH_RETRY_BASE_DELAY`) defined in more than one file?
- Is the SQLite `_MIGRATIONS` list defined independently in both `ha_agent_advanced.py` and `ha_agent_sandbox_engine.py`? If so, are the lists identical? If they diverge, that is a critical DB schema consistency bug.
- Are any of these duplications documented as intentional (in CLAUDE.md, an ADR, or a code comment)? If not, flag as undocumented technical debt.

---

### 6. Dead or unused config keys

- `OLLAMA_ENDPOINT`: defined in `config.py` and `config.yaml.default` — is it imported or used anywhere? If not, is it documented as reserved for future use?
- `LOG_REMOTE_PATH`: defined in `config.py` — is it imported anywhere? Is it explicitly marked as legacy or deprecated with a comment?
- Scan `config.py` for any other exported constants that are never imported by any agent or utility file.

---

### 7. Test coverage completeness (per tests/CLAUDE.md)

The project rules require:
- Every Pydantic schema → 3 tests: valid construction, invalid/missing fields, JSON round-trip
- Every `config.py` key → 1 test in `TestConfigDefaults`
- Every pure-logic function (path derivation, regex match, threshold comparison, token estimation) → at least 1 test

Check:
- List all Pydantic schemas defined in agent files. For each, confirm all 3 tests exist in `tests/test_core.py`.
- List all constants in `config.py`. For each, confirm a test method exists in `TestConfigDefaults`.
- Identify pure-logic functions in `utils/` and agent files that lack any test coverage.
- Note: SSH and Ollama calls are intentionally NOT mocked (per `tests/CLAUDE.md`) — do not flag this.

---

### 8. CI gate consistency

- Do the CI commands documented in CLAUDE.md exactly match what `.github/workflows/test.yml` runs?
- Are all five quality gates present in both places: `black`, `flake8` (errors only, `E9,F63,F7,F82`), `mypy --ignore-missing-imports`, `bandit -r . -x ./tests`, `pytest --cov`?
- Does CI have a coverage floor (`--cov-fail-under`)? Is that threshold documented in CLAUDE.md?
- Does CI test all three Python versions listed in CLAUDE.md?

---

### 9. Decision record completeness

- CLAUDE.md documents these patterns: config centralization (ADR 001 ✅), backup-before-write (ADR 002 ✅), structured LLM output (ADR 003 ✅), structured logging + correlation IDs, dependency injection via Protocol interfaces, HITL notification infrastructure. Do the latter three have ADRs? If not, flag as ADR gaps.
- Are there patterns visible in code that are neither documented in CLAUDE.md nor in any ADR? (e.g., the rate limiter / debouncer behavior, token budget management, sandbox-then-swap pattern details)
- Do existing ADRs cross-reference each other where relevant? (ADR 002 mentions HITL gates — does `docs/plan/autonomy.md` reference ADR 002?)
- Is there a decision record gap for the "asyncio over LangGraph/CrewAI" choice? (Currently documented in roadmap.md as an architectural note — should it be promoted to an ADR?)

---

### 10. Plan detail file internal consistency

For each detail file (`foundation.md`, `autonomy.md`, `netalertx.md`, `evals.md`):

- Does it clearly identify which `implementation-plan.md` item(s) it covers?
- Does it specify all of: config keys added, SQL schema changes (if any), test requirements, and completion criteria?
- Does each TODO item declare its dependencies? (e.g., item 10 should state it requires item 9.5 complete; items 11–19 should reference item 10)
- Are version-pinned references (e.g., specific NetAlertX or HA version numbers) clearly marked as "current at time of writing" so future implementers know to verify against the latest release before starting?
- Does the `netalertx.md` `netalertx.mode` deprecation (replaced by `agent.autonomy_level`) correctly cross-reference `autonomy.md`?

---

## Output format

### Summary
One paragraph: overall health of the project documentation and code organization. Call out the top 3 issues that most urgently need attention before the next implementation session.

### Findings

Group by section (1–10). For each finding:

**[Section N — Name]** ✅/⚠️/❌
Finding: <specific issue, with file names and line numbers where known>
Recommended fix: <concrete, actionable step>

### Quick-fix list
Bulleted list of changes estimated at < 30 minutes each, ordered by impact. These are candidates for a dedicated cleanup session before any new feature work.

### Technical debt register
Bulleted list of larger issues (> 30 min, requiring design decisions). Do not propose implementations — just name the risk, note the affected files, and suggest whether the resolution belongs in a new ADR, a new plan item, or a CLAUDE.md update.
