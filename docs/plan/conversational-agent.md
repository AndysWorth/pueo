# Conversational Agent

Part of the [Roadmap](../roadmap.md) · Milestone 4.8.

---

### Problem

Pueo operates purely reactively today — monitoring loops fire, the agent loop runs, and the user approves or rejects queued cards. There is no way to ask Pueo a direct question, tell it to remember something across sessions, or interactively build new capabilities. This phase adds a Chat tab to the dashboard so the user can talk to Pueo, query live HA state conversationally, store and recall persistent notes, and propose new tools through a sandboxed code flow.

This phase also implements the shared sandbox infrastructure (`read_source`, `propose_patch`, `sandbox_code`) that Phase 21 (Milestone 10) will reuse for its autonomous code-proposal path.

---

### Architecture

**AgentLoop reuse.** The same `AgentLoop.run(initial_context)` drives the conversational agent. It runs with a different system prompt (conversational, not repair-focused), a different `ToolRegistry` (no `apply_fix`/`verify_fix`; adds `remember`/`recall`/`finish_chat`), and `terminal_tool_name="finish_chat"`. No new loop controller is needed.

**Memory: SQLite keyword search.** `recall(query)` does case-insensitive substring matching over `agent_memory`. No embeddings — keyword search is fast, zero-overhead, and testable without Ollama running. The existing ChromaDB infrastructure could replace it in a later enhancement.

**Conversation persistence.** Two SQLite tables — `chat_sessions` and `chat_messages` — store the thread. On each `POST /chat/message`, prior messages are replayed into the LLM context to provide multi-turn continuity.

**SSE for progress visibility.** Since `chat_with_tools` returns a complete response per iteration (not streamed tokens), the chat SSE stream delivers tool-boundary events (`chat_thinking` on each tool call, `chat_done` on loop completion, `chat_error` on failure). The browser shows tool calls in real time as Pueo reasons, then renders the final answer.

**Dedicated `/chat/events` SSE stream.** Chat events are isolated from the global `/events` stream (which carries repair/resource/timeline noise) via a module-level `_chat_subscribers` fan-out list in `utils/supervisor.py` — same pattern as the existing `_sse_subscribers`.

**Code sandbox.** `propose_patch` stores a pending change in `ToolExecutor._pending_patch` (not applied to the live tree). `sandbox_code` copies the repo to a temp directory, applies the patch, and runs the full CI gate (`black`, `flake8`, `mypy`, `pytest`) in a subprocess with a 60-second timeout. The temp directory is cleaned up unconditionally in `finally`.

**Dynamic tool registration.** `add_tool` writes approved code to `user_tools/<name>.py`, imports it, and registers it in `ToolExecutor._dynamic_tools` so future loops (both chat and repair) can call it. Persisted in `registered_tools` SQLite table; loaded at startup. Protected by `CHAT_ALLOW_TOOL_REGISTRATION = false` default and a mandatory approval card (`code_proposal` card type).

**`AgentLoop.terminal_tool_name`.** A new `terminal_tool_name: str = "finish_repair"` parameter on `AgentLoop.__init__` makes the loop termination signal configurable. Chat loops pass `"finish_chat"`. The existing repair loops use the default — no change to existing behavior.

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 65 | DB migration v8: `agent_memory`, `chat_sessions`, `chat_messages` tables |
| 66 | `remember` + `recall` tools: ToolDefinitions, ToolExecutor methods, `CHAT_MEMORY_TOP_K` + `CHAT_ALLOW_TOOL_REGISTRATION` config keys |
| 67 | Chat tool registry (`build_chat_tool_registry`), `finish_chat` ToolDefinition, `AgentLoop.terminal_tool_name` parameter, conversational system prompt |
| 68 | `/chat` GET route, `chat.html` template (session list + message thread + input), `base.html` nav link |
| 69 | `POST /chat/message` + `GET /chat/events` SSE; `asyncio.create_task` loop dispatch; `chat_thinking`/`chat_done`/`chat_error` events |
| 70 | `read_source`, `propose_patch`, `sandbox_code` tools: ToolDefinitions + ToolExecutor methods; subprocess CI gate; 60s timeout |
| 71 | `add_tool` registration: migration v9 (`registered_tools`), `ToolExecutor._dynamic_tools`, `CARD_TYPE_CODE_PROPOSAL`, dashboard approval handler, `user_tools/` loader on startup |
| 72 | Tests: `test_chat.py` (migrations v8+v9, remember/recall, chat registry, sandbox_code, read_source, add_tool); `TestConfigDefaults` entries for two new config keys |

