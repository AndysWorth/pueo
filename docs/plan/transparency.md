# Spec: Transparent Operation (Milestone 11)

## Objective

Make Pueo's reasoning visible in real time. Users can always see what Pueo has done
(event timeline, repair episodes) and what it is currently thinking (live tool-call
trace in Chat). Transparency is Pueo's fourth design goal, alongside safety, privacy,
and autonomy.

## Background

Pueo today shows users the outcome of its reasoning — a finished assistant message in
Chat, a completed repair card, a timeline event — but not the reasoning process itself.
The Chat tab collapses all tool calls into a `<details>` element after the loop
completes. The event timeline records loop outcomes but not per-step progress. Users
have no way to follow along, spot a wrong assumption early, or give additional context
while Pueo is working.

## Design

Four independent delivery chunks. Chunk 1 is the foundation; Chunks 2–4 build on it
without depending on each other.

---

### Chunk 1 — Chat: real-time pre-call + post-call trace

**Goal:** When the user watches the Chat tab, they see Pueo's intent *before* a tool
call and the result *after*, rendered as inline trace cards.

**`utils/agent_loop.py`**

Add `pre_step_callback: Optional[Callable[[ToolCall], Awaitable[None]]]` to
`AgentLoop.__init__()` alongside the existing `step_callback`. Store as
`self._pre_step_callback`. Call it immediately before
`await self._executor.execute(tool_call)`:

```python
if self._pre_step_callback is not None:
    await self._pre_step_callback(tool_call)
tool_result = await self._executor.execute(tool_call)
```

The callback is `async` because `publish_chat_event` may eventually become async.
Existing callers pass `None` implicitly; no behavior change.

**`web/dashboard.py`**

In `_run_chat_loop()`, add:

```python
async def on_pre_step(tool_call: ToolCall) -> None:
    args_preview = _sanitize_args_preview(tool_call.arguments)
    publish_chat_event({
        "event_type": "chat_pre_step",
        "session_id": session_id,
        "tool": tool_call.name,
        "step": 0,           # step number not yet known; client tracks by order
        "args_preview": args_preview,
    })
```

Extend `on_step` to also emit a `chat_step_result` event:

```python
def on_step(step: AgentStep) -> None:
    # existing chat_thinking event
    publish_chat_event({...chat_thinking...})
    # new post-call result event
    output_summary = (step.tool_result.output or step.tool_result.error or "")[:300]
    publish_chat_event({
        "event_type": "chat_step_result",
        "session_id": session_id,
        "tool": step.tool_call.name,
        "step": step.step_number,
        "output_summary": output_summary,
        "success": step.tool_result.success,
    })
```

`_sanitize_args_preview(args: dict) -> str`: join key=value pairs, truncate to 120 chars,
redact values whose key contains `key`, `token`, `password`, `secret`.

Pass `pre_step_callback=on_pre_step` to `AgentLoop(...)`.

**`web/templates/chat.html`**

Replace the current collapsed `<details>` tool trace with inline expandable cards.

- `_pendingToolCalls` becomes `_pendingSteps: Map<int|string, {el, tool, argsPreview}>`.
- On `chat_pre_step`: create a `.tool-trace-card` with a spinner, the tool name, and the
  args preview; append it to the thread; store it in `_pendingSteps` keyed by tool name
  (or arrival order if name repeats).
- On `chat_step_result`: look up the matching card in `_pendingSteps`, replace the spinner
  with a `✓` (success) or `✗` (error) badge, append the output summary in a collapsible
  `<details>`. Mark the card done.
- On `chat_done`: all pending cards should already be resolved; clear `_pendingSteps`.
- `appendMessage()` for history: keep the existing `<details>` approach for loaded history
  (pre/post SSE events are gone after the session ends); Chunk 2 improves this.

**Tests (`tests/test_chat.py`)**

Add a test class `TestPreStepCallback`:
- Confirm `pre_step_callback` fires once before each tool execution.
- Confirm it fires before `step_callback` (check ordering using a shared event list).
- Use `FakeToolCallingLLMClient` producing two tool calls (`run_ha_command`,
  `finish_chat`) to exercise the sequence.

---

### Chunk 2 — Chat: durable tool context in history

**Goal:** Reopening a past session shows tool-call and tool-result rows, not just the
final assistant message.

**`web/dashboard.py`**

After `result = await agent_loop.run(...)`, before writing the final `assistant` row,
insert one `chat_messages` row per step:

