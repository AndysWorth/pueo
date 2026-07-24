# Tool-Calling Agent Loop

Part of the [Roadmap](../roadmap.md) · Milestone 6.

---

### Problem

Pueo's linear pipeline — gather pre-scripted evidence → one Ollama call → fixed action set — can only handle failure modes the designer anticipated. Unknown problems either go undiagnosed or produce hallucinated fixes applied to a scripted template. The agent needs to investigate freely, the same way a human engineer would: observe something, form a hypothesis, gather more evidence, revise.

---

### Decision

Replace the linear pipeline with an iterative agent loop using Ollama's `tools` API. The model decides which tools to call based on what it observes, iterates until it reaches a confident diagnosis and fix, or exhausts its budget.

See [ADR 006](../decisions/006-tool-calling-loop.md) for the architectural rationale.

---

### Tool Registry (`utils/tool_registry.py`)

All tools are defined as Pydantic schemas. The registry produces Ollama-compatible JSON tool schemas on demand. The same registry is used by `OllamaClient` (local) and `ClaudeAPIClient` (cloud escalation, Milestone 7) via the `LLMClientProtocol` interface.

**Initial tool set:**

| Tool | Description |
|------|-------------|
| `read_config` | SFTP fetch of HA config file(s) by remote path |
| `read_logs` | SSH tail of N lines from HA supervisor journal |
| `run_ha_command` | Run an allowlisted `ha` CLI subcommand and return stdout |
| `read_file` | Read an arbitrary remote file (allowlisted directories only) |
| `query_netalertx` | NetAlertX health / device / event data via API |
| `apply_fix` | Write proposed YAML to sandbox, validate, atomic swap — backup-first, always |
| `verify_fix` | Run `ha core check` post-swap and return pass/fail |
| `finish_repair` | Stop token — signals diagnosis complete; returns structured episode stub |

**`apply_fix` constraints:**
- Internally calls `execute_remote_backup()` before any write — safety invariant unchanged, lives inside the tool
- May be called at most once per loop run (prevents thrashing)
- Calls `AutonomyGate.require_approval()` before the backup trigger — HITL gate position unchanged

**`run_ha_command` allowlist (initial):**
`ha core check`, `ha core restart`, `ha core stop`, `ha host info`, `ha backups list`, `ha backups new`, `ha apps list`, `ha os info`

---

### Agent Loop Controller (`utils/agent_loop.py`)

```
AgentLoop.run(initial_context: str) -> AgentLoopResult
```

**Budget (config keys):**

| Key | Default | Meaning |
|-----|---------|---------|
| `AGENT_MAX_TOOL_CALLS` | 20 | Hard cap on tool invocations per run |
| `AGENT_MAX_WALL_SECONDS` | 120 | Wall-clock timeout for the entire loop |

**Termination conditions:**

| Outcome | Trigger |
|---------|---------|
| `success` | Model calls `finish_repair` |
| `exhausted` | Tool call budget reached without `finish_repair` |
| `timeout` | Wall time exceeded |
| `fix_failed` | `apply_fix` + `verify_fix` returned failure |

Both `exhausted` and `timeout` surface an escalation offer in the HITL dashboard when Milestone 7 is installed.

**Result type:**
```python
AgentLoopResult(
    outcome: Literal["success", "exhausted", "timeout", "fix_failed"],
    steps: list[AgentStep],       # full tool call/result history
    episode_stub: dict | None,    # populated on success, feeds Milestone 8
)
```

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 35 | Tool registry + Pydantic schemas: `ToolDefinition`, `ToolCall`, `ToolResult`, `AgentStep` |
| 36 | Tool execution implementations for all tools in the table above |
| 37 | `AgentLoop` controller: budget accounting, tool dispatch, termination detection |
| 38 | HA sandbox engine refactor: replace linear pipeline with `AgentLoop.run()` |
| 39 | NetAlertX healer refactor: same |
| 40 | Safety audit: backup invariant in `apply_fix`; `run_ha_command` allowlist; `apply_fix` once-per-loop |
| 41 | Eval regression check: run `evals/run_evals.py` against refactored pipeline; score must not drop vs item-33 baseline |

---

### Safety Notes

- `run_ha_command` enforces an explicit allowlist — no arbitrary shell execution
- `read_file` enforces an allowlist of permitted remote directories (`/config/`, `/backup/`)
- The backup invariant lives inside `apply_fix`, not at the loop level — the loop cannot bypass it
- `AutonomyGate.require_approval()` is called inside `apply_fix` before the backup trigger, matching current ordering in `ha_agent_sandbox_engine.py`
- The `query_knowledge` tool (Milestone 2 / Phase 14) is registered here but implemented later; the registry slot is reserved from item 35

---

### Done when

- `AgentLoop.run()` replaces the linear pipeline in `ha_agent_sandbox_engine.py` and `netalertx/healer.py`
- All tools are unit-tested with `FakeSSHClient` / `FakeLLMClient`; no real SSH or Ollama calls in the unit suite
- `apply_fix` still enforces backup-first; safety audit signed off
- Eval regression check passes (score does not drop vs M5 baseline)
- ADR 006 committed
