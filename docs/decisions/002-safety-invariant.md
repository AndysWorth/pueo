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
- Future HITL (human-in-the-loop) gates for critical changes (e.g., HACS updates) should be added before step 1, not between steps.
