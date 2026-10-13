# ADR 040 — Gated HA CLI commands

**Status:** Accepted
**Date:** 2026-10-13

## Context

`run_ha_command` in `ToolExecutor` runs HA Supervisor CLI commands over SSH. Most
commands are read-only diagnostic queries (`ha core check`, `ha core info`,
`ha apps list`, etc.) and should execute immediately. A subset are state-changing:

| Command | Effect |
|---|---|
| `ha core restart` | Restarts HA Core (brief service outage) |
| `ha core stop` | Stops HA Core entirely |
| `ha supervisor repair` / `ha su repair` | Removes unused container image layers; reboot recommended |

ADR 041 extends this with four additional gated commands needed by the host-health
investigation pipeline — including parameterized forms (with UUID / slug arguments)
that require a `_ha_command_risk()` function rather than a plain dict lookup.
See ADR 041 for the full command table and validation regex design.

These state-changing commands were executing directly over SSH with no gate, violating
the autonomy-gate invariant in `docs/key-patterns.md` ("Every action that touches remote
state must call `gate.require_approval()`").

## Decision

Introduce `_HA_COMMAND_RISK: dict[str, str]` in `utils/agent/tool_executor.py` that maps
each state-changing allowlisted command to its `RiskLevel`. `_run_ha_command` checks this
map before execution: if the command is present, it calls
`gate.queue_for_approval(risk=RiskLevel[risk_name])` with `CARD_TYPE_HA_COMMAND` in the
payload. Read-only commands (absent from the map) continue to execute directly.

Risk tiers:
- **MEDIUM** — `ha core restart`, `ha supervisor repair`, `ha su repair`
- **HIGH** — `ha core stop`

A card executor `_execute_ha_command` in `web/dashboard.py` handles approved cards:
1. Re-validates the command is in both `_HA_COMMAND_ALLOWLIST` and `_HA_COMMAND_RISK`
   (defense-in-depth: the `.json` card file is written to disk and could be tampered
   with before approval).
2. Runs the command via `AsyncSSHClient.run()`.
3. Writes `.approved` / `.rejected` sentinel files and a timeline event.

## No backup required

Unlike config writes (which follow the safety invariant: backup → write), these CLI
commands do not modify the HA configuration file. `ha core restart` and `ha core stop`
change process state, not files; `ha supervisor repair` removes unused container images.
No backup is triggered before these commands.

## Consequences

- State-changing HA CLI commands are now subject to the same autonomy-gate/HITL flow as
  service calls and config repairs.
- Callers using an auto-approve gate (investigation executors with `FakeAutonomyGate` at
  `AUTONOMOUS` level) are unaffected — the gate approves immediately.
- The `run_ha_command` tool description now lists which commands require approval.
- `prompts/seed_supervisor_cli.md` notes that restart/stop/repair queue an approval card
  at non-autonomous autonomy levels.
