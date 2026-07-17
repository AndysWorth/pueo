# Agent Quality — Item 20

Part of the [Implementation Plan](../implementation-plan.md) · Phase 5 · 1–2 sessions.

---

### 20. Evals with Synthetic HA Scenarios ✅ TODO
**Problem:** There is no way to know if a prompt change, model upgrade, or new feature makes the agent better or worse at its actual job. Unit tests verify code; evals verify agent intelligence.

**Build:**
- `evals/scenarios/` — directory of `.yaml` files, each defining: `name`, `input_config` or `input_log_line`, `expected_is_valid`, `expected_severity`, `expected_issue_keywords: list[str]`, `fix_must_parse: bool`
- Minimum 10 scenarios covering: malformed YAML, missing required key, deprecated integration format, valid config (true negative), CRITICAL traceback log line, INFO line (true negative), ambiguous WARNING
- `evals/run_evals.py` — loads each scenario, runs it through the real Ollama inference pipeline (requires Ollama running locally), scores results, prints a summary table, saves scores to `evals/baseline.json` on first run, compares against baseline on subsequent runs
- Scoring metrics: `is_valid` accuracy, severity accuracy, issue keyword recall, fix YAML parse success rate, mean inference latency

**Add slash command:** `/project:run-evals` — runs `python evals/run_evals.py` and summarises results.

**Add to CI (optional):** A separate workflow job that runs evals against Ollama if available, gated so it does not block PR merges.

**Done when:** Running `python evals/run_evals.py` produces a score table against ≥ 10 scenarios; a deliberate prompt regression visibly drops the score; baseline is committed and tracked in git.
