# Pueo Investigation Prompt — General Methodology

This document describes the investigative process Pueo uses when encountering an unfamiliar
failure mode or a domain where it needs to determine best practices before acting.

The pattern is implemented in `utils/investigation_loop.py` and can be invoked via
`run_investigation(topic, goal, context, llm_client, knowledge_store)`.

---

## The Five-Step Pattern

Every Pueo investigation follows this sequence regardless of domain:

### 1. Gather Evidence
Use read tools to collect current system state relevant to the topic:
- `get_disk_usage` — for storage problems
- `read_logs` — for error patterns
- `run_ha_command("ha host info")` / `run_ha_command("ha backups list")` — for HA state
- `read_file` — for config files

Do not skip evidence gathering to jump to recommendations.

### 2. Consult Knowledge
Always call `query_knowledge` with relevant search terms before forming conclusions:
- Use the specific problem area: `"recorder database purge disk space"`
- Use component names: `"systemd journal vacuum HAOS"`
- Use best-practice phrases: `"home assistant disk full recovery"`

If the knowledge store has no relevant chunks, note this in `knowledge_sources` and lower
`confidence` accordingly. Never guess at best practices.

### 3. Identify Root Causes
Reason from evidence to underlying causes — not symptoms:
- Symptom: "disk is full"
- Root cause: "recorder DB grew to 8 GB due to high-frequency entity polling with no purge schedule"

List each root cause separately.

### 4. Rank Remediation Options
For each option, assess four dimensions:

| Dimension | Questions |
|---|---|
| **Impact** | How much space / performance / reliability does this recover? |
| **Reversibility** | Can the action be undone? Deleting history cannot be; compacting a DB can be redone. |
| **Risk level** | LOW / MEDIUM / HIGH / CRITICAL (see below) |
| **Autonomy** | auto_actions / hitl_actions / manual_only (see below) |

**Risk levels:**
- `LOW` — read-only or easily reversible; no service interruption
- `MEDIUM` — modifies data; recoverable via backup; no outage
- `HIGH` — irreversible data loss or service interruption
- `CRITICAL` — production outage or unrecoverable data loss risk

**Autonomy classification:**
- `auto_actions` — LOW risk, reversible-or-acceptable-loss, no service interruption → execute immediately without HITL
- `hitl_actions` — MEDIUM/HIGH risk, or any action with meaningful data loss or downtime → require user approval
- `manual_only` — requires physical access, hypervisor changes, or human judgment on content (e.g. "which camera recordings to keep")

### 5. Report Structured Findings
Call `finish_investigation` with:
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

---

## Disk Space — Instantiated Example

The disk-critical domain is fully implemented in `utils/disk_recovery.py` as a hardcoded
version of this pattern. The `ResourcePoller._check_and_alert()` runs the safe steps
automatically and produces a `CARD_TYPE_DISK_RECOVERY` HITL card for destructive options.

**Auto-safe (run immediately on disk critical):**
- Truncate `/config/home-assistant.log` — can free 100 MB – 28 GB; HA keeps the file handle
- Vacuum systemd journal to ≤200 MB — typically saves 1–5 GB
- `recorder.purge(keep_days=30, repack=False)` — frees logical space quickly

**HITL-required:**
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
4. Send `report.hitl_actions` to a domain-appropriate HITL card.
5. List `report.manual_only` in the card body.

No bespoke code needed for new domains — the prompt template and structured output schema
are domain-agnostic.
