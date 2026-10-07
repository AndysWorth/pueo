# OpenAI-Compatible Provider — Implementation Spec

Tracked in GitHub issue [#802](https://github.com/AndysWorth/pueo/issues/802).  
Design decision: [ADR 036](../decisions/036-openai-compatible-provider.md).  
Sessions: S5 (this spec) → S6 (implementation).

---

## Goal

Add `LLM_PROVIDER=openai_compat` so Pueo can run inference against any server that
speaks the OpenAI `/v1/chat/completions` wire format — LM Studio, mlx-lm variants
(vllm-mlx, oMLX, FastMLX), llama-swap, and vLLM — without adding a new Python
dependency or modifying `AgentLoop`.

---

## Files to create

| File | Purpose |
|---|---|
| `utils/llm/openai_compat_client.py` | `OpenAICompatClient` + `FakeOpenAICompatClient` |

## Files to modify

| File | Change |
|---|---|
| `utils/llm/llm_factory.py` | New `openai_compat` branch in `make_llm_client()`, `_default_model_for_provider()`, `_provider_name()` |
| `utils/llm/model_options.py` | Short-circuit in `one_shot_options()` for `openai_compat` |
| `utils/agent/agent_loop.py` | Short-circuit in `_derive_loop_call_options()` for `openai_compat` |
| `config.py` | Three new keys: `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_MODEL`, `OPENAI_COMPAT_NUM_CTX` |
| `config.yaml.default` | Three new keys under `llm.openai_compat:` section |
| `setup.sh` | New branch for `openai_compat`: prompt for base URL + model, skip inference model pull |
| `CLAUDE.md` | Update Project Overview provider list and Evaluation Matrix WAN row |

---

## `OpenAICompatClient` design

```python
class OpenAICompatClient:
    """Implements LLMClientProtocol against an OpenAI-compatible server.

    Translation responsibilities (AgentLoop is not modified):
    - _translate_messages_to_openai: converts Ollama-shaped history to OpenAI wire format
    - chat_with_tools: normalises response (arguments string → dict, keep tool id)
    - chat: uses response_format=json_object + schema in system prompt
    """

    def __init__(self) -> None:
        import config as _cfg
        self._base_url = _cfg.OPENAI_COMPAT_BASE_URL.rstrip("/")
        self._model = _cfg.OPENAI_COMPAT_MODEL
        self._api_key = os.environ.get("OPENAI_COMPAT_API_KEY", "not-needed")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(connect=10.0, read=1800.0, write=30.0, pool=5.0),
        )
```

### `chat_with_tools()`

1. Call `_translate_messages_to_openai(messages)` → `openai_messages`
2. POST to `/chat/completions`:
   ```python
   {
       "model": model,
       "messages": openai_messages,
       "tools": tools,        # already in OpenAI format (same as Ollama's format)
       "tool_choice": "auto",
       "temperature": options.get("temperature", 0.0) if options else 0.0,
   }
   ```
3. Parse response:
   - `content = choices[0].message.content or ""`
   - `tool_calls_raw = choices[0].message.tool_calls or []`
   - For each tool call: `json.loads(tc.function.arguments)` → dict; keep `tc.id`
4. Build return dict (Ollama-compatible + `id` preserved):
   ```python
   {
       "role": "assistant",
       "content": content,
       "tool_calls": [
           {"id": tc.id, "function": {"name": tc.function.name, "arguments": args_dict}}
           for tc, args_dict in zip(tool_calls_raw, parsed_args)
       ],
       "_ollama_timing": {
           "eval_ms": None,
           "load_ms": None,
           "input_tokens": usage.prompt_tokens,
           "output_tokens": usage.completion_tokens,
       },
   }
   ```

### `chat()` (structured one-shot)

1. Append the JSON schema as a `"Respond ONLY with valid JSON matching this schema: ..."` line to the system message.
2. POST to `/chat/completions` with `"response_format": {"type": "json_object"}`.
3. Return `{"message": {"content": choices[0].message.content}}`.

### `_translate_messages_to_openai(messages)`

Walk the message list and produce an OpenAI-format list:

```
system → {"role": "system", "content": ...}  (unchanged)
user   → {"role": "user",   "content": ...}  (unchanged)
assistant (no tool_calls) → {"role": "assistant", "content": ...}
assistant (tool_calls)    → {
    "role": "assistant",
    "tool_calls": [
        {
            "id":   tc.get("id") or f"tc_{seq:04d}",
            "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": json.dumps(tc["function"]["arguments"])
                             if isinstance(tc["function"]["arguments"], dict)
                             else tc["function"]["arguments"],
            },
        }
        for seq, tc in enumerate(tool_calls, start=1)
    ],
}
tool → {
    "role":         "tool",
    "tool_call_id": <id from preceding assistant message matching function.name>,
    "content":      msg["content"],
}
```

**ID lookup for tool results:** maintain a name→id mapping as you walk forward.
When you encounter an assistant message with tool calls, record
`{tc["function"]["name"]: tc_id}`. When you encounter the following tool messages,
pop the matching name. This handles the common case of unique names per assistant turn.
If a name appears twice in the same assistant turn (unusual), the first call's ID is used.

---

## Config triple-update

### `config.py`

```python
_openai_compat_cfg = _llm_cfg.get("openai_compat", {})
OPENAI_COMPAT_BASE_URL: str = _openai_compat_cfg.get(
    "base_url", "http://localhost:1234/v1"
)
OPENAI_COMPAT_MODEL: str = _openai_compat_cfg.get("model", "qwen2.5-coder:7b")
OPENAI_COMPAT_NUM_CTX: int = int(_openai_compat_cfg.get("num_ctx", 8192))

if LLM_PROVIDER == "openai_compat" and not os.environ.get("OPENAI_COMPAT_API_KEY"):
    os.environ["OPENAI_COMPAT_API_KEY"] = "not-needed"  # default; log a debug note
```

### `config.yaml.default`

```yaml
llm:
  provider: local           # local | cloud | both | openai_compat
  openai_compat:
    base_url: http://localhost:1234/v1   # LM Studio default; change for mlx-lm / vLLM
    model: qwen2.5-coder:7b             # model name sent in every request
    num_ctx: 8192                        # context window (can't be auto-detected)
```

### `setup.sh`

Add a branch when user selects `openai_compat`:
- Prompt for `BASE_URL` (default `http://localhost:1234/v1`) and `MODEL`
- Skip `ollama pull <inference_model>`
- Still install Ollama and pull `nomic-embed-text` for RAG

---

## `_derive_loop_call_options()` short-circuit

```python
if _cfg.LLM_PROVIDER == "openai_compat":
    from utils.llm.model_options import ModelCallOptions
    ctx = _cfg.OPENAI_COMPAT_NUM_CTX
    return ModelCallOptions(
        think=None,
        num_ctx=ctx,
        temperature=0.0,
        keep_alive="5m",   # unused by the OpenAI-compat client; harmless placeholder
    )
```

---

## `llm_factory.py` updates

```python
def make_llm_client() -> "LLMClientProtocol":
    if config.LLM_PROVIDER == "cloud":
        from utils.llm.cloud_client import ClaudeAPIClient
        return ClaudeAPIClient()
    if config.LLM_PROVIDER == "openai_compat":
        from utils.llm.openai_compat_client import OpenAICompatClient
        return OpenAICompatClient()
    return OllamaClient()

def _default_model_for_provider() -> str:
    if config.LLM_PROVIDER == "cloud":
        return config.CLOUD_MODEL
    if config.LLM_PROVIDER == "openai_compat":
        return config.OPENAI_COMPAT_MODEL
    return config.OLLAMA_MODEL

def _provider_name() -> str:
    if config.LLM_PROVIDER == "cloud":
        return "cloud"
    if config.LLM_PROVIDER == "openai_compat":
        return "openai_compat"
    return "local"
```

---

## Tests

| Test file | What to test |
|---|---|
| `tests/test_openai_compat_client.py` | `_translate_messages_to_openai`: Ollama history → OpenAI format; `json.loads` on arguments; id threading; tool result `tool_call_id` matching |
| `tests/test_openai_compat_client.py` | `chat_with_tools()`: mock `httpx.AsyncClient.post`; assert request body; assert returned `_ollama_timing.input_tokens` from usage |
| `tests/test_openai_compat_client.py` | `chat()`: assert `response_format` in request body; assert schema injected in system prompt |
| `tests/test_config.py::TestConfigDefaults` | One test per new key (`OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_MODEL`, `OPENAI_COMPAT_NUM_CTX`) via `isolated_config` fixture |
| `tests/test_llm_factory.py` | `make_llm_client()` returns `OpenAICompatClient` when `LLM_PROVIDER=openai_compat` |

`FakeOpenAICompatClient` mirrors `FakeLLMClient` — same call-sequence pattern as
`FakeToolCallingLLMClient` — so it can be injected in `AgentLoop` unit tests.

---

## Known limitations

1. **`chat()` schema enforcement** — `response_format: json_object` does not constrain
   to a specific schema. Call sites already catch `JSONDecodeError` and fall back to a
   default, so this degrades gracefully.

2. **Parallel tool calls with duplicate names** — unusual in Pueo's tool set but possible
   in theory. The history translator uses the first unmatched ID, which may mis-route
   tool results. The server will still process them; at worst, the history replay is
   slightly incorrect but the agent loop continues.

3. **No `think` mode** — OpenAI-compat servers do not expose Ollama's `think` parameter.
   Setting `OLLAMA_THINK_MODE` has no effect when `LLM_PROVIDER=openai_compat`.

4. **No model-load timing** — `ollama_eval_ms` and `ollama_load_ms` are always `None`.
   `expected_timeout_ms()` falls back to wall-clock latency for P95 computation, which is
   correct but slightly noisier than Ollama's pure generation time.

---

## Exercising the change (S6 sign-off)

1. Start LM Studio, load `qwen2.5-coder:7b`, enable the server on port 1234.
2. Set `LLM_PROVIDER=openai_compat` in `config.yaml`.
3. Run `python main.py --mode chat` and ask "What is the disk usage on my HA host?"
4. Confirm tool calls appear in the trace and a coherent answer is returned.
5. Check `llm_calls` SQLite table: `provider=openai_compat`, `input_tokens` populated,
   `ollama_eval_ms=NULL`.
