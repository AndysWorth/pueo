# Repair Episode Recording

Part of the [Roadmap](../roadmap.md) · Milestone 8.

---

### Problem

Every successful diagnosis-and-fix contains valuable information: what symptoms appeared, which tools the agent called, what hypothesis chain it followed, what fix it applied, and whether verification passed. Currently this is discarded at the end of each repair cycle. Without a record, Pueo cannot build institutional memory, and the Federated Case Library (Milestone 9) has nothing to contribute.

---

### Episode Schema

```python
RepairEpisode(
    id: str,                          # UUID
    timestamp: float,                 # Unix epoch
    trigger: str,                     # "ha_log" | "netalertx" | "manual" | "escalated"
    symptoms: list[str],              # extracted from tool call history at finish_repair
    tool_sequence: list[ToolCall],    # full ordered list from AgentLoopResult.steps
    hypothesis_chain: list[str],      # model's stated reasoning at each step
    fix_applied: str | None,          # YAML patch text, or None if diagnosis-only
    verification_result: bool,
    model_used: str,                  # e.g. "qwen2.5-coder:7b" or "claude-sonnet-4-6"
    escalated: bool,
    duration_seconds: float,
)
```

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 50 | `repair_episodes` SQLite table (new migration version); `RepairEpisode` dataclass; serialization helper |
| 51 | Serialization hook at `finish_repair` tool call in `AgentLoop`; update `LLMTrace` to include episode reference |
| 52 | `--mode export-episodes --since <date>` CLI: anonymized YAML output; episodes tab in HITL dashboard |

---

### Anonymization Rules

Applied before any export or sharing, not to the stored record:

| Pattern | Replacement |
|---------|-------------|
| IPv4/IPv6 addresses | `<host_N>` — consistent mapping within episode, reset per export batch |
| Hostnames | `<hostname_N>` |
| HA device / entity names | `<device_N>` |
| SSID / network names | `<network_N>` |
| File paths under `/config/` | Preserve structure, strip identifying filenames |
| Backup slugs | `<slug_N>` |

The raw (unanonymized) record stays in SQLite locally; anonymization applies only at export time.

---

### Dashboard: Episodes Tab

- List view: trigger, outcome, model used, escalated flag, duration, date
- Detail view: expand to see tool sequence and hypothesis chain
- Filter by trigger type and outcome
- "Prepare for submission" button — starts the Milestone 9 submission flow

---

### Done when

- Every successful `finish_repair` call writes a `RepairEpisode` to `repair_episodes`
- `--mode export-episodes` produces valid, anonymized YAML for all episodes since the given date
- Dashboard episodes tab renders correctly with filter controls
- Migration tested against real `ha_agent_state.db` before merging (solo project rule)
- `RepairEpisode` has the standard three Pydantic tests: valid construction, invalid fields, JSON round-trip
