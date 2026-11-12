# ADR 057 — Host-health recurrence: cooldown, living card, and history block

**Status:** Accepted (2026-11-12)  
**Covers:** S2 (detection, ADR merged into this doc via S3 implementation) + S3 (recurrence)

## Context

Between 2026-11-09 and 2026-11-11 Pueo triggered 37 `host:swap_pressure:unknown`
investigations in ~48 h (~10.6 h of LLM time, 697 tool calls, 33 card sends). Root causes:

1. **Wrong trigger**: single-sample swap-occupancy threshold flapped every poll (S2 — now PSI + OOM kill).
2. **No cooldown**: the card was resolved on clear, so the next sample immediately re-fired a full investigation (S3 — this ADR).
3. **False verification**: `run_ha_command` counted as an action, so read-only sessions were verified as `success` and distilled (S1 — ADR 050 amendment, PR #1180).

## Decisions

### Detection (S2)
`memory_pressure` fires on PSI `some avg300 ≥ HOST_MEM_PSI_WARN_PCT` (default 10.0 %)
or a new OOM-kill count since the previous poll. Swap occupancy is evidence only.
Requires `HOST_HEALTH_CONFIRM_POLLS` (default 3) consecutive samples to fire and to
clear. The old `swap_pressure` kind and `HOST_SWAP_WARN_PCT` config key are removed
(deprecated; silently ignored if set). See PR #1182.

### Recurrence and living card (S3)
One investigation per finding per `HOST_HEALTH_REINVESTIGATE_HOURS` (default 24 h):

- **Suppress**: a re-fire within the window bumps `host_health:recurrences:<key>` (JSON
  timestamp list, 7-day window) in `user_prefs`, updates the living card JSON in place
  (`recurrence_count`, `last_suppressed_at`), and logs `host_health_recurrence_suppressed`.
  No new timeline row.
- **Bypass**: suppression is skipped when severity rose (e.g. WARN → HIGH) or a new OOM
  kill was detected since the last investigation — these are genuinely new signals.
- **Clear**: a recurring kind (`memory_pressure`, `memory_low`, `supervisor_*`) does not
  resolve its card immediately when the finding drops below threshold. The card stays open
  until the finding has been absent for the full `HOST_HEALTH_REINVESTIGATE_HOURS` window
  (tracked in `ResourcePoller._host_clear_since`). HIGH/CRITICAL findings resolve
  immediately regardless.
- **History block**: each investigation's `initial_context` is prepended with a "History"
  block: times seen in 7 d, last session's root_cause/conclusion, last investigated time
  (queried from `session_outcomes` by `load_recurrence_history()`).

## Implementation

- `config.py`: `HOST_HEALTH_REINVESTIGATE_HOURS` (default 24).
- `utils/disk/resource.py`:
  - `_RECURRING_HOST_KINDS = {"memory_pressure", "memory_low"}`, `_is_recurring_host_kind()`
  - `_update_card_recurrence()` — module-level helper updating `{nid}.json` in place.
  - `ResourcePoller._host_clear_since: dict[str, float]` — per-key timestamp when the
    finding first dropped below threshold after being alerted.
  - `_read/write_last_investigated()`, `_read/write_recurrences()` — `user_prefs` helpers.
  - `_check_host_health` revised fire loop and clear loop.
- `utils/disk/host_health_investigation.py`:
  - `load_recurrence_history(db_path, subject_key)` — queries `session_outcomes`.
  - `run_host_health_investigation` gains `recurrence_history: Optional[dict]`.
  - `initial_context` is prepended with "History" block when `times_in_7d > 0`.
- `web/templates/index.html`: host_health cards show "Seen N× in 7 days" when
  `recurrence_count > 0`.

## Consequences

- Expected outcome: zero investigations while PSI ≈ 0 and no OOM kills, even when
  `mem_available_mb` sits at 440–560 MB.
- When a genuine pressure event fires, the agent sees how many times this has happened
  recently and what the last conclusion was, enabling better trend reasoning.
- The card stays open during the cooldown window; the user can still Acknowledge/Suppress
  it at any time. Card JSON is updated in place — no new dashboard card per recurrence.
- S4 (resource_samples trend table) will add per-poll time-series data to the history.
