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
- For disk_wear: `ha host info --raw-json` → `disk_life_time`.
- For unexpected_reboot: `read_logs(log_source="ha_host")` for OOM kill lines.
- For supervisor_unhealthy/unsupported: `ha os info`, `ha resolution info --raw-json`.