---

### Item 65 — DB migration v8

**Files:** `ha_agent_advanced.py`

Add `_migrate_v8(cursor)` and `(8, _migrate_v8)` to `_MIGRATIONS`:

```sql
CREATE TABLE IF NOT EXISTS agent_memory (
    id      INTEGER PRIMARY KEY,
    key     TEXT NOT NULL,
    content TEXT NOT NULL,
    source  TEXT NOT NULL,   -- 'user' or 'agent'
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id         INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    title      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES chat_sessions(id),
    role            TEXT NOT NULL,       -- 'user' | 'assistant' | 'tool'
    content         TEXT NOT NULL,
    tool_calls_json TEXT,
    ts              REAL NOT NULL
);
```

Reuse: `_migrate_v7` pattern; `init_local_database()` runs all pending migrations automatically.

---

### Item 66 — remember + recall tools

**Files:** `config.py`, `config.yaml.default`, `setup.sh`, `utils/tool_executor.py`, `utils/tool_registry.py`

**New config keys** (under `agent:` in YAML):

| Attr | YAML key | Default |
|------|----------|---------|
| `CHAT_MEMORY_TOP_K` | `chat_memory_top_k` | `10` |
| `CHAT_ALLOW_TOOL_REGISTRATION` | `chat_allow_tool_registration` | `false` |

`setup.sh` prompts for `CHAT_ALLOW_TOOL_REGISTRATION` with a `false` default and a clear warning that enabling this allows the agent to write and register arbitrary Python code.

**`ToolExecutor.__init__`** gains a `db_path: str = DB_PATH` parameter stored as `self._db_path`. Used only by remember/recall; other tools are unaffected.

**`_remember(key, content, source)`** — `INSERT INTO agent_memory` with `ts = time.time()`. Returns `ToolResult(success=True, output=f"Remembered: {key}")`.

**`_recall(query)`** — `SELECT * FROM agent_memory WHERE content LIKE ? OR key LIKE ? ORDER BY ts DESC LIMIT ?` with `f"%{query}%"`. Returns formatted lines or "Nothing found."

**`ToolDefinition` constants** for `remember` (args: `key: str, content: str`) and `recall` (args: `query: str`) added to `utils/tool_registry.py` and registered by `build_chat_tool_registry()` in item 67.

---

### Item 67 — Chat tool registry + AgentLoop.terminal_tool_name

**Files:** `utils/tool_registry.py`, `utils/agent_loop.py`

**`AgentLoop`:** Add `terminal_tool_name: str = "finish_repair"` to `__init__`; store as `self._terminal_tool_name`. Replace the hardcoded `"finish_repair"` string check in `_loop_body` with `self._terminal_tool_name`. All existing tests pass unchanged because the default matches the old hardcoded value.

**`finish_chat` ToolDefinition** — args: `summary: str`. No `action_taken` field (conversational, not repair-oriented).

**`build_chat_tool_registry() -> ToolRegistry`** — registers: `read_config`, `read_logs`, `run_ha_command`, `read_file`, `query_knowledge`, `query_netalertx`, `remember`, `recall`, `read_source`, `propose_patch`, `sandbox_code`, `add_tool`, `finish_chat`. Does **not** include `apply_fix` or `verify_fix`.

