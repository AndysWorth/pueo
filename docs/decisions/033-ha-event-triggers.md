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

| Event type | Condition | Target loop |
|---|---|---|
| `persistent_notification_event` | — | `notification_poll` |
| `repairs_issue_registry_updated` | — | `repair_poll` |
| `state_changed` | entity `update.*`, state or `latest_version` changed | `update_check` |
| `state_changed` | entity `automation.ai_agent_auto_*`, `ai_agent_ha_installed` | `lovelace_poll` |
| `automation_triggered` | entity `automation.ai_agent_auto_*`, `ai_agent_ha_installed` | `lovelace_poll` |
| `lovelace_updated` | `ai_agent_ha_installed` | `lovelace_poll` |

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
- When `ai_agent_ha` is installed, `lovelace_poll` also wakes promptly on
  `automation.ai_agent_auto_*` creation/change and on any lovelace config update; the
  existing lovelace investigation then checks dashboard entity health and raises a card on
  failure.  All three new wake types are gated on `ai_agent_ha_installed` from the
  HA environment profile so the extra dispatching is a no-op for installations without the
  companion.
- `lovelace_updated` is added to `_SUB_EVENTS` in `ha_event_subscriber.py`, and
  `_is_interesting_state_change` is extended to pass through `automation.ai_agent_auto_*`
  state changes so they reach the wake dispatcher.
- Polling CPU overhead is reduced when the subscriber is healthy.
- `supervised_sleep` gains an extra `asyncio.wait_for` call per sleep; overhead is
  negligible (one coroutine awaited per loop iteration).
- A new supervisor task `ha_event_wake_dispatch` appears on the Overview page.

### Addendum — Host sleep and catch-up wakes (2026-10-16)

macOS asyncio timers use `time.monotonic()`, which does not advance during system sleep.
While the Mac sleeps, the entire supervisor is frozen; HA closes the idle WebSocket
connection and the three poll loops are stretched by the sleep duration.  To handle this:

1. **Sleep detection.** The wake dispatcher compares wall-clock elapsed (`time.time()`)
   with monotonic elapsed (`time.monotonic()`) on every 1-second tick.
   `host_sleep_seconds()` returns the difference; a result ≥ `HOST_SLEEP_THRESHOLD_SECONDS`
   (30 s) means the host slept.  All three time-sensitive loops are woken immediately,
   bypassing the 5-second debounce.  The event `host_sleep_detected` is logged at INFO.

2. **Reconnect-triggered catch-up.** `HAEventSubscriber.session_count()` increments after
   each successful WS handshake.  When the dispatcher sees the count rise above a previous
   non-zero value (a reconnect, not the first connect), it wakes all three loops.  The
   event `ha_event_subscriber_reconnect_catchup` is logged at INFO.

3. **Sleep-aware drop classification.** `run_forever` now computes how long the host may
   have slept since the last received message (using `_last_rx_wall` and `_last_rx_mono`
   set in `_connect_and_subscribe`).  Drops inferred to follow a host sleep are logged at
   INFO as `ha_event_subscriber_reconnect_after_sleep`; unexpected drops remain WARNING
   `ha_event_subscriber_error`.  The classification uses the pure function
   `_classify_drop(slept_s) -> str` in `ha_event_subscriber.py`.

Together these ensure that notifications, repairs, and updates that arrived while the host
was asleep are checked within one poll cycle after wake, rather than waiting up to 60 min
for the next scheduled fallback poll.
- `update.*` state changes are filtered by `_is_update_wake_worthy` (`main.py`) before
  waking `update_check`.  Attribute-only mutations such as `in_progress` and
  `update_percentage` — emitted every few seconds during an install — are ignored so the
  wake debounce is not exhausted and the poll loop does not produce duplicate timeline
  entries during the install window.
