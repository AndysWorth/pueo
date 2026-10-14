# Host-health investigation runbook

## Trigger
Pueo detects one of: swap_pressure, memory_low, disk_wear, unexpected_reboot,
supervisor_unhealthy, supervisor_unsupported, resolution_issue.

## Swap pressure / memory low

**Likely causes:** Recorder growing unbounded, a runaway custom integration,
or low-RAM device with many integrations loaded.

**Evidence to gather:**
- `ha apps list`, then `ha apps stats <slug>` per suspect app — find top memory consumers
- `ha core stats` — check Core process RSS
- `read_logs(lines=30, log_source="ha_core")` — look for OOM kills or high-memory warnings

**Fixes:**
- If one app is clearly the outlier: `ha apps restart <slug>` (MEDIUM)
- If Core is leaking: recommend `ha core restart` (MEDIUM); do not auto-restart
  unless autonomy_level >= 3
- If recorder is the root cause: recommend reducing purge_keep_days in configuration.yaml

## Disk wear

**Note:** No automated fix. eMMC wear is irreversible.

**Evidence:**
- `ha host info --raw-json` → disk_life_time field (% of life *used*)
- HA Supervisor considers > 90% a critical condition

**Recommended user action:**
- Back up HA immediately
- Consider migrating the HA data volume to an external SSD
- HA Yellow: SD card can be replaced while OS stays on NVMe

## Unexpected reboot

**Evidence to gather:**
- `read_logs(lines=50, log_source="ha_host")` — OOM kill lines ("Out of memory: Kill process")
- `ha os info` — check if an OS update was scheduled (auto-reboot after update)
- `read_logs(lines=20, log_source="ha_supervisor")` — Supervisor activity around reboot time

**Fixes:** Usually needs_action. If OOM, recommend reducing recorder history or upgrading RAM.

## Supervisor resolution issue

**Evidence:**
- `ha resolution info --raw-json` → issues[] and suggestions[]
- Match the issue uuid against the suggestions list

**Key types:**
- `rpi_firmware_update_blocked`: Pi firmware is outdated. Apply matching suggestion.
- `reboot_required`: OS update pending. Apply matching suggestion.
- `dns_server_failed`: Check network/DNS. Apply healthcheck suggestion.
- `free_space`: Low disk space. Handled by disk recovery path — outcome="monitoring".

**Fix pattern:**
1. Find a suggestion with matching uuid or type
2. If suggestion.auto == true: `ha resolution suggestion apply <uuid>` (MEDIUM)
3. Otherwise: set outcome="needs_action" with steps

## Supervisor unhealthy / unsupported

**Evidence:**
- `ha os info` / `ha core info` — check version vs version_latest
- `ha resolution info --raw-json` — get all issues

**Fix:** Usually requires an OS or Supervisor update. Recommend it with
outcome="needs_action"; Pueo's update manager applies updates (`ha os update` is not
available via run_ha_command).

---
trigger_patterns:
  - swap_pressure
  - memory_low
  - disk_wear
  - unexpected_reboot
  - supervisor_unhealthy
  - supervisor_unsupported
  - resolution_issue