```python
for step in result.steps:
    if step.tool_call.name == "finish_chat":
        continue  # final message stored separately
    tool_calls_json = json.dumps([step.tool_call.name])
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, tool_calls_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (session_id, "assistant", "", tool_calls_json, step.timestamp + loop_start_wall),
    )
    output = (step.tool_result.output or step.tool_result.error or "")[:500]
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, ts)"
        " VALUES (?, ?, ?, ?)",
        (session_id, "tool", output, step.timestamp + loop_start_wall),
    )
```

**`web/templates/chat.html`**

- Add a `<button id="btn-show-tools">Show tool calls</button>` toggle above the thread.
- `appendMessage()`: when `role === 'tool'`, create a `.tool-result-row` with class
  `hidden` by default; reveal when toggle is active.
- `appendMessage()` for assistant rows with `tool_calls_json`: render as a
  `.tool-trace-card.history` (no spinner, just the tool name + args).

**Tests (`tests/test_chat.py`)**

Add a test confirming that after a loop completes, `chat_messages` contains rows with
`role='assistant'` and `tool_calls_json` set, plus paired `role='tool'` rows, for each
non-terminal step.

---

### Chunk 3 — Autonomous repairs: live SSE trace + timeline

**Goal:** When Pueo runs an autonomous repair, the overview page shows the current step
live and the timeline records per-step rows.

**`utils/agent_loop.py`**

Add `timeline_callback: Optional[Callable[[str, str], Awaitable[None]]]` to
`AgentLoop.__init__()`. After each step (post-result), if set, call it with
`(tool_call.name, one_line_status)` where `one_line_status` is
`"step N — {tool_name}: OK"` or `"step N — {tool_name}: ERR"`.

**`web/dashboard.py`** (card-action executors)

When constructing `AgentLoop` for repair card approval, pass:

```python
async def on_timeline(tool_name: str, status_line: str) -> None:
    await write_timeline_event("INFO", "agent_loop", status_line)
    publish_event("agent_step", {"tool": tool_name, "status": status_line,
                                  "card_id": card_id})
```

**`web/templates/index.html`**

Add a `<div id="active-repair-widget" class="hidden">` in the loop health section.
Subscribe to the `/events` SSE stream for `agent_step` events. While events arrive, show
the current step live (tool name + step status). Hide the widget 3 seconds after a
`repair_done` or `repair_failed` event.

**`utils/repair_episode.py`**

`ToolCall` already has `name` and `arguments`. Extend the `tool_sequence` serialization
in `_record_episode()` to also store `result_summary` per step (truncated to 500 chars
from `tool_result.output`). Store in the existing JSON blob — no schema migration needed.

**Tests (`tests/test_chat.py` or `tests/test_core_agent.py`)**

Confirm `timeline_callback` fires once per step and receives the correct tool name.

---

### Chunk 4 — Chat guidance during active repair *(stretch)*

**Goal:** While an autonomous repair loop is running, the user can type into the Chat
tab to inject additional context (e.g. "the NAS is offline, ignore errors from it").

**`utils/agent_loop.py`**

Add `inject_context(message: str) -> None`: appends a `{"role": "user", "content": message}`
entry to `self._messages` (the live conversation list inside the running loop). The loop
picks it up on the next LLM call naturally.

**`web/dashboard.py`**

`POST /repair/inject` — finds the running `AgentLoop` instance held by `LoopSupervisor`
and calls `inject_context()`. Returns 409 if no repair loop is active.

**`web/templates/chat.html`**

When an `agent_step` SSE event arrives on `/events` (indicating an active repair), show
a banner: "Pueo is running a repair — you can send guidance below." The send button
routes to `/repair/inject` instead of `/chat/message` while the banner is active.

**Safety:** Injected messages are user-role messages in the conversation history. They
cannot bypass `AutonomyGate` or the backup invariant. The model may ignore irrelevant
context; the worst case is a slightly worse diagnosis.

---

## Implementation order

| Order | Item | Effort |
|-------|------|--------|
| 1 | Part 1: Documentation | Small |
| 2 | Chunk 1: Chat pre/post call trace | Medium |
| 3 | Chunk 2: Durable chat history | Small |
| 4 | Chunk 3: Autonomous repair SSE + timeline | Medium |
| 5 | Chunk 4: Chat guidance injection | Large (stretch) |

## Verification

See `docs/roadmap.md` Milestone 11 validation gate for end-to-end verification steps.

**CI gate** (all must pass before each PR):
```bash
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests,./.venv
pytest --cov --cov-fail-under=90 --ignore=tests/integration
```
