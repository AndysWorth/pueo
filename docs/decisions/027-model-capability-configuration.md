# ADR 027 — Model Capability Configuration: Closing the Select/Use Gap

## Status
Accepted

## Context

`recommend_model()` selected a model based on detected capabilities, then discarded that information. Every LLM call used identical parameters regardless of what capabilities were detected:

```python
options={"temperature": 0.0}   # ignores model's recommended temperature
# no num_ctx                   # context_length detected but discarded
# no think=                    # thinking capability never used
# no keep_alive                # model unloads between cycles, 10–30s reload cost
```

Ollama uses ~2048 as the global context window default when the Modelfile does not set it. Agent sessions with multiple tool results can easily exceed 2048 tokens, causing silent history truncation that degrades LLM reasoning quality without any error.

`CANDIDATE_MODELS` was a hardcoded tier list that missed all newer installed models (`qwen3.8:27b-mlx`, `qwen3.6`, `gemma4`, `deepseek-r1`). On a 64 GB M1 Max, `recommend_model()` returned `qwen3:32b` — missing `qwen3.8`, the better/newer model actually installed.

Two Ollama bugs informed the thinking mode policy decision (both researched live 2026-09-15):
- **Fixed**: ~60% tool-call failure rate when `think + tools` were both active (Ollama 0.17.x–0.30.x; clean in 0.31.2+).
- **Still open** — [Issue #17617](https://github.com/ollama/ollama/issues/17617): a leaked `</think>` tag in assistant history (from long-context model degeneration) can lock an agentic client into a self-sustaining infinite tool-call loop that reportedly ran ~31M tokens in one case. Directly relevant to Pueo's multi-turn agent loops.

## Decision

Build a **capability-to-configuration translation layer** (`utils/llm/model_options.py`) that derives the right call parameters for each use case from model capabilities and runtime context.

### `ModelCallOptions` dataclass

```python
@dataclass
class ModelCallOptions:
    think: bool | Literal['low', 'medium', 'high'] | None
    num_ctx: int
    temperature: float
    keep_alive: str
    num_predict: int | None
    seed: int | None
```

### `derive_call_options()` policy

**`think`**: Non-thinking models → `None` (don't pass the parameter). Thinking models:
- `think_mode="auto"` (default): `think=False` in production (DEBUG_LEVEL=0); `think='low'` at level 1; `think='high'` at level 2+. Production default is `False` — not because of the (now-fixed) 60% failure rate, but because Issue #17617 means any `</think>` that leaks into history can cause an infinite loop in Pueo's agentic sessions.
- `think_mode="off"`: always `False`
- `think_mode="low"|"medium"|"high"`: fixed budget for inspection use

**`num_ctx`**: Derived from `min(context_length, safe_max)` where `safe_max` scales with available RAM (131072 for 40+ GB, 32768 for 20–40 GB, 16384 for 10–20 GB, 8192 otherwise). Overridable with `OLLAMA_NUM_CTX`.

**`temperature`**: `0.0` always except when thinking is active and the model has a `recommended_temperature` (e.g. 0.6 for qwen3 — temperature 0 + thinking produces repetitive reasoning loops; the model's own recommendation is the right choice).

**`keep_alive`**: `"-1"` in supervisor mode (model stays loaded while Pueo is alive); `"5m"` in one-shot mode.

**`num_predict`**: `1024` for one-shot triage/analysis calls (caps runaway generation in structured output calls); `None` (unlimited) for agent loops.

### Dynamic model scoring

`CANDIDATE_MODELS` is removed entirely. `recommend_model()` is replaced with:

```python
score = param_count_b + (2.0 if has_thinking else 0.0)
```

Any model with `tools` capability in `ollama show` output that fits within RAM budget is eligible. Parameter count is parsed from the model name regex `(\d+(?:\.\d+)?)b`. The thinking bonus (+2.0) reflects that thinking models produce higher-quality reasoning at equivalent parameter count.

### Three new config keys (triple-update rule applied)

```
OLLAMA_THINK_MODE  (config.py + config.yaml.default + setup.sh)
OLLAMA_NUM_CTX     (config.py + config.yaml.default + setup.sh)
OLLAMA_KEEP_ALIVE  (config.py + config.yaml.default + setup.sh)
```

## Consequences

- `OllamaModelInfo` gains `has_thinking: bool` and `recommended_temperature: Optional[float]`; `_check_model_caps()` returns a 4-tuple
- `LLMClientProtocol.chat_with_tools()` gains `think` parameter (default `None` — backward-compatible)
- `OllamaClient.chat_with_tools()` passes `think` and `keep_alive` to the Ollama SDK when set
- `ClaudeAPIClient.chat_with_tools()` accepts `think` (ignored — Anthropic uses its own mechanism); sets `_thinking` on result unconditionally when thinking blocks are present
- `AgentLoop._loop_body()` calls `_derive_loop_call_options()` once per session before the loop
- Five one-shot callers (`ha_log_monitor`, `config_analysis`, `tool_executor`, `netalertx/diagnosis`, `netalertx/log_monitor`) use `one_shot_options()` instead of `{"temperature": 0.0}`
- `_review_limit()` in `AgentLoop` retains `{"temperature": 0.0}` — it is a one-off self-assessment call, not a tool-calling session
- `setup.sh` hardware tier table updated to recommend `qwen3` family instead of `qwen2.5-coder`

## Related decisions
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): `temperature=0.0` rule for structured output calls is preserved; the exception (thinking mode) is explicitly documented here.
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): `ClaudeAPIClient` accepts and ignores `think=` to maintain interface conformance.
- [ADR 022 — Adaptive per-call LLM timeout](022-adaptive-per-call-timeout.md): `keep_alive="-1"` in supervisor mode reduces the `load_duration` contribution to the latency percentile, which in turn reduces estimated timeouts over time.
