# ADR 036 — OpenAI-Compatible LLM Provider

**Status:** Accepted
**Date:** 2026-10-07

## Context

Pueo currently supports three `LLM_PROVIDER` values (ADR 006):

- `local` — Ollama; zero WAN for inference
- `cloud` — Anthropic Claude API
- `both` — Ollama for autonomous cycles, Claude for approved escalation

All three have a shared limitation: they require a specific runtime (Ollama) or a cloud
account (Anthropic). On Apple Silicon Macs, several alternative local inference servers
offer better performance per watt, more recent model support, or simpler deployment than
Ollama, while exposing an OpenAI-compatible `/v1/chat/completions` endpoint:

- **LM Studio** (0.3.6+ with tool calling, 0.4.0+ headless daemon)
- **mlx-lm servers** (vllm-mlx, oMLX, FastMLX, MLX-Textgen)
- **llama-swap** (Go proxy in front of llama.cpp or other runtimes)
- **vLLM** (high-throughput; mostly Linux GPU servers but Apple Silicon work in progress)

Adding a fourth provider behind the existing `LLMClientProtocol` boundary lets Pueo run
on any of these without forking or patching the inference layer.

## Decision

1. **Add `LLM_PROVIDER=openai_compat`** as a fourth provider value.

2. **Implement `OpenAICompatClient`** in `utils/llm/openai_compat_client.py`, using
   `httpx.AsyncClient` (already in `requirements.txt`). No new dependency is added.

3. **The client is the translation boundary.** `AgentLoop` is not modified. The client
   translates the Ollama-shaped message history to OpenAI wire format on every call and
   normalises the response back to Ollama-compatible shape before returning.

4. **`both` pairing is not extended.** The `both` semantic is specifically "local Ollama
   for autonomous cycles + Claude for approved escalation." Adding an `openai_compat`
   leg to `both` is deferred; operators who want strong local inference use
   `openai_compat` with a capable server.

5. **RAG embeddings stay on Ollama** in all modes (same policy as `cloud`, ADR 006).

## Wire format and translation

### Request (chat/completions)

```json
POST {OPENAI_COMPAT_BASE_URL}/chat/completions
{
  "model": "qwen2.5-coder:7b",
  "messages": [ ... ],
  "tools": [ ... ],
  "tool_choice": "auto",
  "temperature": 0.0
}
```

