# LLM Provider Abstraction + Cloud Escalation

Part of the [Roadmap](../roadmap.md) · Milestone 7 · Phase 18 (items 73–76).

---

### Problem

Local 7B models can follow a multi-step tool-calling loop and handle common failure patterns. Novel failure modes with complex multi-step reasoning exceed their reliable capability. When the tool loop exhausts its budget without a fix, the incident currently goes unresolved.

Additionally, Pueo's original "0 WAN during fix cycles" constraint is too rigid for some deployments. Some users would prefer a cloud model as their primary inference engine, or want to use cloud as a capable fallback rather than leaving incidents unresolved.

---

### Decision

Make the LLM inference engine a first-class switchable setting (`LLM_PROVIDER`), not a hardcoded implementation detail. Three modes:

| Mode | Inference | RAG embeddings | Cloud spend |
|------|-----------|---------------|-------------|
| `local` (default) | Ollama (local) | Ollama / nomic-embed-text | None |
| `cloud` | Anthropic API | Ollama / nomic-embed-text | Every inference call |
| `both` | Ollama for autonomous cycles; Claude available vian approved escalation | Ollama / nomic-embed-text | Only on approved escalations |

RAG embeddings always use local Ollama regardless of `LLM_PROVIDER` — `nomic-embed-text` is a small model and its privacy/locality properties are desirable.

The "0 WAN during autonomous fix cycles" evaluation-matrix constraint is explicitly overridden by this milestone. It is replaced with: _WAN during autonomous fix cycles = 0 when `LLM_PROVIDER=local` (default); cloud mode intentionally sends inference traffic to Anthropic._

approved escalation — the original M7 scope — becomes the natural behavior of `both` mode: when `AgentLoop` returns `outcome = "exhausted"` or `"timeout"`, a `CARD_TYPE_CLOUD_ESCALATION` approval card is surfaced. The user approves per-incident; the same tool registry re-runs under Claude with the full failed-loop step history as context.

---

### Architecture

#### `utils/llm_factory.py` — single decision point

```python
def make_llm_client() -> LLMClientProtocol:
    if config.LLM_PROVIDER == "cloud":
        return ClaudeAPIClient()
    else:
        return OllamaClient()   # "local" or "both": autonomous cycles stay local

def _default_model_for_provider() -> str:
    return config.CLOUD_MODEL if config.LLM_PROVIDER == "cloud" else config.OLLAMA_MODEL
```

All 20+ call-sites that currently fall back to `OllamaClient()` are migrated to `make_llm_client()`. `AgentLoop.__init__` uses `_default_model_for_provider()` as its `model` default.

#### `utils/cloud_client.py` — `ClaudeAPIClient`

Implements `LLMClientProtocol` from `interfaces.py` (unchanged). Four translation layers, all internal to the client so no caller is aware of provider differences:

**1. Tool schema adapter** — called once per `chat_with_tools` invocation. Pueo's tool registry emits OpenAI/Ollama format:
```json
{"type": "function", "function": {"name": "…", "description": "…", "parameters": {…}}}
```
Anthropic expects:
```json
{"name": "…", "description": "…", "input_schema": {…}}
```
Strip the outer `type`/`function` envelope; rename `parameters` → `input_schema`.

**2. Response normalizer** — Anthropic returns tool calls as content blocks:
```json
{"type": "tool_use", "id": "toolu_…", "name": "…", "input": {…}}
```
Normalize to the shape `agent_loop.py` lines 190–203 expect:
```json
{"tool_calls": [{"function": {"name": "…", "arguments": {…}}}], "content": ""}
```
Store the original `id` values in a side-table for use by the history translator.

**3. History translator** — `agent_loop.py` accumulates messages in Ollama-shaped format:
- Assistant turn: `{"role": "assistant", "content": "", "tool_calls": [{"function": {…}}]}`
- Tool result: `{"role": "tool", "content": "…", "name": "tool_name"}`

On each call, `ClaudeAPIClient.chat_with_tools` translates the full accumulated history to Anthropic format before sending:
- Assistant turn → `{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_…", "name": "…", "input": {…}}]}`
- Tool result → `{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_…", "content": "…"}]}`

**4. `chat()` method** (structured output for Pydantic schemas) — Anthropic does not support a `format` dict like Ollama. Implement via tool-forcing: define a single schema tool with the target Pydantic schema as `input_schema`, set `tool_choice={"type": "tool", "name": schema_tool_name}`, parse the `input` field of the resulting tool-use block as the structured result.

#### Instantiation rules

