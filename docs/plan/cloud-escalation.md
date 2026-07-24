# HITL Cloud Escalation

Part of the [Roadmap](../roadmap.md) · Milestone 7.

---

### Problem

Local 7B models can follow a multi-step tool-calling loop and handle common failure patterns. Novel failure modes with complex multi-step reasoning exceed their reliable capability. When the tool loop exhausts its budget without a fix, the incident currently goes unresolved.

---

### Decision

When `AgentLoop` returns `outcome = "exhausted"` or `"timeout"`, surface a HITL dashboard card offering to escalate to Claude (Anthropic API). User approves per-incident; the same tool registry re-runs under Claude with the failed local loop's full step history included as context.

Cloud escalation is explicitly opt-in, user-approved, and per-incident. It does not affect the "0 WAN during autonomous fix cycles" constraint — escalation is a distinct mode initiated by human decision, not part of the autonomous cycle.

---

### Implementation

**`ClaudeAPIClient` (`utils/cloud_client.py`)**
- Implements `LLMClientProtocol` — same interface as `OllamaClient`
- Uses Anthropic Python SDK with prompt caching on the system prompt (reduces cost on repeated escalations from similar contexts)
- Tool adapter: maps Pueo's `ToolDefinition` Pydantic schemas to Anthropic tool-use JSON format
- See `/project:claude-api` skill for SDK patterns and caching setup

**Escalation HITL card fields:**
- Summary of what the local model tried (tool call sequence from `AgentLoopResult.steps`)
- Termination reason (`exhausted` / `timeout` / `fix_failed`) and step count
- Estimated cost (token count estimate × current Claude pricing, computed before API call)
- Scope: which tools Claude will have access to (same registry as local loop, including `query_knowledge` if Phase 14 is installed)
- Approve / Reject / Approve with budget cap override

**Billing guard:**

| Key | Default | Meaning |
|-----|---------|---------|
| `CLOUD_ESCALATION_ENABLED` | `false` | Must opt in explicitly in `config.yaml` |
| `CLOUD_MAX_COST_PER_INCIDENT_USD` | 0.50 | Hard cap per escalation; abort if estimate exceeds this |
| `CLOUD_MAX_DAILY_SPEND_USD` | 5.00 | Rolling 24-hour spend cap |
| `ANTHROPIC_API_KEY` | — | Read from environment variable only, never from `config.yaml` |

Daily spend is tracked in a new `cloud_spend` SQLite table. Resets at midnight local time.

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 46 | `ClaudeAPIClient` + tool adapter; `CLOUD_ESCALATION_ENABLED = false` default enforced at startup |
| 47 | Escalation HITL card: cost estimate, tool history summary, approve/reject with budget display |
| 48 | Cloud response pipeline: Claude's tool calls dispatched via the same Pueo tool execution layer as local calls |
| 49 | Billing guard: per-incident cap, daily rolling cap, `cloud_spend` SQLite table, midnight reset |

---

### Done when

- `CLOUD_ESCALATION_ENABLED = false` by default; no API calls without explicit config opt-in AND user approval of the HITL card
- Billing caps enforced; spend logged to `cloud_spend`; daily cap resets correctly
- Full tool call history from the failed local loop is passed to Claude as context
- `ClaudeAPIClient` has unit tests with `FakeLLMClient` and fake Anthropic responses; no real API calls in CI
- `ANTHROPIC_API_KEY` is never written to `config.yaml` or any committed file — startup raises if it appears in the config file
