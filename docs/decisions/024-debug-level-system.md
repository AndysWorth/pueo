# ADR 024 — DEBUG_LEVEL 0–3 replaces DEBUG_MODE/DEBUG_VERBOSE booleans

## Status
Accepted

## Context
The previous debug system used two independent boolean flags: `DEBUG_MODE` (enable debug logging) and `DEBUG_VERBOSE` (disable truncation). These were awkward for two reasons:

1. **Four states, two knobs.** There was no natural way to express "I want per-step LLM detail but not raw 16KB payloads" — `DEBUG_VERBOSE` implicitly required `DEBUG_MODE` to be meaningful, but nothing enforced this.
2. **Episode capture was only for chat.** `AgentLoop` had a `capture_llm` parameter that was `True` only in chat sessions. Autonomous repair, NetAlertX healing, disk recovery, and update-manager sessions produced no episode HTML, making it impossible to inspect what the model actually sent and received during a production incident.
3. **One-shot LLM calls were invisible.** Log triage, notification analysis, breaking-change analysis, and repair-issue classification all made direct `chat_with_tools` calls outside of `AgentLoop`. No instrumentation existed for them.

## Decision
Replace `DEBUG_MODE` and `DEBUG_VERBOSE` with a single integer `DEBUG_LEVEL` (0–3):

| Level | Logging | Episode capture | Truncation |
|-------|---------|-----------------|------------|
| 0 | INFO only | Off | On |
| 1 | DEBUG: per-step summaries | On (all sessions) | On |
| 2 | DEBUG + full payloads | On (all sessions) | Off |
| 3 | DEBUG + full payloads + RAG chunks | On (all sessions) | Off |

**Backward compatibility:** `DEBUG_MODE` and `DEBUG_VERBOSE` are computed properties in `config.py` (`DEBUG_MODE = DEBUG_LEVEL >= 1`, `DEBUG_VERBOSE = DEBUG_LEVEL >= 2`) so existing callers continue to work without changes.

### Per-level log statements added

- **L1 — `ssh_client.py`**: `ssh_run` (command[:100]) — what SSH command fired
- **L1 — `agent_loop.py`**: `llm_call_detail` (model, prompt tokens), `llm_hypothesis` (first assistant turn), `tool_call` (name + summary args)
- **L2 — `agent_loop.py`**: `tool_call_full` (complete args and result)
- **L3 — `agent_loop.py`**: `knowledge_chunks` + `knowledge_pre_inject` (full RAG content)
- **L2 — `ssh_client.py`**: `ssh_run_output` (stdout[:500])
- **L1 — `ha_agent_sandbox_engine.py`**: `sandbox_preflight_result`
- **L2 — `ha_agent_sandbox_engine.py`**: `sandbox_preflight_output`
- **L1 — `ha_log_monitor.py`**: `log_triage_result`
- **L1 — `ha_notification_manager.py`**: `notification_enriched`
- **L1 — `ha_update_manager.py`**: `breaking_change_analysis`

### Episode capture for all AgentLoop sessions

`capture_llm=True` is now set at all 9 non-chat `AgentLoop` call sites (HA repair, NetAlertX healing, disk recovery investigation, update manager, notification investigation, etc.). Episode HTML is written by `AgentLoop.run()` unconditionally when `DEBUG_LEVEL >= 1`. Old episode files are rotated by `rotate_old_episodes()` after `DEBUG_EPISODE_RETENTION_DAYS` (default: 7).

### One-shot LLM call recording

A new `llm_one_shot_calls` table (V33 migration) captures every direct LLM call that occurs outside of `AgentLoop`. `record_one_shot()` in `utils/debug/capture.py` writes a row with: mode, model, prompt/response snippets (truncated when `DEBUG_LEVEL < 2`), latency, and timestamp. All 11 one-shot sites are instrumented.

### Dashboard changes

`/api/debug-mode` POST/GET now returns `{"enabled": bool, "verbose": bool, "level": int}` — backward-compatible with all existing callers. The pill in `base.html` shows `[DBG:N]` for `N >= 1` instead of the previous binary `Debug: on/off`. The settings editor exposes `debug_level` as an integer field (0–3) replacing the two boolean toggles.

## Consequences

- `DEBUG_MODE` and `DEBUG_VERBOSE` remain valid everywhere they were used; no call-site changes needed.
- Users upgrading from a `config.yaml` with `debug_mode: true` will have that key ignored silently — the new `debug_level: 1` key must be set explicitly. `setup.sh` handles this for fresh installs.
- Episode capture at L1 means every agent session produces an HTML file. The `DEBUG_EPISODE_RETENTION_DAYS` and `rotate_old_episodes()` cleanup ensure disk usage stays bounded.
- One-shot table rows at L0 are written only when `DEBUG_LEVEL >= 1` (the function is a no-op otherwise), so production systems at the default level see no overhead.
- V33 migration adds `llm_one_shot_calls` to both `ha_agent_advanced.py` and `ha_agent_sandbox_engine.py` (dual-migration pattern per project convention).

## Related decisions
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): one-shot calls all use Pydantic schema enforcement; `record_one_shot()` stores both the raw prompt and the validated response.
- [ADR 005 — asyncio over agentic framework](005-asyncio-over-agentic-framework.md): `record_one_shot()` is called via `asyncio.create_task(asyncio.to_thread(...))` to avoid blocking event loop.
- [ADR 018 — Unified agent methodology](018-unified-agent-methodology.md): the 6-phase cycle's hypothesis and evidence-gathering steps are now visible at L1 via `llm_hypothesis` and `tool_call` log events.
