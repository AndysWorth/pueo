# ADR 003 — Structured LLM output via Pydantic schemas

## Status
Accepted

## Context
LLM outputs are strings by default. Parsing free-text responses for structured data (severity levels, boolean flags, YAML snippets) is brittle and non-deterministic. The agent needs to act on LLM decisions programmatically without string parsing.

## Decision
All Ollama calls use `format=PydanticModel.model_json_schema()` to instruct the model to return valid JSON matching the schema, and `temperature=0.0` for deterministic output. The response is immediately validated with `PydanticModel.model_validate_json()`, which raises on schema violations.

Each agent layer defines its own response schema:
- `DiagnosticsReport` — config analysis (valid/invalid, severity, fix YAML)
- `LogEvaluation` — log triage (actionable, root cause, confidence score)

## Consequences
- Ollama's structured output mode is required; this rules out models that don't support it.
- `ollama.chat` is synchronous and must always be wrapped in `asyncio.to_thread()` to avoid blocking the event loop.
- If the model returns malformed JSON (rare but possible), `model_validate_json` raises and the pipeline logs the error rather than acting on garbage data.
- Adding a new agent capability means defining a new Pydantic schema first — this is intentional as it forces explicit design of the data contract before the prompt.