**`_CHAT_SYSTEM_PROMPT`** constant (in `utils/agent_loop.py`):

```
You are Pueo, a Home Assistant assistant. Use tools to look up live state,
answer questions, and investigate problems. Use remember/recall to store and
retrieve context across sessions. Use read_source, propose_patch, sandbox_code,
and add_tool to build new capabilities when the user asks. Always end by calling
finish_chat with a plain-language summary of what you found or did. Never return
plain text — always call a tool.
```

Chat `AgentLoop` is constructed with `system_prompt=_CHAT_SYSTEM_PROMPT` and `terminal_tool_name="finish_chat"`.

---

### Item 68 — /chat UI

**Files:** `web/dashboard.py`, `web/templates/chat.html` (new), `web/templates/base.html`

**Routes added to `dashboard.py`:**
- `GET /chat` → renders `chat.html`
- `GET /chat/sessions` → JSON list of sessions `[{id, created_at, title, message_count}]`
- `DELETE /chat/sessions/{id}` → deletes session + messages, returns 204

**`base.html`:** Add `<a class="nav-link{% if p.startswith('/chat') %} active{% endif %}" href="/chat">Chat</a>` to the nav bar.

**`chat.html` layout** (extends `base.html`):
- Left sidebar: session list (`GET /chat/sessions`), "New chat" button, per-session delete button
- Main panel: scrollable message thread — user messages right-aligned, Pueo messages left-aligned; collapsible tool-call trace per message (shows `chat_thinking` steps in italics)
- Footer: `<textarea>` + Send button
- JS: `EventSource('/chat/events')` for real-time progress; `fetch('/chat/message', {method:'POST', body: JSON.stringify({session_id, message})})` to send

---

### Item 69 — POST /chat/message + /chat/events SSE

**Files:** `web/dashboard.py`, `utils/supervisor.py`

**`utils/supervisor.py`:** Add `_chat_subscribers: list[asyncio.Queue]` module-level list and `subscribe_chat()`/`unsubscribe_chat()` functions mirroring the existing `subscribe()`/`unsubscribe()`. Add `publish_chat_event(event: dict)` that puts to all chat subscriber queues (non-blocking, drops if full).

**`GET /chat/events`:** Same `StreamingResponse` + `subscribe_chat()`/`unsubscribe_chat()` pattern as `/events`. Emits only `event_type` values starting with `"chat_"`.

**`POST /chat/message`** (body: `{session_id: int | null, message: str}`):
1. Create session row if `session_id` is null; set title from first 60 chars of message
2. Persist user message to `chat_messages`
3. Load all prior `chat_messages` rows for the session (ordered by `ts ASC`)
4. `asyncio.create_task(_run_chat_loop(session_id, message, history))`
5. Return `JSONResponse({"session_id": session_id}, status_code=202)`

**`_run_chat_loop(session_id, message, history)`** (async coroutine):
1. Reconstruct the supervisor's shared `ToolExecutor` (via `get_supervisor_instance()._tool_executor` if available; fall back to creating one with `FakeSSHClient` equivalent in standalone dashboard mode — chat tools that don't need SSH will still work)
2. Build `AgentLoop` with chat registry, `_CHAT_SYSTEM_PROMPT`, `terminal_tool_name="finish_chat"`, shorter budget (`max_tool_calls=10`, `max_wall_seconds=60`) for interactive UX
3. Reconstruct message history as `[{"role": r, "content": c} ...]` and prepend to `initial_context`
4. On each tool call start: `publish_chat_event({"event_type": "chat_thinking", "session_id": session_id, "tool": tool_name})`
5. On completion: persist Pueo's summary to `chat_messages` (role=`"assistant"`); `publish_chat_event({"event_type": "chat_done", "session_id": session_id, "content": summary})`
6. On exception: `publish_chat_event({"event_type": "chat_error", "session_id": session_id, "error": str(exc)})`

