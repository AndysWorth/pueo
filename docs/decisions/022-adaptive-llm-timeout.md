# ADR 022 — Adaptive Per-Call LLM Timeout with Latency History

## Status
Accepted

## Context
On 2026-08-25, a Chat session timed out after exactly 300 s with only 2 tool steps completed. Root cause: `AgentLoop.run()` wrapped its entire `_loop_body` in a single `asyncio.wait_for(..., timeout=300)`. When Ollama runs slowly (large context, thermal throttle, model swapping), two LLM calls consumed the full 300 s wall-clock budget before the third could complete — and the session was killed even though Ollama was working correctly.

The 300 s wall-clock guard was inherited from evals tuning, not from a correctness requirement. It conflated two distinct failure modes:

1. **Slow but correct**: The LLM is generating a long answer on slow hardware. This is not a failure — it is expected behavior that the old timeout could not distinguish from the next case.
2. **Genuinely stuck**: The LLM server has hung, crashed, or entered an infinite generation loop. This is a real failure that warrants aborting the call.

## Decision
Replace the single outer wall-clock guard with a **per-call timeout** on each `chat_with_tools` invocation, derived from historical LLM latency data:

```
timeout = max(
    P95(recent_latency_ms) × factor,   # expectation × safety multiplier
    min_timeout_ms                       # floor: 5 minutes
)
clamped to max_timeout_ms               # ceiling: 30 minutes
```

With fewer than 5 recorded calls for this model/provider, use a `default_timeout_ms` of 10 minutes. Config controls all knobs.

### New components

**`utils/llm/llm_stats.py`** — Two functions:
- `record_llm_call()` — inserts one row into `llm_calls` with wall-clock latency and Ollama-native timing fields (`eval_duration`, `load_duration` in nanoseconds → milliseconds).
- `expected_timeout_ms()` — queries `llm_calls`, computes Pn(latency_ms) × factor, clamps to `[min_ms, max_ms]`. Returns `default_ms` when fewer than `min_samples` rows exist.

**`llm_calls` SQLite table (V25 migration)**
```sql
CREATE TABLE llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    episode_id      TEXT,
    session_id      INTEGER,
    model           TEXT    NOT NULL,
    provider        TEXT    NOT NULL,
    call_type       TEXT    NOT NULL,
    latency_ms      REAL    NOT NULL,
    ollama_eval_ms  REAL,
    ollama_load_ms  REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER
)
```

Ollama-specific fields (`ollama_eval_ms`, `ollama_load_ms`) are stored separately so model-load spikes on cold starts do not inflate the expected generation latency.

### Changes to `AgentLoop`
- The outer `asyncio.wait_for(_loop_body, ...)` is removed.
- Each `chat_with_tools` call is wrapped with `asyncio.wait_for(..., timeout=per_call_secs)`.
- `_per_call_timeout_seconds()` computes the timeout from `expected_timeout_ms()`.
- A per-call timeout expiry logs `llm_call_stuck` and propagates `asyncio.TimeoutError` out of `_loop_body` to `run()`, which catches it as `outcome = "stuck"` (distinct from the old `"timeout"` which was a wall-clock budget overrun).
- `AGENT_MAX_WALL_SECONDS` is retired; `AGENT_MAX_WALL_SECONDS` is kept as a deprecated alias equal to `AGENT_PER_CALL_MIN_TIMEOUT_SECONDS` so call-sites that still pass `max_wall_seconds=` to `AgentLoop.__init__` silently ignore the argument.

### Principle: Correctness over speed
Pueo is a home-automation safety agent. An incomplete repair is worse than a slow one. Timeouts should fire only when something is genuinely stuck — not when the LLM is merely slow. This distinction requires knowing what "normal slow" looks like for this model on this hardware, which only long-term latency history can provide.

## Consequences
- **First 5 calls** on a fresh install use a generous 10-minute default per call. After that, the timeout adapts to real observed latency.
- **`"stuck"` outcome** (`AgentLoopOutcome`) is added alongside the existing `"exhausted"`, `"timeout"`, `"fix_failed"`, and `"awaiting_approval"`. Dashboard and callers that check outcome should treat `"stuck"` similarly to `"exhausted"` — the loop made partial progress but could not finish.
- **`OllamaClient.chat_with_tools()`** now returns a `_ollama_timing` dict alongside the normal response; `AgentLoop` pops and records it, callers that pass the response directly (tests) are unaffected because the key is absent on non-Ollama clients.
- **`AGENT_MAX_WALL_SECONDS`** config key and `AgentLoop.__init__` parameter are deprecated. The key no longer appears in `config.yaml.default`; existing configs that set it are silently ignored.
- **Config keys added** (triple-update: `config.py`, `config.yaml.default`, commented-out): `AGENT_PER_CALL_TIMEOUT_FACTOR`, `AGENT_PER_CALL_MIN_TIMEOUT_SECONDS`, `AGENT_PER_CALL_MAX_TIMEOUT_SECONDS`, `AGENT_LLM_LATENCY_LOOKBACK`, `AGENT_LLM_LATENCY_PERCENTILE`.
- The evaluation matrix row `"Loop wall time | ≤ 120 seconds"` is removed; the `"Inference latency"` row is updated to describe the adaptive per-call timeout.

## Related decisions
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): `expected_timeout_ms()` reads from SQLite; the timeout derivation is deterministic and requires no LLM call.
- [ADR 005 — asyncio over agentic framework](005-asyncio-over-agentic-framework.md): per-call `asyncio.wait_for` fits cleanly into the existing asyncio event loop; no framework change required.
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): `expected_timeout_ms()` is keyed on `model` and `provider`; cloud calls and local calls build separate latency histories.
