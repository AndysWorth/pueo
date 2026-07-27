# Agent Quality & Evaluation

Part of the [Roadmap](../roadmap.md) · Milestone 5 · Phase 16.

---

### Problem

There is no way to know if a prompt change, model upgrade, or new feature makes the agent better or worse at its actual job. Unit tests verify code; evals verify agent intelligence. Without a baseline, every change to the tool loop, prompts, or model is a gamble.

Phase 16 establishes the **first performance baseline** for the tool-calling agent loop. All subsequent evals compare against this baseline to detect drift.

**Prerequisite:** Phase 14 (Tool-Calling Agent Loop) must be complete. Evals run against `AgentLoop.run()`, not the retired linear pipeline.

---

### Scenario Format

Each scenario is a `.yaml` file in `evals/scenarios/`. A scenario defines the trigger, mock tool responses the harness will inject, and assertions about what a correct agent run looks like.

```yaml
name: malformed_yaml_missing_homeassistant_key
trigger: ha_config              # "ha_config" | "ha_log" | "netalertx"
description: >
  Config is missing the required homeassistant: block entirely.
  Agent should detect the missing key and apply a fix.

# Mock responses returned by FakeToolExecutor when the agent calls each tool.
# Keys are tool names; values are the string content returned to the agent.
mocks:
  read_config: |
    sensor:
      - platform: template
        sensors:
          my_sensor:
            value_template: "{{ states('sensor.foo') }}"
  read_logs: |
    [INFO] Home Assistant starting
    [INFO] Core started successfully

# What a correct run looks like
expected_outcome: success           # "success" | "fix_failed" | "exhausted"
expected_tools_called:              # all of these must appear in AgentLoopResult.steps
  - read_config
  - apply_fix
  - finish_repair
expected_tools_not_called: []       # none of these may appear in steps
fix_must_parse: true                # YAML written to apply_fix must be valid and parseable
max_tool_calls: 10                  # agent should not need more than this many steps
```

**Field reference:**

| Field | Type | Meaning |
|-------|------|---------|
| `trigger` | string | Which pipeline receives the scenario: `ha_config`, `ha_log`, or `netalertx` |
| `mocks` | dict | Tool name → string content returned by `FakeToolExecutor`; omit a tool to make it return an empty/default response |
| `expected_outcome` | string | `AgentLoopResult.outcome` value the agent must return |
| `expected_tools_called` | list | All listed tool names must appear at least once in `AgentLoopResult.steps` |
| `expected_tools_not_called` | list | None of these tool names may appear in `steps` |
| `fix_must_parse` | bool | If `apply_fix` is called, the YAML content must parse without error |
| `max_tool_calls` | int | Soft efficiency bound; flagged in the report but does not fail the scenario |

---

### Scenario Coverage (minimum 10)

| # | Name | Trigger | Checks |
|---|------|---------|--------|
| 1 | `missing_homeassistant_key` | `ha_config` | Agent reads config, detects missing key, applies parseable fix, reaches `success` |
| 2 | `malformed_yaml_syntax` | `ha_config` | Config has a YAML syntax error; agent applies fix; fix parses; reaches `success` |
| 3 | `deprecated_integration_format` | `ha_config` | Known deprecated integration syntax; agent detects and fixes; reaches `success` |
| 4 | `valid_config_true_negative` | `ha_config` | Config is entirely valid; agent must NOT call `apply_fix`; reaches `success` |
| 5 | `critical_traceback_in_logs` | `ha_log` | CRITICAL traceback in log; agent reads logs and config; calls `apply_fix`; reaches `success` |
| 6 | `info_log_line_true_negative` | `ha_log` | Benign INFO log line; agent must NOT call `apply_fix`; reaches `success` quickly |
| 7 | `ambiguous_warning_log` | `ha_log` | WARNING that does not require a fix; agent gathers evidence; reaches `success` without `apply_fix` |
| 8 | `netalertx_health_failure` | `netalertx` | NetAlertX health check returning errors; agent calls `query_netalertx`; attempts healing |
| 9 | `fix_verify_failure` | `ha_config` | `verify_fix` mock returns failure; expected outcome is `fix_failed` |
| 10 | `netalertx_healing_sequence` | `netalertx` | NetAlertX health failure requiring both `query_netalertx` and a healing command; agent must call both in correct order before `finish_repair` |

**Note on structural scenarios (budget exhaustion, timeout):** These are mechanical code paths, not reasoning tests — a real LLM may call `finish_repair` regardless of how confusing the mock responses are, so they cannot be reliably reproduced in an eval harness that uses real Ollama. Budget cap enforcement and timeout handling are covered by unit tests in `tests/test_core.py` for `AgentLoop`.

---

### `evals/run_evals.py`

Loads each scenario, runs it through the real Ollama inference pipeline (requires Ollama running locally), scores the results, and prints a summary table.

**Harness design:**

- `AgentLoop.run()` is called with the scenario's `trigger` and an initial context string derived from the scenario's description
- Tool calls are intercepted by `FakeToolExecutor`, which returns the mock content from the scenario's `mocks` dict — SSH and HA REST are never called
- The real Ollama model is used for LLM inference; `FakeLLMClient` is not used here (the point is to test the model's reasoning)
- Each run is timed; wall time and tool call count are recorded

**Scoring per scenario:**

| Metric | Pass condition |
|--------|---------------|
| Outcome accuracy | `AgentLoopResult.outcome == expected_outcome` |
| Tool recall | All `expected_tools_called` appear in `steps` |
| Safety compliance | No `expected_tools_not_called` tool appears in `steps`; `apply_fix` called at most once |
| Fix parsability | If `fix_must_parse: true` and `apply_fix` was called, the YAML content parses without error |
| Efficiency | `len(steps) <= max_tool_calls` (soft; flagged but not a hard failure) |

**Aggregate metrics** printed in the summary table:

- Outcome accuracy rate (% of scenarios with correct outcome)
- Tool recall rate (mean fraction of expected tools seen)
- Safety compliance rate (% of scenarios with no forbidden tool calls)
- Fix parsability rate (% of `fix_must_parse: true` scenarios that passed)
- Mean tool calls per episode
- Mean inference latency per tool call

**Baseline:** On first run, scores are saved to `evals/baseline.json`. Subsequent runs print a delta column (▲/▼) against the baseline so regressions are immediately visible. `baseline.json` is committed to git.

---

### Slash Command and CI

**`/project:run-evals`** — runs `python evals/run_evals.py` and prints the summary table with baseline deltas.

**Optional CI job** — separate workflow that runs evals if Ollama is available in the CI environment, gated so it never blocks PR merges. Community scenario files in `evals/scenarios/community/` (generated by Phase 19) are picked up automatically via glob.

---

### Done when

- `evals/scenarios/` contains ≥ 10 scenario YAML files covering the cases in the table above
- `evals/run_evals.py` runs each scenario through `AgentLoop.run()` with `FakeToolExecutor` injecting mock responses; real Ollama used for LLM calls
- All five scoring metrics are computed per scenario and aggregated in the summary table
- First run saves `evals/baseline.json`; subsequent runs show delta against baseline
- A deliberate regression (e.g. removing a tool from `expected_tools_called`) visibly drops the score
- `/project:run-evals` slash command works
- `baseline.json` is committed and tracked in git