### Response (tool call path)

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "read_config",
          "arguments": "{\"path\": \"/config\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }],
  "usage": { "prompt_tokens": 150, "completion_tokens": 25, "total_tokens": 175 }
}
```

Two key differences from Ollama that the client normalises before returning:

1. **`arguments` is a JSON-encoded string**, not a dict. The client calls
   `json.loads(arguments)` and stores a dict in the normalised result.

2. **Tool calls carry `id` fields.** The client preserves these in the normalised
   result so `AgentLoop` stores them in the message history for history replay.

### History translation (`_translate_messages_to_openai`)

On every `chat_with_tools()` call, the client translates the accumulated message history
before sending. The translation is stateless (derives everything from the history list):

| Incoming (Ollama/Pueo format) | Outgoing (OpenAI wire format) |
|---|---|
| `{"role": "system", "content": ...}` | unchanged |
| `{"role": "user", "content": ...}` | unchanged |
| `{"role": "assistant", "tool_calls": [...]}` | add `"type": "function"`, convert `arguments` dict → JSON string, keep `id` if present or assign `tc_{seq:04d}` |
| `{"role": "tool", "name": X, "content": Y}` | look back to the most recent assistant message, find tool call with `function.name == X`, use its `id` as `"tool_call_id"` |

Parallel tool calls in one assistant message are handled by matching each subsequent tool
result message by `name`. If two tool calls share the same function name (unusual in Pueo's
tool set), the first unmatched ID wins.

## Config keys (triple-update rule applies)

| Key | Type | Default | Description |
|---|---|---|---|
| `OPENAI_COMPAT_BASE_URL` | str | `"http://localhost:1234/v1"` | Server base URL including `/v1` |
| `OPENAI_COMPAT_MODEL` | str | `"qwen2.5-coder:7b"` | Model name sent in every request |
| `OPENAI_COMPAT_NUM_CTX` | int | `8192` | Context window; can't be auto-detected from server |

`OPENAI_COMPAT_API_KEY` is an **env var only** (same credential-hygiene rule as
`ANTHROPIC_API_KEY`). Default value `"not-needed"` satisfies servers that require a
non-empty string but do not validate the value. A startup guard raises if the env var is
absent and `LLM_PROVIDER=openai_compat`.

## Model capability detection

Ollama's capability detection (`list_ollama_models`, `detect_local_hardware`) is not
applicable here. `_derive_loop_call_options()` in `agent_loop.py` short-circuits for
`openai_compat` and returns fixed `ModelCallOptions`:

| Field | Value | Reason |
|---|---|---|
| `think` | `None` | Not an Ollama-native param; excluded from request |
| `temperature` | `0.0` | Deterministic default; no model recommendation available |
| `num_ctx` | `OPENAI_COMPAT_NUM_CTX` | Config-supplied; not detectable from server |
| `keep_alive` | N/A | Server-managed; not sent in OpenAI-compat requests |
| `num_predict` | `None` | Let the server decide |

The `one_shot_options()` utility in `model_options.py` also short-circuits for
`openai_compat`, returning `{"temperature": 0.0, "num_predict": 1024}` (no `num_ctx`
since the OpenAI compat request body uses `max_tokens`, not `num_ctx`).

## Timing and `llm_stats`

| `record_llm_call` field | Source |
|---|---|
| `latency_ms` | Wall-clock time of the `httpx` call |
| `input_tokens` | `usage.prompt_tokens` from response |
| `output_tokens` | `usage.completion_tokens` from response |
| `ollama_eval_ms` | `None` — not available |
| `ollama_load_ms` | `None` — not available |

`expected_timeout_ms()` and `_provider_name()` use `provider="openai_compat"`.
With fewer than 5 recorded samples the 10-minute default applies (same as other providers).

## `chat()` method — structured output limitation

The `chat()` method (used by one-shot pre-filters like log-line triage) passes
`format=Model.model_json_schema()` in the Ollama protocol. The equivalent for OpenAI-compat
is `response_format: {"type": "json_schema", "json_schema": {...}}` (supported in LM Studio
0.3.29+, vLLM, some mlx servers) or `{"type": "json_object"}` (wider support, no schema
enforcement).

The `OpenAICompatClient.chat()` implementation:
1. Sends `response_format: {"type": "json_object"}` — broad compatibility.
2. Includes the JSON schema as a formatted string appended to the system prompt so the
   model sees the target shape.
3. Returns the response in the same `{"message": {"content": "<json>"}}` shape that
   `OllamaClient.chat()` returns.

**Known limitation:** Schema is enforced by prompt nudging, not by the server's constrained
decoding. This is less reliable than Ollama's `format=` parameter. The affected call sites
(log-line triage, config analysis) already handle malformed JSON responses gracefully by
catching `json.JSONDecodeError` and returning a default.

## Consequences

- No new `requirements.txt` entry — `httpx` is already present.
- `make_llm_client()` gains an `openai_compat` branch importing `OpenAICompatClient` lazily.
- `_default_model_for_provider()` returns `config.OPENAI_COMPAT_MODEL` for this provider.
- `_provider_name()` returns `"openai_compat"`.
- `setup.sh` prompts for `OPENAI_COMPAT_BASE_URL` and `OPENAI_COMPAT_MODEL` when
  `openai_compat` is chosen; skips the Ollama inference model pull; still installs Ollama
  for RAG embeddings.
- CLAUDE.md Project Overview provider list is updated.
- The evaluation matrix `"WAN packets during fix cycles"` row gains a fourth note: `0 WAN
  when LLM_PROVIDER=openai_compat` (server runs locally by definition).

## Target servers verified (as of 2026-10-07)

| Server | Tool calling | Notes |
|---|---|---|
| LM Studio 0.3.6+ | ✅ | `base_url=http://localhost:1234/v1`; no API key validation |
| mlx-lm / vllm-mlx | ✅ | Qwen/Llama chat templates required on model |
| oMLX | ✅ | Supports Llama, Qwen, DeepSeek, Gemma tool formats |
| llama-swap | ✅ (pass-through) | Proxies to any OpenAI-compat upstream; tool support depends on upstream |
| vLLM 0.9+ | ✅ | `base_url=http://localhost:8000/v1`; per-request metrics with `--enable-per-request-metrics` |

## Related decisions

- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): this ADR extends
  the existing factory pattern; `LLMClientProtocol` is unchanged.
- [ADR 022 — Adaptive LLM timeout](022-adaptive-llm-timeout.md): timing fields
  (`ollama_eval_ms`, `ollama_load_ms`) are nullable for this provider.
- [ADR 027 — Model capability configuration](027-model-capability-configuration.md):
  `_derive_loop_call_options` short-circuits for `openai_compat`; ADR 027 patterns
  (think mode, keep_alive) do not apply.
