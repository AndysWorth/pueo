# ADR 010 — Agent self-awareness via `read_source`

## Status
Accepted

## Context
Pueo's tool-calling agent loop decides which tools to call and in what order. When the loop encounters an unfamiliar situation — an integration it has not seen before, a failure mode that does not match its training — it needs to reason about what capabilities are available to it before choosing an action.

The `read_source` tool already exists in `utils/tool_executor.py`. It reads any file from the Pueo repository root (allowed extensions: `.py .yaml .md .toml .txt`, capped at 8,000 chars, no path traversal). It is registered in `build_chat_tool_registry()` and `build_code_proposal_registry()` but absent from `build_ha_tool_registry()` and `build_netalertx_tool_registry()` — the two registries used during autonomous repair and monitoring.

As a result, the repair agent has no way to inspect its own capabilities. It cannot read `utils/tool_registry.py` to confirm which tools are registered, examine `utils/tool_executor.py` to understand what a tool does, or verify which Pueo subsystems exist before deciding how to approach a problem.

## Decision
Register `read_source` in all agent registries:
- `build_ha_tool_registry()` — HA repair agent
- `build_netalertx_tool_registry()` — NetAlertX healer
- `build_chat_tool_registry()` — already registered
- `build_code_proposal_registry()` — already registered

Update `_AGENT_LOOP_SYSTEM_PROMPT` in `utils/agent_loop.py` to include a brief note that the agent can call `read_source` to inspect its own tool registry or pipeline code when uncertain about available capabilities.

The safety constraints on `read_source` remain unchanged:
- Safety-critical paths (`utils/autonomy.py`, `interfaces.py`, `config.py`) are readable but write-blocked by the existing `_SAFETY_CRITICAL_PATHS` list in `propose_patch`
- `read_source` is read-only — it cannot modify files
- The 8,000-char cap and allowed-extension allowlist remain

## Rationale
LLM inference is Pueo's reasoning layer for all significant actions (see ADR 011's architectural principle). For that reasoning to be grounded, the model needs to know what it can do. Exposing `read_source` universally lets the model verify its own tool list before committing to an approach, without adding any new capability surface — the tool already exists and is already used in chat sessions.

The one tool-call cost per self-inspection is negligible against the 20-call budget and is far cheaper than a loop that exhausts its budget pursuing a missing capability.

## Consequences
- All four agent registries expose `read_source`
- The repair system prompt mentions self-inspection as an option
- `build_ha_tool_registry()` and `build_netalertx_tool_registry()` each gain one line
- No new executor code required — `_read_source` is already implemented
- Tests: existing `test_tool_executor.py` coverage of `read_source` applies; confirm the two new registry factory functions include it

## Related decisions
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): `read_source` is provider-agnostic; it never makes an LLM call, so it works identically with Ollama and Claude clients.
- [ADR 007 — Agent code proposals](007-agent-code-proposals.md): `read_source` is the first step in the code-proposal pipeline; making it available in repair loops creates a natural bridge to code-proposal mode when `capability_gap=True`.
- [ADR 011 — HA live lookup](011-ha-live-lookup.md): self-awareness of Pueo's own code and awareness of HA's code are complementary capabilities delivered in the same milestone.
