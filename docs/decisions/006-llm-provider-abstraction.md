# ADR 006 — Configurable LLM Provider (local Ollama vs. cloud API)

## Status
Accepted

## Context
Pueo was originally designed with an absolute constraint: zero WAN packets during autonomous fix cycles. All LLM inference routes to a local Ollama instance. This constraint is appropriate for a privacy-first home automation system, and it remains the default.

Two problems motivated revisiting it:

1. **Capability ceiling.** Local 7B models handle common HA failure patterns well but are unreliable on novel, multi-step reasoning problems. When the tool loop exhausts its budget without a fix, the incident goes unresolved with no higher-capability fallback.

2. **Deployment flexibility.** Some users are comfortable routing inference to a cloud provider in exchange for better model quality, especially on machines where local GPU resources are limited. Hard-coding Ollama prevents this without forking the project.

The existing `LLMClientProtocol` in `interfaces.py` already defines the interface both `OllamaClient` and any cloud client must satisfy. The abstraction is in place — what was missing was a way to select between implementations at runtime.

## Decision
Make the LLM inference engine a configurable setting (`LLM_PROVIDER`) with three modes:

- `"local"` (default) — Ollama only; all existing behavior preserved; no WAN for inference
- `"cloud"` — Anthropic API as primary LLM; all inference calls go to Anthropic
- `"both"` — Ollama handles autonomous repair and monitoring cycles; Claude is available as a HITL-approved escalation when the local loop exhausts its budget

The implementation lives in two new files:
- `utils/cloud_client.py` — `ClaudeAPIClient` implementing `LLMClientProtocol`
- `utils/llm_factory.py` — `make_llm_client()` factory that reads `LLM_PROVIDER` and returns the appropriate client

All call-sites that previously fell back to `OllamaClient()` are migrated to `make_llm_client()`.

## Rationale

**The `LLMClientProtocol` boundary makes this safe.** Every agent function already accepts an optional injected `llm_client: Optional[LLMClientProtocol]`. The `"cloud"` path simply changes what the factory returns — no caller needs to know about the provider.

**`"both"` mode preserves the local-first property for autonomous cycles.** The factory returns `OllamaClient` in `"both"` mode. Claude is only invoked when a human approves a HITL escalation card — it is never called during an unattended repair cycle.

**RAG embeddings stay local in all modes.** `nomic-embed-text` via Ollama is used for vector embeddings regardless of `LLM_PROVIDER`. Embedding models are small and fast; privacy of indexed content is worth keeping local.

**Credential hygiene is non-negotiable.** `ANTHROPIC_API_KEY` is read exclusively from `os.getenv()`. The startup guard raises if it is absent when the provider requires it, and raises if the literal string appears anywhere in `config.yaml` (a plaintext file on disk is not an appropriate store for a billable credential).

## Consequences

- Adding `anthropic` to `requirements.txt` introduces a new dependency. It is only imported when `LLM_PROVIDER` is `"cloud"` or `"both"`.
- The `ClaudeAPIClient` must translate between Ollama's tool-use format and Anthropic's — the tool schema, response content blocks, and history message format all differ. This translation is entirely internal to `ClaudeAPIClient`; no other module is aware of it.
- The evaluation matrix row `"WAN packets during fix cycles | 0"` is replaced with a two-row entry reflecting the new policy: 0 WAN when `LLM_PROVIDER=local`; intentional WAN in `cloud` mode; 0 WAN for autonomous cycles in `both` mode.
- Billing caps (`CLOUD_MAX_COST_PER_INCIDENT_USD`, `CLOUD_MAX_DAILY_SPEND_USD`) are enforced in `ClaudeAPIClient` before each API call and tracked in a `cloud_spend` SQLite table. The caps are configurable in the settings UI.
- `setup.sh` asks for provider preference and conditionally skips the Ollama inference model pull when `cloud` is chosen. Ollama is still installed in all modes for RAG embeddings.

## Related decisions
- [ADR 001 — Config centralization](001-config-centralization.md): `LLM_PROVIDER`, `CLOUD_MODEL`, and billing keys follow the triple-update rule (`config.py` + `config.yaml.default` + `setup.sh`); `ANTHROPIC_API_KEY` follows the env-var-only credential pattern established in `ha-update-manager.md`.
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): `ClaudeAPIClient.chat()` achieves structured output via tool-forcing (Anthropic's equivalent of Ollama's `format` parameter), keeping the Pydantic schema contract intact for all callers.
- [ADR 005 — asyncio over agentic framework](005-asyncio-over-agentic-framework.md): no framework change required; `AgentLoop` is provider-agnostic because it only calls `LLMClientProtocol` methods.
