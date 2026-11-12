---
signature: host:memory_pressure:*
---

# Memory-pressure investigation — verdict rules

## Verdict rules

**PSI ≈ 0 and no OOM kill → `monitoring`.**
If the pre-injected trend shows PSI some avg300 ≈ 0 across all windows and
oom_kill_range = 0, the host is not under real memory pressure. State in one
sentence that swap occupancy alone does not indicate pressure and that memory
is stable. Call finish_host_health with outcome="monitoring". Do not investigate
further.

**Swap occupancy alone proves nothing.**
Swap may be populated from a previous high-load period. Only active swap I/O
(PSI > 0, rising psi_some_avg300) or an OOM kill indicates real pressure.
Never recommend hardware replacement based on swap occupancy.

**Falling trend in mem_available + PSI rising → `needs_action`.**
If the trend shows a negative slope (> 5 MB/day decline) and PSI some avg300
is above the threshold, name the top memory consumer from the breakdown and
recommend investigating that component.

**OOM kill delta > 0 → `needs_action`.**
If oom_kill_range in any window is > 0, the kernel killed a process. Read
`ha host logs` to identify the killed process. Name it in the root cause.

**disk_life_time note.**
`disk_life_time` from `ha host info --raw-json` is the percentage of eMMC
write-endurance life consumed (0–100 %), not a number of days.

## Evidence tools in this registry
read_logs, run_ha_command, get_disk_usage, recall, query_knowledge,
get_ha_profile, read_source.
(get_entity_history and get_integration_diagnostics are not available.)
