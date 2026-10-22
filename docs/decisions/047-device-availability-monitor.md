# ADR 047 — Device availability monitor

**Status:** Accepted
**Date:** 2026-10-22

---

## Context

Pueo had zero visibility when a cloud-push integration (EcoNet heat pump) dropped for
40 minutes on 2026-10-22 — and on two earlier occasions of 17 and 54 minutes. HA
logged `EcoNet MQTT unexpected disconnect` followed by a paho thread death, but Pueo
saw nothing because:

1. No loop watches entity or device availability. `HAEventSubscriber` buffers
   `state_changed` events, but `_ha_event_wake_dispatcher` only acts on `update.*`
   and ai_agent_ha events.
2. `CRITICAL_LOG_PATTERN` is matched line-by-line; "Uncaught thread exception" and
   its Traceback span multiple lines and were not matched (addressed separately in
   PR B, S5).
3. The Pueo host sleeps, so a design that relies only on the live event stream would
   miss outages that begin or resolve during sleep.

325 entities were present, with 71 unavailable at investigation time. A naive
entity-level alert would produce continuous noise from Christmas lights, seasonal
plugs, and humidifiers.

---

## Decision

### Device-level alerting with arming

Availability is evaluated at the **device** level, not the entity level. A device is
**offline** when every non-disabled entity linked to it has state `unavailable`.
Entities with no linked device are ignored.

A device can only alert if Pueo has **previously seen it online** (arming rule). Devices
that are already offline on the first run — seasonal decorations, unplugged appliances,
rarely-used devices — are ignored until they come back online at least once. This
eliminates the 71-entity noise floor without requiring a per-device exclusion list.

### HA `last_changed` as the source of offline time

The **offline-since** time for each entity is taken from HA's `last_changed` field,
not from Pueo's wall clock. This means:

- The offline time is correct even after a Pueo restart or host sleep.
- The grace period check (see below) uses a time that predates the current Pueo run if
  the device was already offline before startup.

### Grace period

The poll loop evaluates devices every 60 seconds. A device does not generate an alert
until it has been continuously offline for at least `DEVICE_AVAILABILITY_GRACE_MINUTES`
(default 10 min). Short HA restarts and reloads (which typically produce a few-second
unavailability blip) are suppressed without any special logic.

### Grouping by config entry

Findings are grouped by `config_entry_id`. A cloud-side outage that takes many devices
offline — e.g. all seven Kasa plugs in one account — produces one investigation and
one card, not one per device.

### Polling over event-stream reliance

The poll loop reads `get_states()` from HA's WebSocket API every 60 seconds. The HA
entity registries and device registries are cached for 30 minutes. This approach is
correct even when the Pueo host sleeps, because `last_changed` preserves the true
offline time regardless of when Pueo polls.

### Snapshot persistence (no migration)

The per-device state (last_online_ts, alerted flag) is stored as a JSON blob in
`user_prefs` under key `device_availability:state`. No SQLite schema migration is
needed; the key is created on first write and is tolerant of forward and backward
schema evolution via JSON deserialization defaults.

### Investigation

An `AgentLoop` investigation runs via `utils/ha/device_availability_investigation.py`,
modelled on `utils/disk/host_health_investigation.py`. It uses `ReadOnlySSHClient`
with the real `AutonomyGate(AUTONOMY_LEVEL)` (ADR 043 investigation rule). The only
plausible repair action is `reload_integration`, which is gated and produces a HITL
card at lower autonomy levels.

Initial context includes: integration domain and config entry state, each device's
name, model and offline-since time, other devices in the same entry that are still
online, and a 7-day unavailability frequency count per device (to distinguish a new
outage from a chronically-flapping device).

The terminal tool sends a `CARD_TYPE_DEVICE_OFFLINE` card carrying `episode_id`,
deferring distillation per ADR 046. Non-success outcomes write a 30-minute backoff
row (`device_offline_backoff:<entry_id>`) per the stuck-loop backoff rule.

### Recovery is not verification

When a device comes back online, the poll loop writes a timeline INFO event and calls
`mark_card_resolved`. Recovery is **not** treated as verification of the diagnosis
(ADR 046). A `reload_integration` card executor is already a system verifier; the user
can also click "Pueo was right" to verify the diagnosis explicitly.

### Runbook signature

`sig_device_availability(domain)` → `device:offline:<domain>`. Seed runbook
`seed_device_offline.md` covers cloud-push integrations whose push thread dies and
only recovers on the next scheduled poll; it explains how to distinguish a
vendor-cloud cause from a local-network cause using `fetch_url` and sibling devices.

---

## Consequences

- Pueo detects device availability outages lasting more than the grace period, even
  across host sleep.
- The arming rule suppresses the ~71 already-offline seasonal devices automatically.
- A 40-minute EcoNet outage like the 2026-10-22 event would generate a timeline WARN,
  a debug episode, and a `device_offline` HITL card.
- One investigation and card per config entry, not per device, keeps the signal-to-
  noise ratio acceptable.
- The snapshot uses `user_prefs` JSON; no migration is needed. Rollback is a commit
  revert; disabling via `device_availability_enabled: false` also silences the loop.

---

## Files

- `utils/ha/device_availability.py` — pure evaluator + snapshot + poll loop
- `utils/ha/device_availability_investigation.py` — `AgentLoop` investigation
- `utils/agent/tool_registry.py` — `build_device_availability_tool_registry()`
- `utils/hitl/card_types.py` — `CARD_TYPE_DEVICE_OFFLINE`
- `utils/knowledge/runbook_signature.py` — `sig_device_availability()`
- `utils/knowledge/strategy_seeder.py` — seed registration
- `prompts/agent_loop_device_availability.md`
- `prompts/seed_device_offline.md`
- `config.py`, `config.yaml.default`, `setup.sh` — two new config keys
- `main.py` — supervisor wiring
- `tests/test_device_availability.py`