- `ClaudeAPIClient.__init__` reads `config.ANTHROPIC_API_KEY`; raises `RuntimeError` if absent (startup guard in `config.py` is the first line of defense)
- Default model: `config.CLOUD_MODEL`; callers may override
- Uses `anthropic` Python SDK (`requirements.txt`)

---

### Configuration

New config sections (ADR 001 triple-update rule: `config.py` + `config.yaml.default` + `setup.sh`):

| Key | YAML path | Default | Source |
|-----|-----------|---------|--------|
| `LLM_PROVIDER` | `llm.provider` | `"local"` | `config.yaml` |
| `CLOUD_MODEL` | `cloud.model` | `"claude-sonnet-4-5"` | `config.yaml` |
| `CLOUD_MAX_COST_PER_INCIDENT_USD` | `cloud.max_cost_per_incident_usd` | `0.50` | `config.yaml` |
| `CLOUD_MAX_DAILY_SPEND_USD` | `cloud.max_daily_spend_usd` | `5.00` | `config.yaml` |
| `ANTHROPIC_API_KEY` | — (env only) | `None` | `os.getenv("ANTHROPIC_API_KEY")` |

**`config.yaml.default` additions:**
```yaml
llm:
  provider: "local"  # "local" | "cloud" | "both"
                     # local: Ollama only (default, no WAN for inference)
                     # cloud: Anthropic API as primary LLM
                     # both: Ollama for autonomous cycles + Claude available for approved escalation

cloud:
  model: "claude-sonnet-4-5"           # Claude model for cloud/both modes
  max_cost_per_incident_usd: 0.50      # Hard cap per escalation incident
  max_daily_spend_usd: 5.00           # Rolling 24-hour spend cap
  # ANTHROPIC_API_KEY must be exported in ~/.zshenv — never written here
```

**Startup guards in `config.py`** (after all keys are loaded):
1. If `LLM_PROVIDER in ("cloud", "both")` and `ANTHROPIC_API_KEY is None`: raise `RuntimeError`.
2. Read raw `config.yaml` text; if `"ANTHROPIC_API_KEY"` or `"anthropic_api_key"` appears as a YAML key: raise (credential stored in wrong location).

---

### `setup.sh` provider wizard

Inserted after Section 2 (Ollama, ~line 148):

1. `ask "LLM provider (local/cloud/both)" "local" LLM_PROVIDER`
2. If `cloud` or `both`:
   - `ask "Claude model" "claude-sonnet-4-5" CLOUD_MODEL`
   - Print: `"Add 'export ANTHROPIC_API_KEY=<your-key>' to ~/.zshenv and reload your shell."`
   - Warn if `ANTHROPIC_API_KEY` is not currently set in the environment
3. If `cloud`: skip Ollama inference model pull (still pull `nomic-embed-text` for RAG embeddings — Ollama is required in all modes for embeddings)
4. Write `llm.provider`, `cloud.model`, and billing keys into the `config.yaml` heredoc

---

### Dashboard settings UI

New `"LLM Provider"` group in `_EDITABLE_PARAMS` (`web/dashboard.py`):

| Key | Control | Notes |
|-----|---------|-------|
| `LLM_PROVIDER` | `<select>` (options: local/cloud/both) | `restart_required: True` |
| `CLOUD_MODEL` | text input | `restart_required: True` |
| `CLOUD_MAX_COST_PER_INCIDENT_USD` | number input (0–50) | live-apply |
| `CLOUD_MAX_DAILY_SPEND_USD` | number input (0–500) | live-apply |

The existing `options` support in `settings.html` (line 74) already renders a `<select>` dropdown — no structural template changes needed. A read-only `api_key_set` boolean is added to the `GET /settings` response and rendered as a status badge ("ANTHROPIC_API_KEY: set ✓ / not set ✗") in the LLM Provider card via a small template addition.

---

### Billing guard

**DB migration v15** — `cloud_spend` table:
```sql
CREATE TABLE IF NOT EXISTS cloud_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,
    timestamp REAL NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
```

Helpers in `web/database.py` (or a new `utils/billing.py`):
- `record_cloud_spend(incident_id, model, input_tokens, output_tokens, cost_usd)`
- `get_daily_spend() -> float` — `SUM(cost_usd)` where `timestamp >= local midnight`
- `get_incident_spend(incident_id) -> float`

