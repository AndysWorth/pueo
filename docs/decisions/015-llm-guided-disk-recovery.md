# ADR 015 — LLM-guided disk recovery via investigation loop

## Status
Accepted

## Context
`utils/disk_recovery.py` makes all recovery decisions procedurally. When HA disk space drops below the critical threshold, it runs a fixed sequence of SSH commands: check backup count, offload oldest backup, purge recorder DB, clear logs. The order and selection of actions are hardcoded.

The module's own docstring described it as "a hardcoded instantiation of the investigation pattern." `utils/investigation_loop.py` was written as the generalisation of exactly this pattern — a read-only, LLM-guided agent that forms hypotheses about what is consuming space, gathers evidence, and returns structured `auto_actions` and `hitl_actions` with risk levels. The two modules existed in parallel without being connected.

This was the only significant Pueo subsystem where a judgment call about which actions to take in what order was made procedurally rather than through LLM reasoning. The architectural principle in `CLAUDE.md` (Key Patterns: "LLM-guided all actions") explicitly requires that functions changing HA state or making judgment calls about what to do next belong in an agent loop, not a direct call.

The heuristic sequence was not wrong — it handled the most common cases. But it could not adapt to unusual situations (e.g., a single enormous backup dominating disk usage while the log directory is small), and it produced no human-readable explanation of why a particular action was chosen.

## Decision
`run_disk_recovery()` now calls `investigate_with_fallback(topic="HA disk space critically low", ...)` at the start, before running any SSH commands. The investigation loop gathers evidence (backup sizes, log directory size, recorder DB size), forms a hypothesis about the dominant cause, and returns a structured `InvestigationReport` with:

- `auto_actions` — actions the loop may take autonomously, each with an `action_key` that maps to an existing recovery function
- `hitl_actions` — actions requiring human approval, surfaced as approval cards

If the investigation succeeds, the recovery sequence is driven by the report's `auto_actions` list rather than the hardcoded sequence. The `action_key` field dispatches to existing `disk_recovery.py` functions via a lookup table — no new recovery capability is introduced, only the decision about which capability to invoke.

If the investigation times out or raises, the heuristic sequence runs unchanged as a fallback. No regression in reliability.

## Rationale
The investigation loop already handles evidence gathering and hypothesis formation correctly. Reusing it for disk recovery:

1. **Applies the principle consistently.** Every Pueo subsystem that makes judgment calls about HA state now goes through LLM reasoning.
2. **Handles unusual distributions.** A model that has read the actual backup sizes will prioritise offloading the three 4 GB backups over purging a 50 MB log directory; the heuristic cannot do this.
3. **Generates an explanation.** `InvestigationReport.summary` is surfaced in the dashboard timeline, giving the user a plain-English account of why particular recovery actions were taken.
4. **Zero regression risk.** The heuristic fallback means a failed or timed-out investigation degrades gracefully to the previous behaviour.

## Consequences
- `run_disk_recovery()` accepts `llm_client` and `knowledge_store` parameters (dependency-injected); defaults to `None`, which skips the investigation and runs the heuristic
- The action-key dispatch table in `disk_recovery.py` is the authoritative mapping from `InvestigationAction.action_key` to callable; `action_key` values must match keys in this table or the action is skipped with a warning
- `investigate_with_fallback()` is called with a 60-second timeout; disk recovery is time-sensitive and a long-running investigation that blocks the emergency recovery path would make things worse
- The dashboard timeline gains a `disk_recovery_investigation` event row showing the model's hypothesis and chosen action sequence
- Tests must cover: investigation success (mock returns report, assert actions match report), investigation failure (mock raises, assert heuristic runs), unknown action_key (assert warning logged, not raised)

## Implementation detail — `finish_investigation` schema

The terminal tool for disk-recovery investigation sessions. The LLM calls it with:

```json
{
  "summary": "Plain-English diagnosis of root cause(s)",
  "root_causes": ["..."],
  "auto_actions": [{"name": "...", "description": "...", "estimated_impact": "...", "reversible": true, "risk_level": "LOW", "action_key": "..."}],
  "hitl_actions": [...],
  "manual_only": ["..."],
  "knowledge_sources": ["chunk/source refs used"],
  "confidence": 0.85
}
```

`action_key` is the dispatch key that maps the LLM's chosen action to a concrete Python function in `disk_recovery.py`. An unrecognised `action_key` is silently skipped with a warning.

**Three-tier classification:**
- `auto_actions` — LOW risk, reversible or acceptable loss, no service interruption → executed immediately without approval
- `hitl_actions` — MEDIUM/HIGH risk, or meaningful data loss or downtime → surfaced as a `CARD_TYPE_DISK_RECOVERY` approval card
- `manual_only` — requires physical access, hypervisor changes, or human judgment on content → listed in the card body only

**Disk-space action tiers (implemented in `utils/disk/disk_recovery.py`):**

Auto-safe (run immediately):
- Truncate `/config/home-assistant.log` — can free 100 MB–28 GB; HA keeps the file handle open
- Vacuum systemd journal to ≤200 MB — typically saves 1–5 GB
- `recorder.purge(keep_days=30, repack=False)` — frees logical space quickly

Approval-required:
- `recorder.purge(repack=True)` — physical compaction; needs ~2.5× DB size in free space
- Aggressive purge (keep_days=7) — larger history loss
- Clear `/mnt/data/supervisor/tmp/` — orphaned failed-backup temp files (up to 60 GB)
- Delete old backups from HA — irreversible without a confirmed local copy

Manual-only:
- Expanding the VM disk (hypervisor action)
- Deciding which camera recordings or media files to keep

The schema is domain-agnostic — `run_investigation()` in `utils/agent/investigation_loop.py` uses the same `finish_investigation` tool for any topic; only the `action_key` values and dispatch table are domain-specific.

## Related decisions
- [ADR 002 — Safety invariant](002-safety-invariant.md): disk recovery does not write HA config; the backup-before-write invariant does not apply. But any recovery action that deletes a backup must confirm the backup is offloaded to Pueo storage first (the SHA-256 gate in the evaluation matrix).
- [ADR 005 — asyncio over agentic framework](005-asyncio-over-agentic-framework.md): `investigate_with_fallback()` is itself an asyncio coroutine; no framework change required.
- [ADR 012 — Hypothesis-driven repair cycle](012-hypothesis-driven-repair.md): the investigation loop used here uses the same hypothesis-driven prompt pattern established for repair.
