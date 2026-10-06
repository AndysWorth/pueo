# ADR 035 — Full-Autonomous Level + Autonomy Level Renumber

**Status:** Accepted  
**Date:** 2026-10-06

## Context

Pueo's autonomy gate previously used levels 1–4:
- 1 REPORT_ONLY — observe and report; never act
- 2 SUGGEST — propose every action; require human approval
- 3 GUIDED — auto-execute LOW-risk; pause for MEDIUM / HIGH / CRITICAL
- 4 AUTONOMOUS — auto-execute LOW / MEDIUM / HIGH; pause for CRITICAL

Level 4 (AUTONOMOUS) still required a human approval card for CRITICAL-risk items — specifically Core and OS updates. Users who want fully unattended operation have no path to auto-apply these updates.

Meanwhile, the LLM analysis (`_finish_update_analysis`) already produces a structured safety assessment with `safe_to_update` and could naturally provide a `confidence` score.

## Decision

1. **Renumber levels 0–3** — same behavior, new numbers, zero-indexed convention:
   - 0 REPORT_ONLY, 1 SUGGEST, 2 GUIDED, 3 AUTONOMOUS

2. **Add level 4 FULL_AUTONOMOUS** — auto-applies CRITICAL-risk items when:
   - `safe_to_update=True` (LLM assessment)
   - `confidence ≥ AUTO_APPLY_CONFIDENCE_THRESHOLD` (default 0.85)
   - Below the threshold, falls back to level 3 behavior (sends an approval card)

3. **Bypass lives in `_finish_update_analysis()`**, not inside `AutonomyGate` — the gate is domain-agnostic. `AutonomyGate` at level 4 already returns `True` for all risks including CRITICAL in `require_approval()` and `queue_for_approval()`. The update-specific confidence check is an additional domain constraint that belongs next to the update logic, not in the generic gate.

4. **`FakeAutonomyGate` is unchanged** — it does not implement a `level` property. The `isinstance(gate, AutonomyGate)` check in `_finish_update_analysis()` ensures the fake always takes the card-sending path in tests, which is the correct test-double behavior.

## Breaking Change

⚠️ **Existing users with `autonomy_level: 2` in their `config.yaml` will shift from SUGGEST → GUIDED** after upgrading. GUIDED auto-executes LOW-risk actions without asking (e.g. read-only calls, name locks). Users who want the old level-2 behavior must change their `config.yaml` to `autonomy_level: 1`.

The default also changes from 2 (SUGGEST) to 1 (SUGGEST) — numerically different, behaviorally identical for new installs.

## Consequences

- Any caller that constructs `AutonomyGate(level=N)` with a hardcoded integer for the old 1–4 range must be updated to N-1. The investigation loop's `AutonomyGate(level=3)` was the only such call in production code.
- The `confidence` field is optional in `finish_update_analysis` — older LLM calls that omit it default to `None`, which is treated as below-threshold (card is sent).
- `AUTO_APPLY_CONFIDENCE_THRESHOLD` is configurable per-instance in `config.yaml` so operators can tighten or loosen the gate.
- `AutonomyGate` gains a public `level` property so callers can read the current level without reaching into `_level`.
