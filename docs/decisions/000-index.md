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
| [038](038-runbook-lifecycle.md) | Runbook lifecycle: signature-keyed, distilled, evidence-validated | Accepted | Remove `save_runbook`; symptom signatures; post-session distillation WorkItem; `validated` state; gaps → `knowledge_gaps` (never retrieved); `runbook_usage` table; updated authority tiers. Supersedes runbook/learning sections of ADR 018 and runbook tier rows of ADR 030. |
| [039](039-federated-validation.md) | Federated runbook validation via pueo-kb | Accepted | Instance id; `kb_id = rb_+sha256(sig)[:12]`; evidence files in PRs; CI owns MANIFEST.json + promotion (≥3 successes, ≥2 instances, 0 failures); candidates ingest at 0.45 `[COMMUNITY CANDIDATE]`; validated/seed at 0.70 `[COMMUNITY RUNBOOK]`; flagged skipped; Sybil risk documented |
| [040](040-gated-ha-cli-commands.md) | Gated HA CLI commands | Accepted | `_HA_COMMAND_RISK` map in `tool_executor.py`; state-changing commands (`ha core restart` MEDIUM, `ha core stop` HIGH, `ha supervisor repair` MEDIUM) route through `gate.queue_for_approval` → `CARD_TYPE_HA_COMMAND`; `_execute_ha_command` re-validates at execution time; no backup required (no config file written) |
| [041](041-host-health-triggers.md) | Host-health triggers | Accepted | `_is_ha_command_allowed()` + `_ha_command_risk()` replace exact-match dict for parameterized gated commands: `ha resolution suggestion apply <uuid>` MEDIUM, `ha resolution issue dismiss <uuid>` LOW, `ha apps restart <slug>` MEDIUM, `ha resolution healthcheck` LOW; `_UUID_RE` / `_SLUG_RE` prevent injection |
| [042](042-ha-sidebar-icon.md) | HA sidebar: custom iconset, pixel-traced PNG, `extra_module_url` | Accepted | `pueo:owl` custom iconset from pixel-traced `nav_32.svg`; `frontend: extra_module_url` loads JS globally (not Lovelace resource); `add_extra_module_url` text-level edit preserves `!include`/`!secret`; setup-time write under backup chain; `setup.sh` section 5.6 |
| [043](043-read-only-investigation-ssh.md) | Read-only SSH for autonomous investigations | Accepted | `ReadOnlySSHClient` replaces `_NullSSH` in repair-issue/notification/lovelace investigations; allowlisted non-gated `ha` commands + fixed log commands only; reads limited to `/config/`,`/backup/` minus secrets/auth; writes raise; `run_ha_command` reports `exit N:` errors |
| [044](044-verified-learning.md) | Verified learning | Accepted (§3 superseded by 046) | Per-symptom seeds with wildcard signatures; distill only after verification (success or failure+correction); card actions count as verification; `_MIN_EVIDENCE_CALLS` removed; `no_new_knowledge` flag skips write; verified-only repair history embedding. Supersedes runbook lifecycle sections of ADR 038. |
| [045](045-companion-provisioning.md) | Companion provisioning | Accepted | Deterministic code (no LLM); backup → HACS download → restart → config flow → options → verify; always-approve at all levels including FULL_AUTONOMOUS; `--mode companion-setup`; no tool registration; `utils/ha/companion_setup.py` is the authority. |
| [046](046-explicit-verification.md) | Explicit verification | Accepted | "Pueo was right" (`/right/{nid}`) = user success; Approve/Acknowledge/Apply-fixes verify nothing; card executors (ha_command, service_call, config reload, automation, repair fix, manual update) are system verifiers; every card-sending session defers; `awaiting_approval` writes no gap. Supersedes ADR 044 §3. |
| [047](047-device-availability-monitor.md) | Device availability monitor | Accepted | Poll HA for offline devices every 60 s; device-level alerting with arming; `last_changed` as offline-since source; grouped by config entry; 10-min grace period; recovery not verification |
| [048](048-netalertx-network-evidence.md) | NetAlertX as network-layer evidence | Accepted | Pull over push; `build_presence_context()` resolves device MACs via WS registry + queries NetAlertX once per investigation; on-LAN vs off-LAN decision rule in seed; event poll (S3) for new-device cards only |
| [049](049-procedural-lessons.md) | Procedural distillation lane | Accepted | Tool-use lessons learned after any conclusive session without waiting for user verification; stored under `## Procedure notes` in candidate runbook; pre-injected as `[PROCEDURE NOTES]`; verdict lane unchanged (ADR 044/046) |
