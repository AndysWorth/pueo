# Self-Improving Code Proposals  *(stretch goal)*

Part of the [Roadmap](../roadmap.md) · Milestone 10.

---

### Problem

When Pueo encounters a failure mode for which it has no tool, a human engineer must write new Python code and open a PR manually. This milestone closes that loop: the agent identifies the gap, proposes the code, validates it against CI in a sandbox, and surfaces a HITL approval card to open the PR. Approved changes become reusable capabilities for every future incident of that type.

This is a stretch goal. It does not block any other milestone. Implement when Milestones 7 and 9 are complete (cloud model quality + community context both improve proposal quality significantly).

---

### New Tools (added to registry in Phase 18)

| Tool | Description |
|------|-------------|
| `read_source` | Read Pueo source files and recent `git log` as grounding context |
| `propose_patch` | Generate a unified diff against the current codebase |
| `sandbox_code` | Apply patch to a temp directory, run `pytest --tb=short` in a subprocess with no network access and a 60s timeout |
| `open_pr` | `gh pr create` with the diff and CI results in the PR body — fires only after explicit HITL approval |

---

### Safety Constraints

**Sandbox:**
- `sandbox_code` subprocess: no network access, 60-second wall timeout, temp directory cleaned up unconditionally in `finally`
- Proposed diff must pass `flake8` and `mypy --ignore-missing-imports` before `sandbox_code` is called

**Write gates:**
- `open_pr` requires explicit HITL approval — never auto-fires, regardless of autonomy level
- Agent may not modify `utils/autonomy.py`, `interfaces.py`, or `config.py` without an additional confirmation step beyond the standard autonomy gate (these files are safety-critical)
- Any diff that touches `execute_remote_backup()`, the backup invariant chain, or the autonomy gate is blocked automatically and cannot be proposed without a mandatory security review step

**Scope limits:**
- `read_source` is read-only; no write access to the working tree outside the sandbox temp dir
- `propose_patch` output is applied only in the sandbox — never to the live working tree until `open_pr` is approved and the PR merges via normal git flow

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 56 | `read_source` + `propose_patch` tools; diff generation prompt engineering |
| 57 | `sandbox_code` tool: subprocess sandbox with no-network isolation, pytest runner, lint gate |
| 58 | Code proposal HITL card: diff viewer in dashboard, test output, approve/reject |
| 59 | `open_pr` tool: `gh pr create` integration, PR body template (diff + test summary + ADR reference) |
| 60 | Security review: sandbox escape vectors, allowlist enforcement, safety-critical file block list |
| 61 | ADR 007: Agent-generated code proposals with sandboxed CI gate |

---

### Done when

- Agent proposes a new tool in response to a synthetic capability-gap scenario
- `sandbox_code` runs CI against the patch and reports pass/fail + output
- HITL card shows rendered diff + test results; user approves → PR opens
- Safety-critical file block list tested with a deliberate attempt to patch `utils/autonomy.py`
- Security review complete; no sandbox escape paths identified
- ADR 007 committed
