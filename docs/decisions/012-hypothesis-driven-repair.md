# ADR 012 — Hypothesis-driven repair cycle and query-first investigation

## Status
Accepted

## Context
The original `_AGENT_LOOP_SYSTEM_PROMPT` was seven lines: "read config/logs, call apply_fix, call finish_repair." This symptom-driven flow caused three recurring problems:

1. **Premature fixes.** The model would call `apply_fix` after reading a single file, without verifying the root cause.
2. **Knowledge base ignored.** `QUERY_KNOWLEDGE` was registered in the HA tool registry but never mentioned in the prompt. Evals showed the model rarely called it unless the log line explicitly named a known integration.
3. **No self-verification.** The model proposed YAML and immediately applied it with no reflection step. `_review_limit()` critiqued budget usage after the fact; nothing critiqued fix correctness before `apply_fix` was called.

The `utils/investigation_loop.py` module (built for Milestone 11) already demonstrated a better pattern: form a hypothesis, gather evidence to test it, confirm root cause, propose a targeted fix. That pattern was isolated to the investigation loop and never applied to the main repair prompt.

Small 7B local models benefit disproportionately from explicit structure in the system prompt. Without it they default to reactive, first-match behaviour. Adding structure costs no tokens and no extra LLM calls.

## Decision
Restructure `_AGENT_LOOP_SYSTEM_PROMPT` (now loaded from `prompts/agent_loop_ha.md`) around a five-phase hypothesis-driven investigation cycle:

```
1. Form a hypothesis — state it in one sentence before calling any tool
2. Query knowledge first — call query_knowledge to check for known breaking changes
3. Gather evidence — read_config / read_logs / read_file to confirm or refute the hypothesis
4. State root cause — write a one-sentence root cause statement before calling apply_fix
5. Verify — call verify_fix after apply_fix; call finish_repair with outcome
```

The prompt also explicitly names `query_knowledge` in the recommended flow: "For integration-related issues, call query_knowledge first before examining config."

For repair cards surfaced to the user, `poll_for_repairs()` now calls a single-shot LLM pass (`RepairIssueAnalysis`) before building the card. This replaces the previous keyword-match classification and ensures the card body contains a plain-English explanation and recommended-action rationale rather than a raw HA `translation_key` string.

## Rationale
ReAct (Reasoning + Acting) research consistently shows that explicit reasoning traces before each tool call improve small-model performance. Stating a hypothesis forces the model to commit to a theory before gathering evidence, which reduces both premature action and aimless tool calls. The self-critique nudge ("state root cause before apply_fix") costs zero tokens and catches the most common failure mode: fixing a symptom without identifying the cause.

The `query_knowledge` prompt addition closes the loop between the RAG infrastructure (built in Phase 15) and its use during repair. Infrastructure that is never mentioned in the prompt is effectively invisible to the model.

The LLM explanation on repair cards extends the same principle to user-visible output: the model that understands the failure domain should generate the human explanation, not a keyword regex.

## Consequences
- `_AGENT_LOOP_SYSTEM_PROMPT` is now in `prompts/agent_loop_ha.md`; loaded at agent loop startup via `load_prompt()`
- The repair loop prompt is longer but more precise; budget accounting is unchanged (the hypothesis text is part of assistant turns, not additional LLM calls)
- `RepairIssueAnalysis` Pydantic schema is the structured output for single-shot repair card enrichment; a safe default is returned on LLM failure so the card still surfaces
- Evals must include at least one scenario that verifies `query_knowledge` is called before `read_config` when the log line mentions a known integration
- The old inline `_AGENT_LOOP_SYSTEM_PROMPT` string in `agent_loop.py` is removed; `load_prompt("agent_loop_ha")` is the call-site

## Related decisions
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): `RepairIssueAnalysis` follows the same Pydantic schema + temperature=0 pattern.
- [ADR 013 — Prompt externalization](013-prompt-externalization.md): the prompt move to `prompts/agent_loop_ha.md` is specified separately.
- [ADR 014 — Episodic context injection](014-episodic-context-injection.md): episodic context is prepended to the initial message, not the system prompt; the two changes compose cleanly.
