# ADR 003 — Structured LLM output via Pydantic schemas

## Status
Accepted

## Context
LLM outputs are strings by default. Parsing free-text responses for structured data (severity levels, boolean flags, YAML snippets) is brittle and non-deterministic. The agent needs to act on LLM decisions programmatically without string parsing.

## Decision
All significant Pueo reasoning flows through `AgentLoop` using Ollama's (or Anthropic's) **tool-calling API** (`chat_with_tools`). The model iterates over tool calls until it reaches a confident conclusion or exhausts its budget — this is the primary LLM interaction pattern (see ADR 019).

For one-shot pre-filter calls that do not benefit from iteration (streaming log line triage, notification analysis, breaking-change text analysis), `format=PydanticModel.model_json_schema()` with `temperature=0.0` forces deterministic, parseable JSON validated by `PydanticModel.model_validate_json()`.

Pydantic schemas are used throughout: as `format=` targets for one-shot calls, and as the structured inputs/outputs for tool definitions. The original two schemas (`DiagnosticsReport`, `LogEvaluation`) remain for the one-shot paths in `ha_agent_core.py` and `ha_log_monitor.py`; the full codebase now has dozens of schemas.

## Consequences
- One-shot `format=` calls require a model with Ollama structured-output support; the tool-calling path does not depend on `format=`.
- The `asyncio.to_thread()` wrapper applies to the synchronous `ollama.chat()` calls used in one-shot paths. `AgentLoop.run()` is async throughout — no wrapping needed for the tool-calling path.
- If the model returns malformed JSON (rare but possible), `model_validate_json` raises and the pipeline logs the error rather than acting on garbage data.
- Adding a new agent capability means defining a new Pydantic schema first — this is intentional as it forces explicit design of the data contract before the prompt.
- All Pydantic schemas are defined once and imported where needed — never duplicated across agent modules. Divergent field descriptions produce different `model_json_schema()` output, causing inconsistent Ollama behavior.

## Related decisions
- [ADR 001 — Config centralization](001-config-centralization.md): `OLLAMA_MODEL` and `OLLAMA_ENDPOINT` (from `config.py`) are the single source of model identity for all structured output calls.
- [ADR 002 — Safety invariant](002-safety-invariant.md): `DiagnosticsReport.is_valid = False` is what triggers the backup-before-write chain; structured output correctness is a prerequisite for the safety invariant to fire reliably.
