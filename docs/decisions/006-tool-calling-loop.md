# ADR 006 — Tool-calling agent loop over linear pipeline

## Status
Proposed

## Context

Pueo's current architecture is a fixed linear pipeline: gather pre-scripted evidence → one Ollama call producing a `DiagnosticsReport` → execute a predetermined action set (sandbox → atomic swap → restart). The evidence gathered and the actions available are both fixed at design time.

This works for failure modes the designer anticipated. Novel failures — unknown integration conflicts, new HAOS versions, hardware-specific issues, multi-causal problems — either go undiagnosed or produce hallucinated fixes applied to a scripted template. The agent cannot ask "what else should I look at?"

## Decision

Replace the linear pipeline with an iterative tool-calling loop using Ollama's `tools` API. The model receives an initial context, decides which tool to call, observes the result, and iterates until it calls `finish_repair` (success) or exhausts its budget (escalation offer). The tool registry is defined as Pydantic schemas in `utils/tool_registry.py` and shared across both `OllamaClient` and `ClaudeAPIClient`.

This is the same architecture as Claude Code and Devin: a language model driving a tool execution environment.

## Rationale

- An open tool-calling loop can investigate unknown failure modes; a scripted pipeline cannot
- Ollama's `tools` API supports this natively for models that implement it (Qwen 2.5 Coder 7B, Llama 3.1+)
- The existing abstractions — `SSHClientProtocol`, `LLMClientProtocol`, `AutonomyGate` — compose naturally; the loop calls the same clients and gates
- ADR 005 chose plain `asyncio` over a framework because the state machine was simple; the tool loop is still plain `asyncio` — no framework is introduced

## Consequences

**Safety invariant unchanged.** The `apply_fix` tool internally calls `execute_remote_backup()` before any write. The loop enforces `apply_fix` at most once per run. The backup-before-write chain is enforced inside the tool, not at the loop level, and cannot be bypassed by loop logic.

**Budget management required.** The loop enforces a maximum tool call count (`AGENT_MAX_TOOL_CALLS`, default 20) and a wall-clock timeout (`AGENT_MAX_WALL_SECONDS`, default 120). Exhausting the budget is a valid outcome — it triggers an escalation offer to the user (Milestone 7) rather than failing silently.

**Eval baseline required before migration.** The M5 eval baseline must be committed before the pipeline is refactored. The post-migration score must not drop. This is a hard prerequisite: without a measurement baseline, regression is invisible.

**Model capability is the bottleneck.** 7B models reliably handle 5–8 tool calls with straightforward reasoning; 10+ steps with complex branching needs larger models. The loop architecture is correct today; model capability will catch up. Milestone 7 (cloud escalation) covers the gap for incidents the local model cannot close.

**`run_ha_command` must enforce an allowlist.** The tool loop must not become arbitrary shell execution. Permitted `ha` subcommands are explicitly enumerated; anything else is rejected with a tool error before SSH is contacted.

## Related decisions

- [ADR 002 — Safety invariant](002-safety-invariant.md): `apply_fix` tool is the sole write path; backup invariant enforced inside it unchanged
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): Tool call/result schemas are Pydantic models; `finish_repair` produces a structured `AgentLoopResult`; `temperature=0.0` and schema enforcement remain
- [ADR 005 — asyncio over agentic framework](005-asyncio-over-agentic-framework.md): Loop is still plain `asyncio`; ADR 005's trigger condition (multi-agent coordination or agent-to-agent message passing) has not been reached