The `AgentLoop` does not natively emit per-tool events. Item 69 adds an optional `step_callback: Callable[[AgentStep], None] | None = None` parameter to `AgentLoop.__init__`; it is called after each step in `_loop_body`. This avoids polling and keeps the callback decoupled.

---

### Item 70 — Code sandbox tools

**Files:** `utils/tool_executor.py`, `utils/tool_registry.py`

**`read_source(path: str)`:**
- `_REPO_ROOT = Path(__file__).parent.parent` — the Pueo repository root
- Resolve `path` relative to `_REPO_ROOT`; reject traversal outside it
- Allow only `.py`, `.yaml`, `.md`, `.toml`, `.txt` extensions
- Read and return up to 8000 characters (token budget)

**`propose_patch(path: str, content: str)`:**
- Validate `path` against same restrictions as `read_source`
- Store `(path, content)` in `self._pending_patch: dict[str, str]` (replaces any prior pending patch for the same path; only one patch pending at a time)
- Return confirmation string

**`sandbox_code(description: str)`:**
- Raise if `_pending_patch` is empty
- `tmpdir = tempfile.mkdtemp(prefix="pueo_sandbox_")`
- `shutil.copytree(_REPO_ROOT, tmpdir, dirs_exist_ok=True)`
- Write patched file(s) from `_pending_patch` into `tmpdir`
- Run sequentially in subprocess (60s `asyncio.wait_for` timeout each):
  ```
  black --check <patched_file>
  flake8 --count --select=E9,F63,F7,F82 --show-source --statistics <patched_file>
  mypy --ignore-missing-imports <patched_file>
  pytest tests/ --tb=short --ignore=tests/integration -x -q
  ```
- Collect combined stdout/stderr; truncate to 3000 chars
- Clean up `tmpdir` in `finally`
- Return `ToolResult(success=all_passed, output=combined_output)`

Each subprocess call uses `asyncio.to_thread(subprocess.run, ..., capture_output=True, text=True, timeout=60)`.

**ToolDefinitions** for all three tools added to `utils/tool_registry.py`; registered in `build_chat_tool_registry()`.

---

### Item 71 — add_tool registration + code_proposal approval card

**Files:** `ha_agent_advanced.py`, `utils/tool_executor.py`, `utils/card_types.py`, `web/dashboard.py`, `web/templates/index.html`, `config.py`, `config.yaml.default`, `setup.sh`

**Migration v9** adds `registered_tools` table:
```sql
CREATE TABLE IF NOT EXISTS registered_tools (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    code            TEXT NOT NULL,
    created_at      REAL NOT NULL
);
```

**`ToolExecutor`:**
- Add `_dynamic_tools: dict[str, Callable] = {}` to `__init__`
- Add `register_dynamic_tool(name: str, fn: Callable) -> None` method
- In `execute()`, after all static `if name == ...` checks, add: `if name in self._dynamic_tools: return await self._dynamic_tools[name](args)`

**`_add_tool(name, description, parameters_schema, code)` method:**
1. Check `CHAT_ALLOW_TOOL_REGISTRATION` — return error if `False`
2. `compile(code, "<string>", "exec")` — syntax check, raises `SyntaxError` on bad code
3. Verify `_pending_patch` contains a sandbox-validated version of this code (i.e., `sandbox_code` was called and passed) — if `_sandbox_passed` flag is not set, return error "Run sandbox_code first"
4. Queue a `code_proposal` approval card with payload `{name, description, parameters_schema, code, sandbox_output}`
5. Return `ToolResult(awaiting_approval=True)`

**`ToolExecutor` state:** Add `_sandbox_passed: bool = False` and `_sandbox_output: str = ""` flags; `sandbox_code` sets these on success; `reset()` clears them.

**`CARD_TYPE_CODE_PROPOSAL = "code_proposal"`** in `utils/card_types.py`.

