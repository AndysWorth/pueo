# ADR 025 — Serialized work queue for LLM/HA operations

## Status
Accepted

## Context

Before this change, multiple Pueo monitoring loops could simultaneously initiate LLM inference calls and HA SSH operations. Three races were possible:

1. **Simultaneous repairs.** `_stream_and_triage` in `ha_log_monitor.py` called `asyncio.create_task(trigger_remediation_pipeline())` for every qualifying log line without checking whether a repair was already running. Two rapid log events from the same HA failure could submit two concurrent `AgentLoop.run()` calls to Ollama — doubling inference load and both trying to write the same `/config/configuration.yaml`.

2. **Simultaneous ha_backup calls.** The backup-before-write safety invariant (ADR 002) requires a confirmed backup slug before every config write. Two concurrent repair pipelines would each call `execute_remote_backup()` at the same time; on HA, concurrent backup creation is at best undefined behavior and at worst fails entirely.

3. **Chat contending with repair.** `POST /chat/message` called `asyncio.create_task(_run_chat_loop(...))` unconditionally. A user typing in Chat while an autonomous repair was running would start a second `AgentLoop` against Ollama, increasing latency for both and consuming HA SSH connections.

These races were not theoretical: in production logs, duplicate `repair_done` events for the same incident confirmed that concurrent repair cycles had run.

## Decision

Introduce `PueoWorkQueue` in `utils/agent/work_queue.py`: a module-level singleton that runs all LLM/HA operations serially, one item at a time, with priority ordering and deduplication.

### Core design

```python
@dataclass
class WorkItem:
    priority: int           # lower = higher priority
    activity_type: str
    description: str
    dedup_key: str          # "" = no dedup
    suppress_while_running: frozenset[str]  # hold until these activity types finish
    coro_factory: Callable[[], Coroutine]
    submitted_at: float = field(default_factory=time.time)
```

`WorkItem` is ordered by `(priority, submitted_at)` using comparison methods, which allows `bisect.insort` to maintain a sorted pending list.

`PueoWorkQueue` maintains:
- `_pending: list[WorkItem]` — sorted by `bisect.insort`
- `asyncio.Lock` for thread-safe mutation
- `asyncio.Event` as a wakeup signal for the consumer
- `_running: WorkItem | None` — the currently executing item
- `_active_types: set[str]` — activity types currently running

`submit(item)` acquires the lock, checks for a dedup match (running or pending item with the same non-empty `dedup_key`), inserts with `bisect.insort`, sets the event, and publishes a `queue_update` SSE event.

`_consume()` is an infinite loop: wait for the event, pick the first pending item whose `suppress_while_running ∩ _active_types` is empty, run it, then check again.

### Priority constants

| Constant | Value | Used for |
|---|---|---|
| `PRIORITY_CRITICAL` | 10 | Repair triggered by live log event |
| `PRIORITY_HIGH` | 20 | Triage, disk recovery, notification analysis |
| `PRIORITY_NORMAL` | 30 | Update analysis, lovelace investigation, config analysis, chat |
| `PRIORITY_LOW` | 40 | Background / routine |

### Suppression pattern

An item can declare `suppress_while_running=frozenset({"ha_update"})` to wait until all `ha_update` activity types have cleared from `_active_types`. This allows disk recovery to wait for an ongoing HA update to finish before starting a potentially disruptive backup retention purge. Callers declare their dependencies; the queue enforces them.

### Ordering invariant

A queued repair (CRITICAL, priority 10) starts only after the currently running item finishes. If chat is running when a repair is submitted, chat runs to completion and then the repair starts. A repair submitted while nothing is running starts immediately.

This satisfies the design requirement: "let a running chat session finish" — the chat won't be interrupted — while ensuring a queued repair starts as soon as the slot is free.

### Mandatory queue rule — AgentLoop

All `AgentLoop.run()` calls that represent a judgment call or HA-state change **must** be submitted through `PueoWorkQueue`. Bypassing the queue (calling `AgentLoop.run()` directly in a supervisor context) is a correctness violation regardless of the calling context.

Use `get_work_queue_or_none()` and fall back to direct execution only when the queue is not initialized (standalone scripts, unit tests).

The only legitimate one-shot LLM calls are:
- Volume-throttled streaming pre-filters (`analyze_log_line_with_ai`) — fire per log line; sole output is a binary gate before a queue submission
- Secondary enrichment inside a running tool executor (`_enrich_fix_context`) — called inside an already-running AgentLoop; cannot itself be a queue submission

### Mandatory queue rule — HA interaction

