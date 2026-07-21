# Agentic Engineering Practices — Implementation Plan

Pick up the next incomplete item at the start of a new session: find it in the Status table below, then open the linked detail file for the full specification before writing any code.

Detail files: [plan/foundation.md](plan/foundation.md) · [plan/autonomy.md](plan/autonomy.md) · [plan/netalertx.md](plan/netalertx.md) · [plan/hitl-dashboard.md](plan/hitl-dashboard.md) · [plan/status-logging.md](plan/status-logging.md)

---

## Status

| #   | Item                                                                 | Status              |
| --- | -------------------------------------------------------------------- | ------------------- |
| 1   | Prompt Management                                                    | ✅ Done (2026-07-15) |
| 2   | Retry with Exponential Backoff                                       | ✅ Done (2026-07-15) |
| 3   | Rate Limiting and Debounce                                           | ✅ Done (2026-07-15) |
| 4   | SQLite Migration Strategy                                            | ✅ Done (2026-07-15) |
| 5   | Structured Logging + Correlation IDs                                 | ✅ Done (2026-07-15) |
| 6   | Context Window / Token Management                                    | ✅ Done (2026-07-15) |
| 7   | Agent Output Content Validation                                      | ✅ Done (2026-07-15) |
| 8   | Dependency Injection / Protocol Interfaces                           | ✅ Done (2026-07-15) |
| 9   | HITL Notification Infrastructure                                     | ✅ Done (2026-07-15) |
| 9.5 | Unified Autonomy Level                                               | ✅ Done (2026-07-19) |
| 10  | NetAlertX Foundation — Package, Config, and API Client               | ✅ Done (2026-07-19) |
| 11  | NetAlertX Installer — Steps 1–4                                      | ✅ Done (2026-07-19) |
| 12  | NetAlertX Installer — Steps 5–8                                      | ✅ Done (2026-07-19) |
| 13  | NetAlertX Device Name Sync — HA Name Reading and Safe Writes         | ✅ Done (2026-07-20) |
| 14  | NetAlertX Device Name Sync — Conflict Resolution and Unknown Devices | ✅ Done (2026-07-20) |
| 15  | NetAlertX Log Monitoring                                             | ✅ Done (2026-07-20) |
| 16  | NetAlertX Health Polling and MQTT                                    | ✅ Done (2026-07-20) |
| 17  | NetAlertX AI Diagnosis                                               | ✅ Done (2026-07-20) |
| 18  | NetAlertX Autonomy-Gated Healing                                     | ✅ Done (2026-07-20) |
| 19  | NetAlertX HA Integration Maintenance                                 | ✅ Done (2026-07-20) |
| 19.5 | HITL Web Dashboard                                                  | ✅ Done (2026-07-20) |
| 20  | NetAlertX Setup Status Logging                                       | ✅ Done (2026-07-20) |

---

## Phases

### Phase 1–3 — Foundation, Observability, Architecture ✅ Complete
Items 1–9. All complete as of 2026-07-15. Covers prompt management, SSH/Ollama retry with backoff, rate limiting and debounce, SQLite migration versioning, structured JSON logging with correlation IDs, token budget management, YAML content validation, dependency injection via Protocol interfaces, and HITL notification infrastructure (FileNotifier, NtfyNotifier, WebhookNotifier).

→ [plan/foundation.md](plan/foundation.md)

---

### Phase 3.5 — Cross-Cutting: Autonomy Control (1 session) ✅ Complete (2026-07-19)
Item 9.5. Adds `agent.autonomy_level` (integer 1–4, default 2) and `AutonomyGate` — the single ask/skip decision point imported by every Pueo module. Also adds `FakeAutonomyGate` for tests. Refactors the hardcoded `requires_hitl()` in the HA sandbox engine. **All Phase 4 items depend on this being implemented first.**

Levels: 1 = report only · 2 = suggest + approve all · 3 = auto LOW-risk + approve MEDIUM/HIGH/CRITICAL · 4 = auto LOW/MEDIUM/HIGH + approve CRITICAL only.

→ [plan/autonomy.md](plan/autonomy.md)

---

### Phase 4 — NetAlertX Integration (11–14 sessions) ✅ Complete (2026-07-20)
Items 10–19. Full lifecycle for a new integration target: install from scratch (items 10–12), sync device names from HA (13–14), monitor logs and health (15–16), AI diagnosis (17), autonomy-gated healing (18), and ongoing HA integration maintenance (19). Requires Phase 3.5 complete before item 10.

| Items | Concern |
|-------|---------|
| 10 | Package skeleton, all config keys, SQLite migration, detector, API client |
| 11–12 | Idempotent installer: 8-step state machine across two sessions |
| 13–14 | HA→NetAlertX device name sync across two sessions |
| 15–16 | Continuous monitoring: log tail and health polling/MQTT |
| 17 | AI diagnosis prompts, Pydantic schema, config validator |
| 18–19 | Healing actions gated by autonomy level; HA integration maintenance |

→ [plan/netalertx.md](plan/netalertx.md)

---

### Phase 4.5 — HITL UX (1 session) ✅ Complete
Item 19.5. Eliminates the 60-minute blocking timeout from `AutonomyGate.require_approval()`, converts monitoring loops to fire healing as `asyncio.create_task()`, and adds a local FastAPI web dashboard (`python main.py --mode dashboard`) for approving or rejecting pending repair actions via browser. Adds `fastapi`, `jinja2`, and `uvicorn` dependencies.

→ [plan/hitl-dashboard.md](plan/hitl-dashboard.md)

---

### Phase 5 — Observability UX (1 session)
Item 20. Wires up `setup_logging()` centrally in `main.py` so all modes emit log output, and adds a human-readable plain-text console formatter used by `--mode netalertx-setup`. Currently the installer emits rich structured events at every step but they are silently dropped because no handlers are attached. The file handler always stays JSON; the stderr handler switches to plain text for the setup wizard.

→ [plan/status-logging.md](plan/status-logging.md)

---

## Tracking

Update the Status column above (`✅ TODO` → `✅ Done (date)`) **and** the matching entry in the linked detail file when an item completes. Add the PR or commit reference as a note in the detail file.
