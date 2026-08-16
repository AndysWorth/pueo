# Self-Improving Code Proposals  *(stretch goal)*

Part of the [Roadmap](../roadmap.md) · Milestone 10.

---

### Problem

When Pueo encounters a failure mode for which it has no tool, a human engineer must write new Python code and open a PR manually. This milestone closes that loop: the agent identifies the gap, proposes the code, validates it against CI in a sandbox, and surfaces a approval card to open the PR. Approved changes become reusable capabilities for every future incident of that type.

This is a stretch goal. It does not block any other milestone. Implement when Milestones 7 and 9 are complete (cloud model quality + community context both improve proposal quality significantly).

---

### Foundation in Phase 17.5

The sandbox tools and the code-proposal approval card were delivered in **Phase 17.5 — Conversational Agent** (items 65–72) as part of the chat code-skill-building feature:

| Tool | Delivered in |
|------|--------------|
| `read_source` | Phase 17.5, item 70 |
| `propose_patch` | Phase 17.5, item 70 |
| `sandbox_code` | Phase 17.5, item 70 |
| Code proposal approval card (`CARD_TYPE_CODE_PROPOSAL`) | Phase 17.5, item 71 |
| `add_tool` (in-process registration) | Phase 17.5, item 71 |

Phase 21 adds only the remaining pieces: the `open_pr` path (formal PR instead of in-process registration), the autonomous trigger, and the security/ADR deliverables.

---

### Safety Constraints

**Sandbox:**
- `sandbox_code` subprocess: 60-second wall timeout, temp directory cleaned up unconditionally in `finally`
- Proposed diff must pass `black`, `flake8`, `mypy`, and `pytest` in the sandbox before the approval card fires

**Write gates:**
- `open_pr` requires explicit approval — never auto-fires, regardless of autonomy level
- Agent may not modify `utils/autonomy.py`, `interfaces.py`, or `config.py` without an additional confirmation step (safety-critical block list implemented in item 85)
- Any diff touching `execute_remote_backup()`, the backup invariant chain, or the autonomy gate is blocked and requires a mandatory security review step

**Scope limits:**
- `read_source` is read-only; no write access to the live working tree outside the sandbox temp dir
- `propose_patch` output is applied only in the sandbox — never to the live working tree until approval

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 83 | `open_pr` tool: `gh pr create` integration; builds on `propose_patch` + `sandbox_code` from Phase 17.5 item 70; PR body template includes diff + test summary + ADR 007 ref |
| 84 | Autonomous gap detection: `finish_repair` called with `capability_gap=True` automatically triggers `propose_patch → sandbox_code → code_proposal` approval card; both HA and NetAlertX loops |
| 85 | Security review: sandbox escape vectors, safety-critical file block list, `read_source` path traversal test |
| 86 | ADR 007: Agent-generated code proposals with sandboxed CI gate |

---

### Done when

- `finish_repair` with `capability_gap=True` (synthetic scenario) triggers the proposal flow automatically
- `open_pr` fires after approval and opens a real PR via `gh pr create`
- Safety-critical block list (`utils/autonomy.py`, `interfaces.py`, `config.py`, backup invariant chain) blocks deliberate patch attempts
- Security review complete; no sandbox escape paths identified
- ADR 007 committed
