---
signature: repair:*:reboot_required
---
# reboot_required — OS or Supervisor update applied, reboot needed

**What it means:** A Home Assistant OS or Supervisor update was installed and requires
a full system reboot to take effect.

**Urgency:** Medium — HA is running on old OS/Supervisor code until reboot; some features
may behave unexpectedly.

## Investigation

- `run_ha_command("ha os info")` — compare `version` vs `version_latest` to confirm which
  update was applied.
- No config check needed; a reboot does not run a config reload.

## Action

- `requires_hitl=true, action="reboot"`

**Explanation template:**
"A Home Assistant OS or Supervisor update has been applied and requires a reboot to take
effect. HA is still running correctly but is using old system code. Approving will reboot
the host."
