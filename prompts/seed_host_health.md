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

## Supervisor resolution issue — no_current_backup

**Why it triggers:** The HA Supervisor requires a recent *full* backup to be present on the
HA host. Automatic HA backups are *partial* — only the Core config. Pueo's own backups are
full, but the retention policy may have deleted them all from HA.

**Evidence:**
- `ha resolution info --raw-json` → confirm the issue type is `no_current_backup`
- `ha backups list --raw-json` → check `type` field; count backups where `type == "full"`

**Fix:**
1. If a full backup already exists on HA: apply the suggestion `create_full_backup` via
   `ha resolution suggestion apply <uuid>` (MEDIUM) to point the Supervisor at it, then
   `ha resolution healthcheck` (LOW) to clear the issue.
2. If no full backup exists on HA: the suggestion still applies and the Supervisor will
   create one after the healthcheck. Apply `create_full_backup` suggestion and confirm.
3. Set outcome="resolved" only after confirming via `ha resolution info --raw-json` that
   the issue UUID is no longer present.

## Supervisor resolution issue — rpi_firmware_update_blocked

**What it means:** The Raspberry Pi EEPROM firmware version is older than what HA OS
expects. On Compute Module 4 (CM4) based boards (HA Yellow, etc.), the EEPROM is
read-only — `update_blocked=true` with `blocked_reason=eeprom_update_unavailable`. This
is a hardware limitation; no Supervisor action can resolve it.

**Do NOT fabricate causes** like "boot partition management" or "NVMe compatibility".
The actual cause is always: CM4 EEPROM is read-only; no Supervisor action can update it.

**Do NOT claim this will self-resolve.** It will not resolve unless a future HAOS
release changes the expectation or the user replaces the CM4 module.

**Investigation:**
1. `run_ha_command("ha os boards rpi firmware --raw-json")` — read `current`, `latest`,
   `update_blocked`, `blocked_reason` directly.
2. If `blocked_reason == "eeprom_update_unavailable"`: hardware limitation, informational.
3. If `blocked_reason` is empty or different: note it and set outcome="needs_action".

**Fix:**
1. `remember(key="rpi_firmware_blocked", content="rpi_firmware_update_blocked present;
   blocked_reason=eeprom_update_unavailable; CM4 EEPROM is read-only; no Supervisor fix
   exists; informational only.")` so this persists across sessions.
2. Set `outcome="informational"` with the EEPROM dates and `blocked_reason` in the
   summary. Do **not** include `recommended_actions`.

## Supervisor resolution issue — general

**Evidence:**
- `ha resolution info --raw-json` → issues[] and suggestions[]
- Match the issue uuid against the suggestions list

**Key types:**
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
