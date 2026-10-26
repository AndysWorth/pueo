# Host-health investigation — method

Use this runbook when no symptom-specific runbook matches.

## Triggers
swap_pressure, memory_low, disk_wear, unexpected_reboot,
supervisor_unhealthy, supervisor_unsupported, resolution_issue.

## General approach

- Phase 1: `query_knowledge` with the specific resolution_issue type or trigger name.
  A symptom-specific runbook may be returned — follow it if so.
- For resolution_issue: `ha resolution info --raw-json` → read issue type and suggestions.
- For memory: `ha apps stats <slug>`, `ha core stats`, `read_logs` for OOM.
- For disk_wear: `ha host info --raw-json` → `disk_life_time`. There is no automated
  fix: `outcome="needs_action"`; recommend a fresh backup off the host and planning a
  storage replacement.
- For unexpected_reboot: the current boot's log starts after the reboot, so it can't
  show why the host went down. Before concluding, read the end of the previous boot with
  `read_logs(log_source="ha_host_prev_boot", lines=200)`.
  - An orderly shutdown (`systemd` stopping services, `Unmounted …`, `Reached target …
    Shutdown`) means something asked the host to restart: a user, an automation, an
    update or the Supervisor. Say so, and say which one only if a log line shows it.
  - OOM kills, kernel panics or I/O errors just before the end point to that cause.
  - A log that stops mid-stream with no shutdown lines means power was lost, or the host
    hung and was reset. It does not tell you which.
  Then `read_logs(log_source="ha_host")` for the current boot's errors.
- For supervisor_unhealthy/unsupported: `ha os info`, `ha resolution info --raw-json`.
