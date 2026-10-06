# ADR 033 — Event-Driven HA Triggers

**Status:** Accepted
**Date:** 2026-10-02

## Context

Before S9, the three high-value poll loops (`notification_poll`, `repair_poll`,
`update_check`) ran on fixed intervals (5–60 min). The S8 HAEventSubscriber (ADR 032)
already buffers relevant HA WebSocket events; S9 closes the latency gap by wiring those
events to the poll loops so they react promptly instead of waiting for the next tick.

## Decision

### supervisor.wake(name)

`LoopSupervisor.wake(name)` sets a per-loop `asyncio.Event` (stored in
`LoopStatus.wake_event`). `supervised_sleep` waits on this event with
`asyncio.wait_for(wake_event.wait(), timeout=seconds)`: if the event is set before the
timeout, the sleep returns early.

Key properties:
- **Non-cancelling.** Unlike `run_now()`, `wake()` never cancels the loop's task. It
  only interrupts the `supervised_sleep` call. If the loop is mid-iteration, the event
  stays latched and fires on the next `supervised_sleep`.
- **Self-clearing.** The event is cleared in the `finally` block of `supervised_sleep`
  so a single wake produces at most one early wakeup.
- **Unknown names are silently ignored.** `wake()` uses `.get()` so callers need not
  check whether a loop is registered.

### Subscriber → wake map (debounce 5 s)

The `ha_event_wake_dispatch` supervisor task polls the ring buffer every second and maps
events to loop names with a per-loop 5-second debounce:

| Event type | Target loop |
|---|---|
| `persistent_notification_event` | `notification_poll` |
| `repairs_issue_registry_updated` | `repair_poll` |
| `state_changed` (entity `update.*`) | `update_check` |

### Fallback poll interval

While the subscriber is connected and healthy, the three loops sleep for
`HA_EVENT_FALLBACK_POLL_MINUTES` (default 60) between iterations instead of their normal
base interval. On disconnect the loops revert to their normal interval immediately (the
`is_connected()` check happens at the start of each sleep).

## Healthy-subscriber definition

The subscriber is considered healthy when `HAEventSubscriber.is_connected()` returns
`True`. This flag is set to `True` after a successful WS handshake and cleared to
`False` whenever the connection drops, before the reconnect backoff begins. The wake
dispatcher checks this flag before dispatching wakes; the poll loops check it before
lengthening their sleep interval.

## Why polling is kept as a fallback

- WebSocket disconnections are transient; poll loops must not stop working during them.
- Some HA deployments disable the WS API or run in restricted networking; normal polling
  provides correct behaviour without the subscriber.
- The fallback interval (60 min default, configurable) is deliberately long: if the
  subscriber is healthy and events drive wakeups, infrequent polling is redundant. If
  the subscriber is down, reverts to the base interval (5 min default) so nothing is
  missed for long.

## Consequences

- Three poll loops react to HA events within ~6 seconds (1 s poll + 5 s debounce) when
  the subscriber is connected.
- Polling CPU overhead is reduced when the subscriber is healthy.
- `supervised_sleep` gains an extra `asyncio.wait_for` call per sleep; overhead is
  negligible (one coroutine awaited per loop iteration).
- A new supervisor task `ha_event_wake_dispatch` appears on the Overview page.
- `update.*` state changes are filtered by `_is_update_wake_worthy` (`main.py`) before
  waking `update_check`.  Attribute-only mutations such as `in_progress` and
  `update_percentage` — emitted every few seconds during an install — are ignored so the
  wake debounce is not exhausted and the poll loop does not produce duplicate timeline
  entries during the install window.
