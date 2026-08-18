# ADR 009 — Transparency as a first-class design goal

## Status
Accepted

## Context
Pueo makes autonomous decisions that affect a live smart home — writing configuration files, restarting the HA core, executing update commands, and managing backups. These actions are gated by the backup invariant and the autonomy gate, but users have no visibility into *what* Pueo is doing while it works. The Chat tab shows a collapsed "N tool calls" summary after completion. The event timeline records loop outcomes but not per-step progress during autonomous repairs.

Three implicit goals already shape the codebase: **safety** (backup-before-write invariant), **privacy** (local inference by default), and **autonomy** (self-healing without manual intervention). A fourth goal — **transparency** — was missing from both documentation and implementation.

## Decision
Transparency is a first-class design goal. Every agent step must be observable:

1. **Chat sessions:** The Chat tab shows Pueo's intent *before* a tool call and the result *after* — not just a collapsed summary. Pre-call events carry tool name and sanitized arguments; post-call events carry output summary and success flag.
2. **Autonomous repairs:** The overview page shows a live "Active repair" widget while a repair loop is running. The event timeline records a per-step row for each tool call, not just the final outcome.
3. **Durable history:** Past chat sessions preserve tool-call and tool-result rows in `chat_messages` so the trace is available on reload, not only during the live session.
4. **Guidance injection (stretch):** While an autonomous repair is running, the user can type context into the Chat tab and the running loop incorporates it on the next LLM call.

## Implementation

`AgentLoop` gains two optional callback parameters:
- `pre_step_callback: Optional[Callable[[ToolCall], Awaitable[None]]]` — fires immediately before `await self._executor.execute(tool_call)`.
- The existing `step_callback` (post-call, carries `AgentStep`) is unchanged.

`dashboard.py` wires `pre_step_callback` in `_run_chat_loop()` to emit `chat_pre_step` SSE events. The existing `step_callback` (`on_step`) is extended to also emit a `chat_step_result` event with output summary and success flag.

`chat.html` replaces the collapsed `<details>` tool trace with inline expandable cards: a spinner appears on `chat_pre_step`; the matching `chat_step_result` replaces the spinner with a ✓ or ✗ badge and the output summary.

Autonomous repair loops (card-action executors in `dashboard.py`) pass a `timeline_callback` to `AgentLoop` that writes a timeline event and publishes an `agent_step` SSE event per step.

## Consequences
- `AgentLoop.__init__` gains `pre_step_callback` parameter. All existing callers pass `None` implicitly — no behavior change for callers that do not opt in.
- Arguments passed to `pre_step_callback` must be sanitized: no credential values, no raw SSH private key paths. The callback receives a truncated string preview of arguments, not the raw dict.
- `chat_messages` rows for tool calls and tool results (role=`assistant` with `tool_calls_json`, role=`tool` with output) are stored after every loop completion. The UI filters them by default and shows them only when the user toggles "Show tool calls".
- `agent_step` SSE events on the general bus are ignored by clients that do not subscribe to them — no change to existing SSE consumers.

## Related decisions
- [ADR 005 — asyncio over agentic framework](005-asyncio-over-agentic-framework.md): The callback hooks are simple `asyncio`-compatible callables; no framework is needed.
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): Transparency applies to both `OllamaClient` and `ClaudeAPIClient` paths — the callbacks fire at the `AgentLoop` level, above the provider boundary.
