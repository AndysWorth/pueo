# Pueo — Agent Orientation

Compact "where is X?" map. `CLAUDE.md` is the authoritative reference; this file points to it rather than repeating it.

## 1. Quick orientation

- **Entry point:** `main.py`; default mode `supervisor`; 24 `--mode` values (see the `choices=` list in `main.py`). Agent imports are deferred inside mode branches so `PUEO_CONFIG` is set first.
- **Runtime:** `LoopSupervisor` (`utils/agent/supervisor.py`) starts the background tasks and the dashboard. Every significant decision goes through `AgentLoop` (`utils/agent/agent_loop.py`).
- **Tools:** `utils/agent/tool_executor.py` (implementations) and `utils/agent/tool_registry.py` (definitions and per-agent registry builders).
- **Config:** `config.py` is the single source. Adding a key means updating `config.py`, `config.yaml.default`, and `setup.sh` (ADR 001).
- **Paths:** mutable directories come from `paths.get_dirs()`, never hardcoded.
- **Tests:** `pytest` (unit, no services needed); `tests/integration/` needs local services (seam tests, Ollama evals).

## 2. Agents (`agents/`)

| Agent file | Handles | Supervisor task(s) | `--mode` |
|---|---|---|---|
| `ha_agent_core.py` | Config fetch + LLM diagnosis (read-only) | — | `diagnose` |
| `ha_agent_advanced.py` | Diagnosis + SQLite memory + backup triggering | — | `advanced` |
| `ha_agent_sandbox_engine.py` | Repair: sandbox-test-then-atomic-swap | repair pipeline (triggered by log monitor) | `repair` |
| `ha_log_monitor.py` | Live log tail + AI triage; `poll_for_updates`, `poll_for_notifications`, `poll_for_repairs` | `ha_log_monitor`, `ha_log_monitor_supervisor`, `repair_poll`, `notification_poll`, `update_check` | `monitor` |
| `ha_lovelace_monitor.py` | Dashboard entity health + benign suppression | `lovelace_poll` | — |
| `ha_notification_manager.py` | Notification triage and IP enrichment | `notification_poll` | `notifications` |
| `ha_update_manager.py` | Update detection, breaking-change analysis, orchestration | `update_check` | `update-check` |

NetAlertX code lives in `netalertx/`; the dashboard in `web/`.

## 3. Utils directory map (`utils/`)

| Directory | Contents |
|---|---|
| `agent/` | `AgentLoop`, `ToolExecutor`, tool registry, `LoopSupervisor`, `PueoWorkQueue`, `AutonomyGate`, investigation loop, config analysis, tool-result guardrail |
| `core/` | Prompt loader, rate limiter/debouncer, token budget (`context.py`), structured logging, retry, timeline |
| `debug/` | Debug episode capture and HTML report writer |
| `disk/` | Hardware detection, disk recovery and usage, archiver, resource monitoring, Pueo backup storage |
| `ha/` | `HARestClient`, `HAWebSocketClient`, `HAEventSubscriber`, SSH client, HA environment profile, Lovelace utils, `service_policy.py` |
| `hitl/` | Approval card types and tracker (`hitl_suppression`), notifier, LLM trace |
| `knowledge/` | ChromaDB store (8 collections), scrapers, strategy seeder, repair-episode embedder, KB ingester/contributor |
| `llm/` | `OllamaClient`, `ClaudeAPIClient`, `make_llm_client()`, model options, latency stats, Ollama monitor |
| `mcp/` | Pueo MCP server (read-only tool subset) |
| `repair/` | Repair episode recording, anonymizer, cloud escalation, billing, YAML validator |
| `replay/` | Episode replay engine |
| `system/` | launchd service management, system audit |

## 4. Interfaces (`interfaces.py`)

Agent functions accept these optional injected clients and fall back to real ones when `None`.

| Protocol | Test double | Covers |
|---|---|---|
| `SSHClientProtocol` | `FakeSSHClient` | SSH read/write/run/stream |
| `LLMClientProtocol` | `FakeLLMClient` (`FakeToolCallingLLMClient` for loops) | `chat` / `chat_with_tools` |
| `HARestClientProtocol` | `FakeHARestClient` | HA REST API |
| `HAWebSocketClientProtocol` | `FakeHAWebSocketClient` | HA WebSocket API |
| `HAEventSubscriberProtocol` | `FakeHAEventSubscriber` | Persistent HA event subscription + ring buffer |
| `NetAlertXClientProtocol` | — (see `netalertx/`) | NetAlertX device list |
| `KnowledgeStoreClientProtocol` | `FakeKnowledgeStore` | ChromaDB upsert/query |

`FakeAutonomyGate` (`utils/agent/autonomy.py`) is the approval-gate double. `AutonomyGate` levels run 0–4: 0=report-only, 1=suggest, 2=guided(auto-LOW), 3=autonomous(auto-LOW/MED/HIGH), 4=full-autonomous(auto-all when LLM confidence ≥ threshold). The level-4 confidence bypass lives in `_finish_update_analysis()` in `utils/agent/tool_executor.py`, not inside the gate itself (ADR 035).

## 5. Prompts (`prompts/`)

A Python package of ~30 `.md` files, loaded via `utils/core/prompts.py::load_prompt()`. `agent_loop.md` is the universal 6-phase cycle; `agent_loop_{lovelace,notification,repair_issue,update_analysis}.md` are mode variants. `seed_*.md` are seed runbooks embedded at RAG refresh by `strategy_seeder.py`. Learning is automatic — post-session distillation (`utils/knowledge/runbook_distiller.py`) creates candidate runbooks without the model calling a tool. No inline prompt strings in `.py` files (ADR 013).

## 6. Common commands

| Command | Purpose |
|---|---|
| `pytest` | Unit test suite |
| `pytest tests/test_foo.py::TestBar::test_baz` | Single test |
| `black --check .` | Format check |
| `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics` | Lint (errors only) |
| `mypy --ignore-missing-imports .` | Type check |
| `bandit -r . -x ./tests,./.venv` | Security scan |
| `python main.py --mode rag-refresh` | Refresh the knowledge base |
| `python main.py --mode audit` | Self-consistency audit (deprecated — use Overview/Disk/Backups tabs) |

The full CI gate is in `CLAUDE.md` (Development Procedure, step 14).

## 7. Architectural rationale

Decisions are in `docs/decisions/`; `docs/decisions/000-index.md` has a one-line summary of all 34 ADRs. Most relevant for common tasks:

- **ADR 001** — config centralization (adding any setting)
- **ADR 002** — safety invariant (anything that writes to HA)
- **ADR 017** — chat tool parity (all clients passed identically in chat and automated pipelines)
- **ADR 018** — unified agent methodology (agent loop, 6-phase cycle)
- **ADR 038** — runbook lifecycle (signature-keyed, post-session distillation, evidence-validated; supersedes save_runbook)
- **ADR 025** — serialized work queue (anything calling `AgentLoop` or writing to HA in supervisor context)
- **ADR 026** — no concurrent LLM/HA
- **ADR 032** — HA capability gaps (native REST/WS extension; MCP adapter rejected)
- **ADR 033** — event-driven HA triggers (`supervisor.wake()`, fallback polling, subscriber→wake map)
