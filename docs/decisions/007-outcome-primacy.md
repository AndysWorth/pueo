# ADR 007 — Outcome primacy: correct fixes over arbitrary limits

## Status
Accepted

## Context
Pueo is a self-healing system for Home Assistant. Its purpose is to keep HA running
correctly and smoothly. Every numeric limit in the codebase (tool-call budget, wall-clock
timeout, confidence threshold, retry cap) was chosen at a point in time with imperfect
information. Some of these limits are safety guards (billing caps, backup invariant, disk
thresholds) — they exist to prevent harm and must be respected unconditionally. Others are
operational heuristics (agent loop budget, investigation timeout, no-tool-streak counter)
— they exist to prevent runaway behavior, not to prevent correct solutions.

When an operational heuristic causes a solvable problem to go unsolved, the heuristic is
misconfigured, not the situation. A repair loop that exhausts its tool budget without
finding a fix has not succeeded; a timeout that fires before an answer is reached has not
protected anything.

## Decision

### 1. Tool-call budgets and wall-clock timeouts are not hard stops

Defaults are raised to generous values that accommodate genuinely complex HA failures.
More importantly: when a limit fires, the LLM gets one structured meta-review call before
the loop declares exhausted. The LLM assesses why the limit was hit, whether more calls
would lead to a solution, and how many it needs. If the LLM confirms it is stuck or that
more budget would not help, then and only then does the loop return `"exhausted"`. This
makes the LLM the decision-maker at its own boundary conditions.

The meta-review call uses structured output (`LimitReviewDecision` Pydantic schema) so
the response is always parseable. Its timeout is **adaptive**: Pueo measures the average
LLM round-trip time from the steps already accumulated in the loop (`total_elapsed /
step_count`), then sets the review timeout to `max(30s, min(3 × avg_step_time, 300s))`.
On fast hardware (avg 3 s/step) this is 30 s; on slow hardware (avg 60 s/step) this is
180 s; the 300 s ceiling prevents an infinite wait if the model goes silent. If no steps
have been recorded yet (limit hit on the first call), the timeout defaults to 60 s. If
the meta-review call itself fails or times out, the loop falls back to the original
`"exhausted"` / `"timeout"` outcome — the review is best-effort, not load-bearing.

Total budget across all extensions is capped at `AGENT_MAX_TOTAL_CALLS` (default 60) to
prevent runaway loops even with a cooperative model. The LLM may request up to
`AGENT_MAX_EXTENSION_CALLS` (default 15) additional calls per extension; extensions beyond
one are allowed only if each prior extension made measurable progress (a new tool was
called successfully).

### 2. Cloud escalation is gated by autonomy level

When the local loop still cannot reach `finish_repair` after all limit reviews:
- `LLM_PROVIDER=both` triggers a cloud escalation attempt
- The escalation is treated as a HIGH-risk action and passed through `AutonomyGate`
- At autonomy level 4 (AUTONOMOUS), `gate.require_approval(risk=HIGH)` returns True
  immediately — no approval card, no human delay
- At level 3 (GUIDED) or 2 (SUGGEST), an approval `cloud_escalation` card is created; the
  human decides whether to invoke the cloud model
- At level 1 (REPORT_ONLY), cloud escalation is never offered

This reuses the existing `AutonomyGate.require_approval()` contract exactly. No new
autonomy concepts are introduced.

### 3. Safety limits are exempt

Billing caps, the backup-before-write invariant, disk thresholds, and rate limits that
prevent runaway repairs are not raised or bypassed. These protect real resources.

### 4. Hardcoded thresholds that cannot be tuned without a code edit are surfaced to config

Every limit an operator might reasonably want to adjust belongs in `config.yaml`.

## Consequences
- `AGENT_MAX_TOOL_CALLS` default raised from 20 → 30.
- `AGENT_MAX_WALL_SECONDS` default raised from 120 → 300.
- `AGENT_MAX_EXTENSION_CALLS` added (default 15): max additional calls per limit-review.
- `AGENT_MAX_TOTAL_CALLS` added (default 60): absolute ceiling across all extensions.
- `LimitReviewDecision` Pydantic schema added to `utils/agent_loop.py`.
- `AgentLoop._loop_body()` and `AgentLoop.run()` gain limit-review logic at exhaustion
  and timeout boundaries.
- Cloud escalation in `both` mode is gated by autonomy level via `AutonomyGate`.
- The approval `cloud_escalation` card path is preserved for levels 1–3; at level 4 it is
  bypassed entirely and cloud runs immediately.
- A shared `run_cloud_escalation()` coroutine in `utils/cloud_escalation.py` is used by
  both the auto-path (level 4) and the approve path (levels 2–3).
- `NETALERTX_SEVERITY_CONFIDENCE_THRESHOLD` added to config; hardcoded `0.9` removed.
- `utils/investigation_loop.py` defaults raised to match the main loop.

## Related decisions
- ADR 002 — Safety invariant: backup-before-write is a safety limit; outcome primacy
  does not override it.
- ADR 006 — LLM provider abstraction: billing caps and `BillingCapError` remain intact;
  automatic escalation is blocked if either per-incident or daily cap is exceeded.
