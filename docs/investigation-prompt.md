# Pueo Investigation Prompt — General Methodology

> **Status:** The 5-step investigation methodology described here is superseded by
> [ADR 018 — Unified Agent Methodology](decisions/018-unified-agent-methodology.md),
> which defines the canonical 6-phase investigation cycle used by all Pueo agent sessions.
> The `finish_investigation` schema and disk-space domain reference below remain accurate.

The investigation pattern is implemented in `utils/agent/investigation_loop.py` and can be
invoked via `run_investigation(topic, goal, context, llm_client, knowledge_store)`.

---

## `finish_investigation` Schema

The terminal tool for investigation sessions. The LLM must call it with:

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

`action_key` is the dispatch key used by the caller (e.g. `disk_recovery.py`) to map the
LLM's chosen action to a concrete Python function. The valid keys for each domain are listed
in the domain sections below. An unrecognised `action_key` is silently skipped.

**Risk levels / autonomy classification:**
- `auto_actions` — LOW risk, reversible or acceptable loss, no service interruption → executed immediately without approval
- `hitl_actions` — MEDIUM/HIGH risk, or meaningful data loss or downtime → require user approval
- `manual_only` — requires physical access, hypervisor changes, or human judgment on content

---

## Disk Space — Instantiated Example

The disk-critical domain is fully implemented in `utils/disk_recovery.py` as a hardcoded
version of this pattern. The `ResourcePoller._check_and_alert()` runs the safe steps
automatically and produces a `CARD_TYPE_DISK_RECOVERY` approval card for destructive options.

**Auto-safe (run immediately on disk critical):**
- Truncate `/config/home-assistant.log` — can free 100 MB – 28 GB; HA keeps the file handle
- Vacuum systemd journal to ≤200 MB — typically saves 1–5 GB
- `recorder.purge(keep_days=30, repack=False)` — frees logical space quickly

**approval-required:**
- `recorder.purge(repack=True)` — physical compaction; needs ~2.5× DB size free space
- Aggressive purge (keep_days=7) — larger history loss
- Clear `/mnt/data/supervisor/tmp/` — orphaned failed-backup temp files (up to 60 GB)
- Delete old backups from HA — irreversible without local copy

**Manual-only:**
- Expanding the VM disk (hypervisor action)
- Deciding which camera recordings or media files to keep

---

## Adding a New Domain

To investigate a new problem domain (e.g. "network latency", "memory leak", "HACS repo conflict"):

1. Call `run_investigation(topic="...", goal="...", context="...")` from wherever the trigger fires.
2. The agent uses `build_investigation_tool_registry()` (read-only tools + `finish_investigation`).
3. Parse `report.auto_actions` and execute safe steps immediately.
4. Send `report.hitl_actions` to a domain-appropriate approval card.
5. List `report.manual_only` in the card body.

No bespoke code needed for new domains — the prompt template and structured output schema
are domain-agnostic.