Any operation that writes to or restarts Home Assistant — SSH config writes, `ha backup new`, sandbox test + atomic swap, `ha core restart`, `ha os update`, HA REST API writes (Lovelace config, recorder purge) — must also run inside a `WorkItem`. The queue's single consumer prevents concurrent HA mutations regardless of whether LLM inference is involved. Card handlers in `web/dashboard.py` that perform HA operations without LLM (`_execute_queued_fix`, `_execute_queued_update`, `_execute_disk_recovery`, etc.) are subject to this rule. Bypassing the queue for HA writes during supervised operation is a correctness violation equivalent to bypassing the backup-before-write invariant (ADR 002).

See ADR 026 for the rationale and full context.

### Callers

| File | Activity type | Priority | dedup_key |
|---|---|---|---|
| `ha_log_monitor.py` triage | `triage` | HIGH | `triage:{line_fp}` |
| `ha_log_monitor.py` repair trigger | `ha_repair` | CRITICAL | `ha_repair:{fp}` |
| `ha_log_monitor.py` repair issue | `repair_issue` | NORMAL | `repair_issue:{issue_id}` |
| `ha_log_monitor.py` update impact analysis | `update_analysis` | NORMAL | `update_analysis` |
| `ha_lovelace_monitor.py` investigation | `lovelace_investigation` | NORMAL | `lovelace_investigation` |
| `ha_notification_manager.py` analysis | `notification` | HIGH | `notification:{ha_nid}` |
| `ha_update_manager.py` update analysis (run_update_check) | `update_analysis` | NORMAL | `update_analysis` |
| `netalertx/log_monitor.py` healer dispatch | `netalertx_repair` | CRITICAL | `netalertx_repair` |
| `netalertx/diagnosis.py` health diagnosis | `netalertx_diagnosis` | HIGH | `netalertx_diagnosis` |
| `utils/disk/resource.py` disk recovery | `disk_recovery` | HIGH | `disk_recovery` |
| `web/dashboard.py` chat | `chat` | NORMAL | `chat` |
| `web/dashboard.py` `approve()` + `apply_fixes()` | `card_execution` | HIGH | `card_{nid}` / `config_fixes_{nid}` |

`utils/agent/config_analysis.py` is excluded: it is always called within a repair pipeline item (CRITICAL), and its AgentLoop inherits the queue's serialization implicitly.

### Initialization

In `main.py`, before starting supervisor loops:
```python
work_queue = init_work_queue()
```
After the event loop is running (after supervisor tasks are registered):
```python
work_queue.start()
```

Callers use `get_work_queue_or_none()` and fall back to direct execution if the queue is not available (standalone scripts, unit tests).

### Dashboard

`GET /api/pueo-queue` returns the pending queue snapshot. The overview page fetches this on load and updates a `queue_update` SSE event to render a "N queued" badge on the Current Activity widget.

## Rationale

**Serial execution over parallel.** Ollama inference is a shared local resource. Parallel AgentLoop calls compete for the same GPU/CPU and inflate latency for every caller. Serialization gives each item the full inference budget and produces better results.

**Priority over FIFO.** A live log event indicating an active HA failure should not wait behind a scheduled background analysis. Priority constants formalize the urgency hierarchy without any caller needing to know about other callers.

**Dedup over locking at each call site.** Adding a lock around each `create_task(repair)` call would suppress duplicates but still allow races between different activity types. The work queue centralizes the concurrency policy.

**Volume protection.** The existing `Debouncer`, `RateLimiter`, and DB-based cooldown checks remain in place as cheap pre-filters before submission. These prevent the queue from filling up during a high-frequency log event burst.

## Consequences

- All significant LLM and HA SSH operations are strictly serial. A burst of HA log errors queues multiple triage items; only the first runs immediately; duplicates are deduped by `dedup_key`.
- Throughput is lower than concurrent execution. This is acceptable: the system's correctness guarantee (one backup per repair, one repair at a time) is worth the serialization cost.
- `PueoWorkQueue.start()` must be called inside the running asyncio event loop; calling it before `asyncio.run()` raises.
- Tests that want queue behavior must call `init_work_queue()` + `queue.start()` in an async fixture; tests that want to bypass the queue call the functions directly (the `get_work_queue_or_none()` fallback path).

## Related decisions

- [ADR 002 — Backup-before-write safety invariant](002-safety-invariant.md): the queue prevents concurrent backup calls by serializing the repair operations that trigger them.
- [ADR 005 — asyncio over framework](005-asyncio-over-agentic-framework.md): `PueoWorkQueue` is implemented in plain asyncio; no external scheduler or framework.
- [ADR 022 — Adaptive per-call LLM timeout](docs/decisions/022-adaptive-llm-timeout.md): a timed-out item propagates `asyncio.TimeoutError` from `coro_factory()`; the queue catches it, logs, clears `_active_types`, and continues with the next item.
