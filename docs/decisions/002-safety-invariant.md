# ADR 002 — Backup-before-write safety invariant

## Status
Accepted

## Context
An AI agent that can write to a production Home Assistant configuration is dangerous if it operates without a recovery path. A bad config write could break the entire smart home. The agent needs to be able to act autonomously but must never leave the system in a state it cannot recover from.

## Decision
No write operation against the live HA configuration may proceed without a confirmed backup snapshot. The enforced ordering is:

1. `execute_remote_backup()` — triggers `ha backup new` over SSH
2. `record_backup_slug()` — saves the slug to the local SQLite registry
3. Remediation action (sandbox deploy → atomic swap)

If the backup step fails, the pipeline aborts and raises. There is no bypass.

## Consequences
- Every repair cycle creates a backup, even for minor fixes. This is intentional — storage is cheap, an unrecoverable HA instance is not.
- The sandbox test (write to `.agent_sandbox/`, validate, revert) provides a second safety layer: the backup is the recovery mechanism, the sandbox is the prevention mechanism.
- HITL gates for CRITICAL changes are placed before step 1, not between steps. The `AutonomyGate.require_approval()` call in `ha_agent_sandbox_engine.py` is the HITL gate; it precedes the backup trigger.

## Related decisions
- [ADR 001 — Config centralization](001-config-centralization.md): `CONFIG_REMOTE_PATH` (from `config.py`) governs where the backup target lives; the backup path is not independently hardcoded.
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): The `DiagnosticsReport.is_valid` field from Ollama is the trigger that initiates the backup chain; schema correctness is required for the safety invariant to fire reliably.
