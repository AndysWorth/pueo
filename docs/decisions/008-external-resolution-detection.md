# ADR 008 — Two-poll confirmation for externally resolved conditions

## Status
Accepted

## Context
HA can resolve pending conditions without going through Pueo's approval flow. The most common case is HA auto-applying an update (e.g., `matter_server` 9.1.1 → 9.2.0 applied silently before Pueo's poll cycle). When this happens, the approval card for that update sits pending forever — the update poll loop has a guard that correctly refuses to resolve unapproved pending cards on a single poll (transient HA boot gaps look identical to genuine external resolution), but there was no second path to confirm the update was truly applied.

The same gap exists for HA repair issues that clear on their own (e.g., a reboot-required repair after the user reboots HA directly) and disk alerts that recover without user action (e.g., HA's background cleanup frees space).

Nothing appeared in the Event Timeline to explain what happened outside Pueo, leaving the user with stale approval cards and no audit trail.

## Decision

### Two-poll confirmation

Before retiring any pending card due to a condition going absent, Pueo now requires the condition to be absent for **two consecutive poll cycles**. On first detection of absence, a timestamp is recorded in an in-memory dict (`_update_gone` for updates). On the second detection, the card is retired and a timeline event is written.

**Why two polls instead of one?**
A transient HA boot or entity reload can make update entities disappear for a single poll cycle without any real resolution. Two polls provides enough dwell time (one full poll interval, typically hours for update checks) to distinguish a transient gap from genuine external resolution. A daemon restart resets the timer; at most one extra poll cycle of delay is acceptable given the hour-scale interval.

**Why not persist the timer to SQLite?**
The timer is diagnostic state, not safety state. If Pueo restarts, the worst outcome is one extra poll cycle before external resolution is confirmed — not a missed or duplicate card. Adding a migration for a transient timestamp adds schema complexity with negligible benefit.

### Timeline event on external resolution

When a condition resolves externally, Pueo writes an INFO timeline event with source `update_check`, `ha_repairs`, or `resource` explaining what happened. The approval card is retired by setting `resolved_at` in `hitl_suppression`. For file-notifier deployments, an `.approved` sidecar is written to hide the card from the approval queue.

No approval card is created for the external resolution — only a timeline entry. The user can review it in the dashboard's Event Timeline tab.

### Reconcile sweep for absent entities

The update poll loop now includes a reconcile sweep after the per-entity loop. This handles entities that vanish entirely from the HA REST response (the most common case with HA auto-update) rather than staying present with `update_available=False`. The same two-poll confirmation applies.

### Resource and repair clearance

Disk critical and disk warn alert clearance sites in `utils/resource.py` now write a timeline event when the alert resolves. Repair reconcile in `ha_log_monitor.poll_for_repairs()` writes a timeline event when a previously-queued issue no longer appears in HA's repair list.

## Consequences

- A pending update card that HA resolves without Pueo's involvement will be retired after the next two poll cycles (typically two hours for update checks). The Event Timeline will show an INFO entry explaining the external resolution.
- Restart within the two-poll window delays confirmation by one extra poll cycle only.
- The `_update_gone` dict is local to `poll_for_updates()` — no cross-coroutine state.
- `_resolve_externally_applied_update()` degrades gracefully when the watch-dir card file is absent (non-FileNotifier deployments): the timeline event still fires with "unknown" version strings.

## Related decisions
- [ADR 002 — Backup-before-write safety invariant](002-safety-invariant.md): External resolution detection is purely observational — no backup or write action is taken, so the safety invariant is not involved.
- [ADR 001 — Config centralization](001-config-centralization.md): `NOTIFY_WATCH_DIR` and `DB_PATH` are the single source of file paths; both are imported from `config.py`.
