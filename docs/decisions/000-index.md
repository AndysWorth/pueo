# Architecture Decision Records — Index

One row per ADR. Open the linked file for context, rationale, and consequences.

| ADR | Title | Status | Summary |
|-----|-------|--------|---------|
| [001](001-config-centralization.md) | Config centralization | Accepted | `config.py` is the single source; triple-update rule for adding keys |
| [002](002-safety-invariant.md) | Safety invariant | Accepted | backup-before-write ordering is inviolable; no bypass |
| [003](003-structured-llm-output.md) | Structured LLM output | Accepted | Tool-calling is primary; Pydantic + `format=` for one-shot pre-filters |
| [004](004-ssh-known-hosts-none.md) | SSH known_hosts=None | Accepted | Intentional for trusted local-network HA hosts |
| [005](005-asyncio-over-agentic-framework.md) | asyncio over agentic framework | Accepted | Direct asyncio; no LangChain/LangGraph dependency |
| [006](006-llm-provider-abstraction.md) | LLM provider abstraction | Accepted | `LLMClientProtocol`; `make_llm_client()` factory; `LLM_PROVIDER` config |
| [007](007-agent-code-proposals.md) | Agent code proposals | Accepted | `propose_patch` + `sandbox_code` flow; `_SAFETY_CRITICAL_PATHS` block list |
| [008](008-external-resolution-detection.md) | External resolution detection | Accepted | Two-poll confirmation before marking an externally resolved condition |
| [009](009-transparency-principle.md) | Transparency principle | Accepted | Live tool-call trace in Chat; per-step event timeline as audit log |
| [010](010-agent-self-awareness.md) | Agent self-awareness | Accepted | `read_source` registered in all four tool registries |
| [011](011-ha-live-lookup.md) | HA live lookup | Accepted | `fetch_ha_docs`; GitHub raw source; cache-only in `LLM_PROVIDER=local` mode |
| [012](012-hypothesis-driven-repair.md) | Hypothesis-driven repair | Superseded | Superseded by ADR 018 (5-phase cycle expanded to 6) |
| [013](013-prompt-externalization.md) | Prompt externalization | Accepted | All system prompts in `prompts/` loaded via `importlib.resources` |
| [014](014-episodic-context-injection.md) | Episodic context injection | Superseded | Superseded by ADR 018 (ADR 031 completes the implementation intent) |
| [015](015-llm-guided-disk-recovery.md) | LLM-guided disk recovery | Accepted | Disk space enforcement via `AgentLoop`; `finish_investigation` terminal tool |
| [016](016-diagnostic-wan-fetch.md) | Diagnostic WAN fetch | Accepted | `fetch_url` read-only HTTP GET; RFC-1918 block list enforced |
| [017](017-chat-tool-parity.md) | Chat tool parity | Accepted | Shared enrichment functions receive identical clients in chat and automated pipelines |
| [018](018-unified-agent-methodology.md) | Unified agent methodology | Accepted | 6-phase investigation cycle; `save_runbook`; COLLECTIONS=7 |
| [019](019-tool-calling-loop.md) | Tool-calling loop | Accepted | `AgentLoop`; budget accounting; `finish_*` terminal tool pattern |
| [020](020-outcome-primacy.md) | Outcome primacy | Accepted | Agent stops on genuine exhaustion; gap runbook required before `outcome=failed` |
| [021](021-dashboard-entity-monitor.md) | Dashboard entity monitor | Accepted | `ha_lovelace_monitor.py`; benign suppression; `should_send_card` gate |
| [022](022-adaptive-llm-timeout.md) | Adaptive LLM timeout | Accepted | P95-based per-call timeout; `llm_calls` SQLite table; `llm_stats.expected_timeout_ms()` |
| [023](023-external-api-resilience.md) | External API resilience | Accepted | Schema drift detection; `.get()` + structured log for required fields |
| [024](024-debug-level-system.md) | Debug level system | Accepted | `DEBUG_LEVEL` integer 0–3; debug episode recording and replay |
| [025](025-serialized-work-queue.md) | Serialized work queue | Accepted | `PueoWorkQueue`; `WorkItem`; priority levels; dedup key |
| [026](026-no-concurrent-llm-ha.md) | No concurrent LLM/HA | Accepted | One LLM call and one HA write at a time; enforced via `PueoWorkQueue` |
| [027](027-model-capability-configuration.md) | Model capability configuration | Accepted | `derive_call_options()`; `think=False` in production; dynamic model scoring |
| [028](028-pueo-mcp-server.md) | Pueo MCP server | Accepted | Read-heavy tool subset on `MCP_PORT`; no write/repair tools via MCP |
| [029](029-hybrid-retrieval.md) | Hybrid retrieval | Accepted | BM25+cosine hybrid; `RAG_HYBRID_WEIGHT=0.3` (70% cosine + 30% BM25) |
| [030](030-authority-ranked-knowledge.md) | Authority-ranked knowledge | Accepted | `authority_score` tiers (1.0→0.30); version-aware boosting; `query_type` routing |
| [031](031-repair-episode-embedding.md) | Repair episode embedding | Accepted | `repair_history` ChromaDB collection; `embed_repair_episodes()` at rag-refresh step 7 |
| [032](032-ha-mcp-consumption.md) | HA capability gaps: native REST/WS | Accepted | MCP adapter rejected; all 8 gaps closed via native REST/WS extension + `HAEventSubscriber` |
| [033](033-ha-event-triggers.md) | Event-driven HA triggers | Accepted | `supervisor.wake()`; non-cancelling sleep interrupt; subscriber→wake map; fallback interval |
| [034](034-opportunistic-hacs-companion-integrations.md) | Opportunistic HACS companion integrations | Accepted | Detect via profile; graceful absence; third-party output as unverified evidence; blocklist companion-added destructive services |
| [035](035-full-autonomous-confidence-gate.md) | Full-autonomous level + level renumber | Accepted | Renumber 1–4 → 0–3; add level 4 FULL_AUTONOMOUS with LLM confidence gate for CRITICAL updates; bypass in `_finish_update_analysis()` not AutonomyGate; breaking change for existing level-2 users |
| [036](036-openai-compatible-provider.md) | OpenAI-compatible LLM provider | Accepted | `LLM_PROVIDER=openai_compat`; `httpx`-based client; history translation in client (AgentLoop unchanged); `response_format=json_object` for `chat()`; no new dependency |
| [037](037-automation-authoring.md) | Automation authoring safety chain | Accepted | `propose_automation` chat-only tool; WS `validate_config` pre-approval; `CARD_TYPE_AUTOMATION_CREATE` (MEDIUM); backup → REST write → reload → verify; `pueo_auto_` prefix; rollback via DELETE |
