# Host-health investigation runbook

## Trigger
Pueo detects one of: swap_pressure, memory_low, disk_wear, unexpected_reboot,
supervisor_unhealthy, supervisor_unsupported, resolution_issue.

## Swap pressure / memory low

**Likely causes:** Recorder growing unbounded, a runaway custom integration,
or low-RAM device with many integrations loaded.

**Evidence to gather:**
- `ha apps stats` — find top memory consumers among add-ons/apps
- `ha core stats` — check Core process RSS
- `ha core logs --lines 30` — look for OOM kills or high-memory warnings

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
- `ha core logs --lines 50` — OOM kill lines (look for "Out of memory: Kill process")
- `ha os info` — check if an OS update was scheduled (auto-reboot after update)
- `ha supervisor logs --lines 20` — Supervisor activity around reboot time

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
- `ha supervisor info` — check version compatibility
- `ha resolution info --raw-json` — get all issues

**Fix:** Usually requires OS or Supervisor update. Run `ha os update` if safe.

---
trigger_patterns:
  - swap_pressure
  - memory_low
  - disk_wear
  - unexpected_reboot
  - supervisor_unhealthy
  - supervisor_unsupported
  - resolution_issue