`ClaudeAPIClient` flow per call:
1. Estimate token count (system prompt + context + tool schemas + new message)
2. Compute estimated cost = `(input_estimate / 1_000_000) × input_price + (output_estimate / 1_000_000) × output_price` (prices hardcoded per model, e.g. Sonnet 4.5: $3/$15 per MTok)
3. Check `get_incident_spend(incident_id) + estimate > CLOUD_MAX_COST_PER_INCIDENT_USD` → raise `BillingCapError`
4. Check `get_daily_spend() + estimate > CLOUD_MAX_DAILY_SPEND_USD` → raise `BillingCapError`
5. Make the API call; record actual token counts from the response

---

### Approved Escalation Card (LLM_PROVIDER = "both")

Triggered when `AgentLoop` returns `outcome in ("exhausted", "timeout")` and `LLM_PROVIDER == "both"`:

- Card type: `CARD_TYPE_CLOUD_ESCALATION` (add to `utils/card_types.py`)
- Card fields:
  - Summary of tool calls from `AgentLoopResult.steps`
  - Termination reason + step count
  - Estimated cost × current billing caps status (`$X.XX estimated | $Y.YY daily / $Z.ZZ cap`)
  - Approve / Reject buttons
- On approve:
  1. Pre-flight billing check (raises if caps exceeded — card shown as declined)
  2. Re-run `AgentLoop` with `ClaudeAPIClient()`, model = `CLOUD_MODEL`, and full failed-loop step history prepended as context messages
  3. Record spend to `cloud_spend` table

When `LLM_PROVIDER = "cloud"`, no escalation card is needed — the primary loop already uses Claude.

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 73 | `utils/cloud_client.py`: `ClaudeAPIClient` (tool schema adapter, response normalizer, history translator, structured-output via tool-forcing); `utils/llm_factory.py`: `make_llm_client()` + `_default_model_for_provider()`; migrate all 20+ `OllamaClient()` call-sites; `AgentLoop` model default becomes provider-aware |
| 74 | Config: `LLM_PROVIDER`, `CLOUD_MODEL`, billing keys in `config.py` + `config.yaml.default`; `ANTHROPIC_API_KEY` env guard + credential guard; `setup.sh` provider wizard; ADR 006 |
| 75 | Dashboard `LLM Provider` settings group in `_EDITABLE_PARAMS`; API key status badge in `settings.html`; update `web/dashboard.py` `_run_chat_loop` + `evals/run_evals.py` call-sites |
| 76 | Billing guard: DB migration v15 `cloud_spend` table + helpers + `BillingCapError`; `CARD_TYPE_CLOUD_ESCALATION`; escalation card + re-run with `ClaudeAPIClient` on approval; `tests/test_cloud.py` |

---

### Tests (`tests/test_cloud.py`)

- `ClaudeAPIClient` with fake Anthropic responses — no real API calls; `FakeAnthropicClient` stub
- Tool schema adapter: verify `parameters` → `input_schema` and envelope stripping
- Response normalizer: verify content blocks → `tool_calls` list
- History translator: verify accumulated Ollama-shaped history translates to Anthropic format
- Billing cap: `BillingCapError` raised when per-incident cap exceeded; same for daily cap
- `make_llm_client()`: returns `OllamaClient` for `"local"` and `"both"`; returns `ClaudeAPIClient` for `"cloud"`
- `TestConfigDefaults` entries for all five new config keys (`LLM_PROVIDER`, `CLOUD_MODEL`, `CLOUD_MAX_COST_PER_INCIDENT_USD`, `CLOUD_MAX_DAILY_SPEND_USD`, `ANTHROPIC_API_KEY`)

---

### Done when

- `LLM_PROVIDER = "local"` (default): all call-sites use `OllamaClient`; `anthropic` SDK not imported at runtime
- `LLM_PROVIDER = "cloud"`, `ANTHROPIC_API_KEY` absent: startup raises with a clear message
- `LLM_PROVIDER = "cloud"`, key present: `make_llm_client()` returns `ClaudeAPIClient`; agent loop completes a full tool-calling cycle
- `LLM_PROVIDER = "both"` + local loop exhaustion: `CARD_TYPE_CLOUD_ESCALATION` card appears in dashboard; approve re-runs with Claude
- Billing caps enforced: `BillingCapError` blocks API call when per-incident or daily cap exceeded
- `ANTHROPIC_API_KEY` string literal in `config.yaml` → startup raises
- Dashboard `LLM Provider` group: provider dropdown, cloud model, billing thresholds, API key status badge all functional
- `setup.sh` with `cloud` choice: skips inference model pull, prints API key export instructions
- `pytest --cov --cov-fail-under=90` passes
