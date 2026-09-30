# Pueo — Development Roadmap (Summary)

> This is a compact extract for per-session Claude Code context. For historical milestone
> specs and per-PR delivery notes, see [docs/roadmap.md](roadmap.md).

## Milestone Status

All milestones are complete as of 2026-09-16. New work is tracked in
[GitHub Issues](https://github.com/AndysWorth/pueo/issues).

| Milestone                                     | Status                  | Code Location                              |
| --------------------------------------------- | ----------------------- | ------------------------------------------ |
| 1. Read-only ingestion & diagnostics          | ✅ Complete              | `ha_agent_core.py`                         |
| 2. Local RAG & knowledge ingestion            | ✅ Complete (2026-07-28) | `utils/knowledge_store.py`, `utils/ha_release_notes_scraper.py`, `utils/hacs_scraper.py` |
| 3. Safe execution / shadow mode               | ✅ Complete              | `ha_agent_sandbox_engine.py`               |
| 4. Closed-loop autonomous healing             | ✅ Complete              | `ha_agent_sandbox_engine.py`               |
| 4.5. HA Resource Stewardship                  | ✅ Complete (2026-07-27) | `ha_agent_advanced.py`, `web/dashboard.py` |
| 4.6. HA Update Manager                        | ✅ Complete (2026-07-27) | `utils/ha_rest_client.py`                  |
| 4.7. HA Notification Intelligence             | ✅ Complete (2026-07-27) | `utils/ha_ws_client.py`, `web/dashboard.py`|
| 4.8. HA Repairs & Update Orchestration        | ✅ Complete (2026-08-05) | `ha_update_manager.py`, `ha_log_monitor.py`|
| 5. Agent quality & evaluation                 | ✅ Complete (2026-07-28) | `evals/`                                   |
| 6. Tool-calling agent loop                    | ✅ Complete (2026-07-28) | `utils/agent_loop.py`                      |
| 6.5. Supervisor + Active Dashboard            | ✅ Complete (2026-07-30) | `main.py`, `web/dashboard.py`              |
| 6.6. Conversational Agent                     | ✅ Complete (2026-07-31) | `web/templates/chat.html`, `utils/tool_executor.py` |
| 7. Configurable LLM Provider + Cloud Escalation | ✅ Complete (2026-08-07) | `utils/cloud_client.py`, `utils/llm_factory.py`, `utils/billing.py` |
| 8. Repair episode recording                   | ✅ Complete (2026-08-10) | `utils/repair_episode.py`, `utils/anonymizer.py` |
| 9. Federated case library                     | ♻️ Superseded (2026-09-05) — runbooks are the single KB signal (see [pueo-kb](https://github.com/AndysWorth/pueo-kb)) | — |
| 10. Self-improving code proposals *(stretch)* | ✅ Complete (2026-08-11) | `utils/tool_executor.py`, `utils/agent_loop.py` |
| 11. Transparent operation                     | ✅ Complete (2026-08-18) | `utils/agent_loop.py`, `web/dashboard.py`, `web/templates/chat.html`, `web/templates/overview.html` |
| 12. Agent self-knowledge + HA live lookup     | ✅ Complete (2026-08-18) | `utils/tool_registry.py`, `utils/tool_executor.py`, `utils/ha_docs_scraper.py`, `utils/agent_loop.py` |
| 13. Unified Agent Methodology                 | ✅ Complete (2026-08-24) | `prompts/agent_loop.md`, `utils/knowledge/strategy_seeder.py`, `utils/agent/tool_registry.py` |
| 14. Knowledge Quality                         | ✅ Complete (2026-09-16) | `utils/knowledge/`, `utils/agent/tool_executor.py` |

### Implementation Phases

Tactical delivery batches in execution order. See `docs/implementation-plan.md` for item-level detail.

| Phase                                              | Status                  | Items   |
| -------------------------------------------------- | ----------------------- | ------- |
| Phase 1–3: Foundation, Observability, Architecture | ✅ Complete (2026-07-15) | 1–9     |
| Phase 3.5: Autonomy Control                        | ✅ Complete (2026-07-19) | 9.5     |
| Phase 4: NetAlertX Integration                     | ✅ Complete (2026-07-20) | 10–19   |
| Phase 4.5: Approval Web Dashboard                  | ✅ Complete (2026-07-20) | 19.5    |
| Phase 5: Observability UX                          | ✅ Complete (2026-07-20) | 20      |
| Phase 6: Installer Intelligence                    | ✅ Complete (2026-07-21) | 21–22   |
| Phase 7: Evidence Capture & Approval Display       | ✅ Complete (2026-07-21) | 23–24   |
| Phase 8: NetAlertX Compatibility Maintenance       | ✅ Complete (2026-07-21) | 25      |
| Phase 9: NetAlertX One-Shot Diagnosis              | ✅ Complete (2026-07-22) | 27      |
| Phase 11: Resource Stewardship                     | ✅ Complete (2026-07-27) | 28–32   |
| Phase 12: HA Update Manager                        | ✅ Complete (2026-07-27) | 33–37   |
| Phase 12.5: HA Repairs & Update Orchestration      | ✅ Complete (2026-08-05) | 12.5A–B |
| Phase 13: HA Notification Intelligence             | ✅ Complete (2026-07-27) | 38–41   |
| Phase 14: Tool-Calling Agent Loop                  | ✅ Complete (2026-07-28) | 42–48   |
| Phase 15: RAG Knowledge Layer                      | ✅ Complete (2026-07-28) | 49–52   |
| Phase 16: Evals                                    | ✅ Complete (2026-07-28) | 53–54   |
| Phase 17: Supervisor + Active Dashboard            | ✅ Complete (2026-07-30) | 55–64   |
| Phase 17.5: Conversational Agent                   | ✅ Complete (2026-07-31) | 65–72   |
| Phase 18: Configurable LLM Provider + Cloud Escalation | ✅ Complete (2026-08-07) | 73–76   |
| Phase 19: Repair Episode Recording                 | ✅ Complete (2026-08-10) | 77–79   |
| Phase 20: Federated Case Library                   | ♻️ Superseded (2026-09-05) — runbooks are the single KB signal (see [pueo-kb](https://github.com/AndysWorth/pueo-kb)) | 80–82   |
| Phase 21: Code Proposals *(stretch)*               | ✅ Complete (2026-08-11) | 83–86   |
| Phase 22: HA RAG Strategy                          | ✅ Complete (2026-08-06) | 87–93   |
| Phase 23: Disk Usage Tab                           | ✅ Complete (2026-08-07) | DU-1–6  |

---

## Evaluation Matrix

These constraints govern all ongoing development. Evaluate every new feature against them before merging.

| Constraint | Target | Mitigation if failing |
|---|---|---|
| Inference latency | Per-call timeout = P95(historical latency) × `AGENT_PER_CALL_TIMEOUT_FACTOR` (default 5×), floor 5 min, ceiling 30 min. No hard per-step latency target — correctness prioritized over speed. See ADR 022. | Quantize model to `q4_K_M`; offload embedding layers to Apple Silicon AMX |
| Config hallucination | Zero on inputs up to 8,000 tokens | Sliding window log ingestion; pass only relevant config sections, not full directories |
| Un-backed writes | 0% — no production write without a confirmed backup slug | `execute_remote_backup()` raises on failure; pipeline aborts |
| LLM inference location | Configurable: `local` (Ollama, default), `cloud` (Anthropic API), or `both` | Set via `LLM_PROVIDER`; cloud and both require `ANTHROPIC_API_KEY` env var; billing caps enforced; WAN only via the designated provider |
| WAN during autonomous fix cycles | 0 when `LLM_PROVIDER=local` (default) | Cloud mode intentionally sends inference traffic to Anthropic; autonomous cycles in `both` mode still use local Ollama |
| HA disk free | ≥ `HA_DISK_CRITICAL_GB` at all times | Block backup trigger + offload older backups automatically before new backup fires. Note: the HA Supervisor independently hard-blocks **all** operations (including `ha backups new`) when free space < 1 GB — Pueo's threshold must remain above `1 GB + largest expected backup size` or the Supervisor will block Pueo's own backup before Pueo's guard can act. Default is 3.0 GB (2 GB above the Supervisor's floor). |
| Backup location | 100% of slugs confirmed on Pueo before deleting from HA | SHA-256 gate; `location = 'both'` required before any HA-side delete |
| Tool loop budget | ≤ 20 tool calls per incident | Hard cap in `AgentLoop`; exhaustion triggers escalation offer, not silent failure |
| Loop wall time | No outer wall-clock guard — replaced by per-call timeout (see Inference latency row). The loop runs until its tool-call budget is exhausted or a call stalls beyond the adaptive threshold (`outcome = "stuck"`). | — |
| Local fix rate | ≥ 80% resolved without cloud escalation | Tune tool count + model size if falling below; cloud escalation is the fallback |
| Episode coverage | 100% of successful repairs recorded | `finish_repair` tool fires serialization unconditionally |
| Cloud spend | Per-incident cap ($0.50) + daily cap ($5.00) | `BillingCapError` before each API call; tracked in `cloud_spend` SQLite table; caps configurable in UI |
| HA knowledge currency | Installed integrations pre-cached at every RAG refresh; unknown domains fetchable on demand in `cloud`/`both` mode | Run RAG refresh after adding new integrations; check `.cache/ha_source/` for missing domains |
| WAN during inference (HA lookup) | 0 when `LLM_PROVIDER=local` | `fetch_ha_docs` serves from cache only in local mode; raises `ToolError` on miss — never makes a network call |
| Runbook coverage | Known query classes have a seed runbook; any session that fails or finds nothing in the KB saves a gap runbook | Add seed runbooks for new query classes; gap runbooks surface to developers via dashboard runbook review UI |

---

## Architectural Notes

**Framework choice:** The original plan specified LangGraph or CrewAI as the agentic framework. Plain `asyncio` was chosen instead — the current state machine is simple enough that a full framework would add dependency weight without benefit. Revisit if the system grows to require multi-agent coordination or complex branching state graphs.

**LLM-guided all actions:** LLM inference is the intended reasoning layer for every significant Pueo action — repair, update, cleanup, notification triage, code proposals. Infrastructure operations that bypass the LLM (scheduled scraper runs, disk-space enforcement, backup retention sweeps) are housekeeping, not decisions. The boundary rule: if a Pueo function changes HA state or makes a judgment call about what to do next, it belongs in an agent loop, not a direct call.
