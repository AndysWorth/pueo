---
signature: repair:sensor:statistics
---
# Sensor statistics repair — state_class_removed / units_changed and related

**What it means:** HA's long-term statistics recorder can no longer store data for
one or more sensors.  This happens when an entity is re-registered (e.g. after a
Companion App or integration update) with different unit or state_class than what
the recorder was tracking.  Statistics from before the re-registration remain in
the DB but new data is no longer appended.

**Initial context pre-injected by Pueo:** entity registry, device registry, current
attributes, statistics metadata, and last stats row date.  Read these facts first
before calling any tool.

## Investigation

1. Check `entity created_at` and `device created_at` in the pre-injected evidence.
   A recent creation date (matching the issue `created` date) confirms the entity was
   re-registered — the source integration or app reported the sensor without the
   expected unit or state_class.
2. Compare `current unit_of_measurement` / `current state_class` with the statistics
   metadata (`statistics metadata unit`, `has_sum`, `has_mean`).  A mismatch is the
   direct cause of the HA issue.
3. Check `last statistics row` to confirm when new stats stopped.
4. Never suggest "set the unit in entity settings" if `current device_class` is `(none)`:
   HA only offers a unit override when a device_class is set.

## Verdict rules

- **Missing unit or state_class + recent re-creation:** the source integration or
  Companion App re-registered the entity without the expected attributes.  The fix
  is at the source (update the app / check the integration config).
- **Statistics have stopped:** this is never benign — history is no longer being
  recorded.  `requires_hitl` must be `true`.
- **No device_class:** never recommend a unit override via HA settings; it won't work.
- **Prior sessions verified success:** earlier sessions are background, not evidence.
  Re-check live attributes before concluding.
- **Never claim "known deprecation"** without quoting the HA release note or doc.

## Options to present in `finish_sensor_stats`

Always include at least:
1. **Fix at the source** — update the Companion App / integration, check permissions,
   re-sync sensors.  Issues self-resolve once the entity re-registers with correct
   attributes.
2. **Accept the data gap** — use HA Developer Tools → Statistics to manage or delete
   the old statistics for the affected sensors.
3. **Ignore all in HA** (via card button) — suppress the issues in HA without changing
   config; statistics remain broken but the repair panel is cleared.

## Terminal tool

Call `finish_sensor_stats` with:
- `explanation`: 2–4 sentences covering root cause, affected entities, and date
- `root_cause`: one sentence
- `options`: ordered list as above
- `requires_hitl`: always `true`
