# ADR 007 — Agent-generated code proposals with sandboxed CI gate

## Status
Accepted

## Context

Pueo's tool-calling loop (ADR 006) can diagnose and repair known failure modes by calling
tools already in the registry. When the agent encounters a failure mode that no existing tool
can address — an integration type it has never seen, a new HAOS supervision API, a
site-specific hardware fault — it has two options: escalate to a human engineer who then
writes code manually, or declare the incident unresolved.

Both options leave the gap open for every future occurrence. Milestone 10 adds a third path:
the agent identifies the gap, writes the code, validates it against CI, and surfaces an approval card
approval card to open a PR. Approved changes become permanent capabilities available to every
future repair cycle.

The sandbox infrastructure (`read_source`, `propose_patch`, `sandbox_code`,
`CARD_TYPE_CODE_PROPOSAL`) was first delivered in Phase 17.5 (Milestone 6.6) for the
conversational agent's code-skill-building feature. Phase 21 adds the autonomous trigger (gap
detection in `finish_repair`) and the `open_pr` path (formal PR instead of in-process
session-local registration).

## Decision

When the agent calls `finish_repair(capability_gap=True, gap_description="...")`, Pueo
treats the outcome as a proposal trigger. The `AgentLoopResult` surfaces `capability_gap=True`
to the caller, which re-enters a code-proposal loop using the same `AgentLoop` controller and
`ToolExecutor` with the sandbox tool set registered.

The proposal flow is a linear, gated sequence:

1. **`read_source`** — agent reads relevant source files from the live working tree (read-only;
   confined to `_REPO_ROOT`; 8 000-character cap per file)
2. **`propose_patch`** — agent stages a replacement file content in memory; not written to disk
   yet; safety-critical file block list and backup invariant symbol check applied here
3. **`sandbox_code`** — Pueo copies the repo to a temp directory, applies the staged patch,
   and runs the full CI gate (`black`, `flake8`, `mypy`, `pytest`); temp dir cleaned up
   unconditionally in `finally` regardless of outcome
4. **`open_pr`** (or **`add_tool`** for session-local registration) — only callable after
   `sandbox_code` succeeds; always queues a approval card; never auto-fires

`open_pr` approval causes Pueo to create a feature branch and execute `gh pr create` with a
body that includes the full diff, CI output, and a reference to this ADR.

## Safety constraints

**Block list enforced at the tool layer.** `propose_patch` rejects patches targeting
`utils/autonomy.py`, `interfaces.py`, and `config.py` unconditionally. These files govern the
autonomy gate, protocol interfaces, and the single configuration source. A bad change to any
of them can disable safety controls silently and is not recoverable by reverting a feature PR.

**Backup invariant chain protected by symbol detection.** Any patch content that introduces a
new definition of `execute_remote_backup` or `record_backup_slug` is rejected. Calling these
functions in a new tool is permitted; redefining them would silently duplicate or bypass the
invariant, which is not detectable at runtime until a write fails to back up.

**Path traversal confined.** `_resolve_repo_path` resolves the requested path relative to
`_REPO_ROOT` and rejects any path that resolves outside it (symlink traversal, `..`
components, absolute escapes). `read_source` and `propose_patch` both go through this resolver.

**Sandbox isolation.** `sandbox_code` runs CI tools as subprocesses with fixed argument lists
— no `shell=True`, no user-supplied shell fragments, no network access. The temp directory is
outside the live working tree; even a CI tool that writes files cannot contaminate the repo.

**approval gate is unconditional.** `open_pr` always queues a notification and waits for human
approval before calling `gh pr create`. This is hard-coded in `_open_pr`, not gated through
`AutonomyGate` — raising the autonomy level cannot bypass it. The same applies to `add_tool`.

**CI must pass before the approval card fires.** `open_pr` and `add_tool` both check
`self._sandbox_passed` and return a tool error if the sandbox has not been run or did not
pass. The agent cannot queue an approval card for a patch that does not compile or fails
existing tests.

## Key design choices

**Same `AgentLoop`, same `ToolExecutor`.** No separate agent or orchestrator is introduced for
the proposal flow. The code-proposal tools (`read_source`, `propose_patch`, `sandbox_code`,
`open_pr`) are registered alongside the repair tools. This reuses the budget accounting,
timeout enforcement, structured LLM output, and client injection already in place.

**Two registration paths for two durations.** `add_tool` registers a tool in-process for the
current session only — useful for chat experiments and one-off tasks. `open_pr` creates a
persistent PR that, once merged, becomes part of the checked-in tool registry for all future
sessions. Both require sandbox pass and approval; only the persistence scope differs.

**`sandbox_code` runs the full CI gate, not just syntax checking.** Parsing and import
checking catch obvious errors; the full gate (`black` + `flake8` + `mypy` + `pytest`) catches
type regressions, logic errors that break existing tests, and style violations that would fail
CI on the PR. The sandbox output (truncated to 3 000 characters) is included in the approval card
so the reviewer can see exactly what passed.

**Gap detection is a `finish_repair` field, not a separate tool call.** The agent signals a
capability gap by including `capability_gap=True` in its `finish_repair` arguments rather than
by calling a dedicated `report_gap` tool. This keeps the terminal tool count at one per loop
(the loop budget is not consumed by a separate gap-reporting step) and makes the gap signal
part of the structured `AgentLoopResult` that callers already handle.

## Consequences

- The agent can propose code that extends its own capabilities, subject to CI pass and human
  approval. The human remains the last gate before any code reaches the repo.
- The safety-critical block list creates a permanent asymmetry: the agent can extend the tool
  registry but cannot modify the controls that govern when tools are called or how
  configuration is loaded. These files remain human-edit-only.
- `sandbox_code` runs `pytest` over the full unit suite in a subprocess on every proposal.
  On machines with a slow test suite, this can take 30–60 seconds. The 60-second timeout is
  a hard cap; a proposal that requires a test run exceeding the budget is rejected even if the
  code itself is correct.
- PRs opened by `open_pr` include a reference to this ADR in the body. Reviewers who see an
  agent-opened PR can follow the reference to understand the approval chain and CI evidence.
- `add_tool` (session-local) and `open_pr` (persistent PR) are complementary, not mutually
  exclusive. A tool prototyped via `add_tool` in a chat session can later be formalized with
  `open_pr` when the user is satisfied with its behavior.

## Related decisions

- [ADR 001 — Config centralization](001-config-centralization.md): `config.py` is on the
  safety-critical block list precisely because it is the single source of all settings; a bad
  agent-generated change there would affect every subsystem at import time.
- [ADR 002 — Safety invariant](002-safety-invariant.md): The backup invariant chain
  (`execute_remote_backup`, `record_backup_slug`) is protected by symbol-level detection in
  `propose_patch` — the same principle as the invariant itself: the protection is enforced
  before the action, not after.
- [ADR 006 (tool-calling loop)](006-tool-calling-loop.md): Code-proposal tools share the
  `AgentLoop` + `ToolExecutor` architecture introduced there. Budget limits, timeout
  enforcement, and client injection apply unchanged to the proposal flow.
- [ADR 006 (LLM provider)](006-llm-provider-abstraction.md): `ClaudeAPIClient` and
  `OllamaClient` are both supported in the proposal loop. Higher-quality cloud models produce
  better patch proposals; `LLM_PROVIDER=both` routes the proposal loop to Claude when the
  user approves the cloud escalation, consistent with the `both`-mode design.
