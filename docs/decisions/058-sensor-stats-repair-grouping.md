# ADR 058 — Sensor statistics repair grouping

**Status:** Accepted (2026-11-21)

## Context

HA raises separate repair issues for each sensor whose long-term statistics are
broken (e.g. `state_class_removed`, `units_changed`).  When a device re-registers
its entities (e.g. iOS Companion App update), all pedometer sensors lose their
state_class / unit simultaneously, producing 5–10 issues that share one root cause.

Before this ADR, `poll_for_repairs` launched a separate `AgentLoop` for each issue.
This triggered 610 repeat investigations on 5 iPhone sensors (Nov 17–21), generating
1,283 distillation LLM jobs and a circular learned rule.  The core problem was that
each per-issue session concluded `requires_hitl=false` (no card) and S1 (#1270)
stopped the loop by recording the no-card conclusion — but the grouping needed in S2
was still missing.

## Decision

Group sensor statistics repair issues by HA device before dispatching to AgentLoop:

1. **Translation-key filter.** Issues with `domain == "sensor"` and
   `translation_key in _STATISTICS_TRANSLATION_KEYS` are diverted in
   `poll_for_repairs` before the per-issue investigation path.
2. **Group key.** `sensor/stats:<device_id>` (from WS entity registry), falling back
   to `sensor/stats:<statistic_id>` when no device is linked.  This key is stored in
   `ha_repair_history` and subjected to the same hitl_sent_at / concluded_fp checks
   as individual issues (S1, ADR from #1270).
3. **One session per group.** `_run_sensor_stats_investigation` builds a deterministic
   evidence block via `utils/ha/sensor_stats_evidence.py` and pre-injects it as
   `initial_context`.  Terminal tool is `finish_sensor_stats`.
4. **Card.** `CARD_TYPE_SENSOR_STATS` shows all affected entities and an
   "Ignore all in HA" action (WS `repairs/ignore_issue` for each member).
5. **Seed.** `seed_repair_sensor_statistics.md` supplies verdict rules:
   - Re-creation date + missing unit/state_class → source integration / app cause.
   - No device_class → never suggest a unit override.
   - Statistics stopped → never conclude benign.
   - Earlier sessions are background, not evidence.

## Alternatives considered

- **One card per issue:** rejected — the root cause is the same and multiple cards
  cause alert fatigue.
- **Generic repair investigation for each issue:** this was the pre-S2 state and caused
  the loop post-mortem scenario.

## Consequences

- 5–10 sensor statistics issues on the same device produce exactly 1 investigation and
  1 card per distinct group fingerprint.
- The "Ignore all in HA" action gives the user a one-click way to suppress all issues
  when the root cause cannot be fixed immediately (e.g. waiting for an app update).
- Group key stored in `ha_repair_history` via `record_repair_seen`; group resolution
  calls `mark_repair_resolved(group_key)` and each member key.
