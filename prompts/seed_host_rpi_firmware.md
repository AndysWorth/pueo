---
signature: host:resolution_issue:rpi_firmware_update_blocked
---
# rpi_firmware_update_blocked — CM4 EEPROM is read-only

**What it means:** The Raspberry Pi EEPROM firmware version is older than what HA OS
expects. On Compute Module 4 (CM4) based boards (HA Yellow, etc.), the EEPROM is
read-only — `update_blocked=true` with `blocked_reason=eeprom_update_unavailable`.
This is a hardware limitation; no Supervisor action can resolve it.

**Do NOT fabricate causes** like "boot partition management" or "NVMe compatibility".
**Do NOT claim this will self-resolve.** It will not resolve unless a future HAOS
release changes the expectation or the user replaces the CM4 module.

## Investigation

1. `run_ha_command("ha os boards rpi firmware --raw-json")` — read `current`, `latest`,
   `update_blocked`, `blocked_reason` directly.
2. If `blocked_reason == "eeprom_update_unavailable"`: hardware limitation, informational.
3. If `blocked_reason` is empty or different: note it and set outcome="needs_action".

## Action

1. `remember(key="rpi_firmware_blocked", content="rpi_firmware_update_blocked present;
   blocked_reason=eeprom_update_unavailable; CM4 EEPROM is read-only; no Supervisor fix
   exists; informational only.")` — persists this fact across sessions.
2. Set `outcome="informational"` with the EEPROM dates and `blocked_reason` in the summary.
3. Do NOT include `recommended_actions` — there is nothing to do.
