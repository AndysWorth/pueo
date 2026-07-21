# Autonomy Control — Item 9.5

Part of the [Implementation Plan](../implementation-plan.md) · Phase 3.5 · 1 session.

**Prerequisite for all Phase 4 (NetAlertX) items.** Must be implemented before item 10.

---

### 9.5. Unified Autonomy Level ✅ Done (2026-07-19) — PR #17
**Problem:** Pueo's action gating is inconsistent and scattered. The HA sandbox engine has a hardcoded `requires_hitl()` check (CRITICAL severity or hacs/database keywords). Every NetAlertX item (10–19) would need its own ask/skip logic without a shared primitive. There is no single user-facing knob that uniformly controls how autonomous Pueo is across all subsystems.

#### Autonomy levels

| Level | Name | Behaviour |
|-------|------|-----------|
| 1 | Report Only | Observe, diagnose, and report. No file writes, no SSH state changes, no service restarts. Ask only for clarification needed to complete the report. |
| 2 | Suggest | Propose every action; require explicit HITL approval before any execution. Ask preference questions when the answer would change what Pueo recommends. |
| 3 | Guided | Auto-execute LOW-risk actions. Pause for approval on MEDIUM, HIGH, and CRITICAL. Ask preference questions only for non-trivial decisions. |
| 4 | Autonomous | Auto-execute LOW, MEDIUM, and HIGH-risk actions. Pause only for CRITICAL or when no fix applies. Preference questions are skipped; use documented safe defaults instead. |

#### Risk classification

| Risk | Examples |
|------|---------|
| LOW | Reading a file, locking a device name in NetAlertX, read-only API calls |
| MEDIUM | Rewriting a non-production config file (e.g., NetAlertX `app.conf`), writing to a sandbox path |
| HIGH | Writing to production HA `configuration.yaml`, restarting an add-on or container, calling `ha core reload` |
| CRITICAL | Removing a top-level block from production config, bulk irreversible operations, any action when backup slug is unavailable |

**Autonomy × Risk matrix** (✓ = auto-execute, ask = HITL approval required, skip = no action taken):

```
Risk →     | LOW  | MEDIUM | HIGH  | CRITICAL
Level 1    | skip | skip   | skip  | skip
Level 2    | ask  | ask    | ask   | ask
Level 3    | ✓    | ask    | ask   | ask
Level 4    | ✓    | ✓      | ✓     | ask
```

At level 1, `require_approval()` returns `False` immediately without sending a notification — the pipeline does not block. Structured log and notifier `send()` still fire so the finding is visible.

#### Preference questions

- **Level 1:** Ask when clarification is needed to produce an accurate report.
- **Level 2:** Ask when a genuine user preference exists and it would change the proposal.
- **Level 3:** Ask only for MEDIUM/HIGH-risk decisions where the right choice is not deterministic.
- **Level 4:** Only ask when completely stuck and the action is irreversible. Otherwise use safe defaults: default-route interface wins; HA name wins over auto-generated NetAlertX names; Mosquitto on the HA host is the MQTT broker; scan subnet derived from selected interface CIDR.

#### Build

**New file: `utils/autonomy.py`**

```python
class RiskLevel(IntEnum):
    LOW = 1; MEDIUM = 2; HIGH = 3; CRITICAL = 4

class AutonomyLevel(IntEnum):
    REPORT_ONLY = 1; SUGGEST = 2; GUIDED = 3; AUTONOMOUS = 4
```

`AutonomyGate` — single decision point imported by all Pueo modules:
- `gate.should_auto_execute(risk: RiskLevel) -> bool` — True if the current level permits executing at the given risk without asking.
- `gate.should_ask_preference(context: str) -> bool` — True if a preference question is appropriate at the current level.
- `async gate.require_approval(subject: str, body: str, payload: dict, notifier: NotifierProtocol, risk: RiskLevel) -> bool` — sends HITL notification and polls for `.approved`/`.rejected` up to `agent.hitl_timeout_minutes`; at level 1 returns False without notifying; at level 4 short-circuits to True for LOW/MEDIUM without notifying.

**New class: `FakeAutonomyGate`** — test double for `AutonomyGate`; configurable `auto_execute_result` and `approval_result` per risk level; exposes call counts for assertions. Lives in `utils/autonomy.py` alongside the real class.

**Config keys to add:**
- `agent.autonomy_level` (integer 1–4, default 2)
- ~~`agent.hitl_timeout_minutes`~~ *(Proposed but deleted in item 19.5 — `require_approval()` now polls indefinitely; see [hitl-dashboard.md](hitl-dashboard.md))*

**Deprecate `netalertx.mode`:** The `netalertx.mode` key (`diagnose|auto_fix|autonomous`) will not be added to the codebase. Add a shim in `config.py` that reads any existing `netalertx.mode` value in `config.yaml` and maps it to an integer level: `diagnose`→1, `auto_fix`→3, `autonomous`→4. Log a deprecation warning at startup if the key is set; users should migrate to `agent.autonomy_level`.

**Modify existing code:**
- `ha_agent_sandbox_engine.py` — replace `requires_hitl(report)` with `gate.require_approval(risk=HIGH, ...)` for production config writes; CRITICAL severity escalates to `risk=CRITICAL`.
- `ha_log_monitor.py` — wrap `trigger_remediation_pipeline()` with `gate.should_auto_execute(risk=HIGH)`; if False, send a notifier event and skip dispatch.
- All NetAlertX items (10–19) import `AutonomyGate` from `utils/autonomy.py`; no item may hard-code its own ask/skip logic.

**Done when:**
- `agent.autonomy_level = 1`: HA sandbox pipeline with a known-bad config produces structured log output and a notifier event but zero SSH writes — `FakeSSHClient.write_calls == []`.
- `agent.autonomy_level = 3`: LOW-risk action (device name lock) auto-proceeds; HIGH-risk (production config write) pauses for approval.
- `agent.autonomy_level = 4`: full HA repair pipeline runs end-to-end without HITL for WARNING severity; CRITICAL severity still pauses.
- `TestAutonomyGate` covers all 16 cells of the risk × level matrix for `should_auto_execute`; `should_ask_preference` returns correct values; `require_approval` short-circuits correctly at levels 1 and 4.
- `TestSandboxHITL` (existing) continues to pass after the `requires_hitl()` refactor.