**Dashboard dispatch for `code_proposal`:**
- Handler `_execute_code_proposal(payload)`:
  1. Write `user_tools/<name>.py`
  2. `importlib.import_module("user_tools.<name>")` — import the module
  3. Extract the callable (function named `tool_implementation` or `name`)
  4. Call `get_supervisor_instance()._tool_executor.register_dynamic_tool(name, fn)` if supervisor is running
  5. `INSERT INTO registered_tools` (or `INSERT OR REPLACE`)
  6. Emit `publish_event({"event_type": "tool_registered", "name": name})`

**Startup loading** in `supervisor_main()` (in `main.py`): After building the `ToolExecutor`, read all rows from `registered_tools` and call `executor.register_dynamic_tool(name, fn)` for each.

**`code_proposal` card in `index.html`:** New template block for `card_type == "code_proposal"`: shows `name`, `description`, code in a `<pre>` block, sandbox output in a collapsible `<details>` block, approve/reject/defer buttons.

---

### Item 72 — Tests

**Files:** `tests/test_chat.py` (new), `tests/test_core.py`

**`tests/test_core.py`** — add `CHAT_MEMORY_TOP_K` and `CHAT_ALLOW_TOOL_REGISTRATION` to `TestConfigDefaults`.

**`tests/test_chat.py`** test classes:

| Class | Tests |
|-------|-------|
| `TestMigrationV8` | `agent_memory` exists; `chat_sessions` exists; `chat_messages` exists after `init_local_database()` |
| `TestMigrationV9` | `registered_tools` exists |
| `TestRememberRecall` | remember stores row; recall finds by keyword; recall empty on no match; recall returns at most `CHAT_MEMORY_TOP_K` rows |
| `TestChatToolRegistry` | `build_chat_tool_registry()` contains `finish_chat`; does NOT contain `apply_fix`; does NOT contain `verify_fix`; contains `remember`, `recall`, `read_source`, `propose_patch`, `sandbox_code`, `add_tool` |
| `TestAgentLoopTerminalTool` | `terminal_tool_name="finish_chat"` causes loop to terminate on `finish_chat`; default `"finish_repair"` unaffected |
| `TestReadSource` | path within repo → returns content; path outside repo → rejected; non-allowed extension → rejected; path > 8000 chars → truncated |
| `TestSandboxCode` | mocked subprocess exit 0 → `ToolResult.success=True`; mocked exit 1 → `success=False` with output; no pending patch → error |
| `TestAddTool` | `CHAT_ALLOW_TOOL_REGISTRATION=False` → error; sandbox not run → error; valid flow (mocked approval) → `awaiting_approval=True` |

---

### Safety notes

- `add_tool` requires sandbox pass + approval regardless of autonomy level — hardcoded, not gated by `AutonomyGate.should_auto_execute()`
- `read_source` and `propose_patch` are read-only and staging-only respectively; no writes to the live tree until approval
- `sandbox_code` runs in a temp directory copy — it cannot write to the real working tree
- Safety-critical files (`utils/autonomy.py`, `interfaces.py`, `config.py`, `ha_agent_advanced.py`) are not explicitly blocked from `propose_patch` in Phase 17.5; that block list is implemented in Phase 21's security review (item 85)
- `CHAT_ALLOW_TOOL_REGISTRATION` defaults to `false`; the feature is inert unless the user explicitly enables it

---

### Done when

- `/chat` tab is accessible in the dashboard and shows a working message thread
- Sending "What is the current HA disk usage?" causes Pueo to call `run_ha_command` or `read_config` and reply with disk info
- Sending "Remember that the media server is at 192.168.1.50" stores a memory row; sending "What do you remember about my media server?" returns it
- Chat history survives a page reload (session persisted in SQLite)
- With `CHAT_ALLOW_TOOL_REGISTRATION=true`, sending "Write a tool that pings a host" causes propose_patch + sandbox_code; approval card appears with diff; approving registers the tool; the tool is callable in the next chat session
- `pytest tests/test_chat.py -v` all pass; CI gate passes (90% coverage)
