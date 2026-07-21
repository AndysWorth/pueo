# ADR 005 — Plain `asyncio` over LangGraph / CrewAI

## Status
Accepted

## Context
The original Pueo plan specified LangGraph or CrewAI as the agentic orchestration framework. Both provide multi-agent coordination, state graph management, tool routing, and memory abstractions. Pueo's architecture is a layered pipeline with a linear repair flow and a single continuous monitoring loop.

## Decision
Use plain `asyncio` for all orchestration. No LangGraph, CrewAI, or other agentic framework dependency.

## Rationale
- Pueo's state machine is simple: fetch → diagnose → gate → backup → sandbox → swap. This is a linear pipeline, not a multi-agent graph.
- The only branching is at the autonomy gate (approve / reject / auto-proceed) and the sandbox result (pass / fail). Both are two-branch conditionals, not complex graph traversals.
- Adding LangGraph or CrewAI would introduce ~30–50 transitive dependencies, a framework-specific mental model for contributors, and framework version coupling — in exchange for abstractions Pueo does not use.
- `asyncio.create_task()` satisfies Pueo's concurrency requirements: the HITL dashboard runs alongside monitoring loops without blocking.

## Consequences
- If Pueo grows to require true multi-agent coordination (e.g., separate diagnosis, planning, and execution agents that communicate asynchronously), this decision should be revisited. The trigger is: more than one LLM call per repair cycle, or a need for agent-to-agent message passing.
- Custom retry, rate limiting, and state persistence (currently in `utils/retry.py`, `utils/rate_limiter.py`, and SQLite) would be replaced or supplemented by framework primitives on migration. The current `asyncio` code provides a clean migration surface because each concern is isolated in its own utility module.

## Related decisions
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): Pydantic schemas and `asyncio.to_thread()` wrappers around `ollama.chat` provide the structured LLM integration that a framework would otherwise supply.
