# HA Repair Issue Investigation Runbook

This runbook covers investigation of Home Assistant repair issues surfaced via the repairs panel.

## Common repair issue patterns

### config_entry_reauth — Integration needs re-authentication
- **Cause**: OAuth token expired, API key revoked, or password changed
- **Urgency**: Low to Medium — integration stops working but HA continues running
- **Investigation**: Check domain logs for auth errors; no need to read config
- **Action**: requires_hitl=true, action="dismiss" (dismissing prompts re-auth flow)
- **Explanation**: "The {domain} integration's credentials have expired. Dismissing this
  repair issue will prompt you to re-authenticate on the next page load."

### reboot_required — OS or Supervisor update applied
- **Cause**: Home Assistant OS or Supervisor update requires a restart to take effect
- **Urgency**: Medium — system is running on old code until reboot; some features may behave oddly
- **Investigation**: run_ha_command("ha supervisor info") to check pending versions
- **Action**: requires_hitl=true, action="reboot"
- **Explanation**: "A Home Assistant OS or Supervisor update has been applied and requires a
  reboot to take effect."

### restart_required — HA Core update applied
- **Cause**: HA Core update was installed; a restart activates the new version
- **Urgency**: Low — running fine, just on old code
- **Investigation**: run_ha_command("ha supervisor info") to confirm update version
- **Action**: requires_hitl=true, action="restart"
- **Explanation**: "Home Assistant Core has been updated. A restart is needed to activate the
  new version."

### integration_disabled / setup_error — Integration failed to load
- **Cause**: Integration raised an exception during setup (bad config, unreachable device, etc.)
- **Urgency**: Medium — that integration's entities are unavailable
- **Investigation**: read_logs to find the exception; query_knowledge for known issues
- **Action**: requires_hitl=true, action="dismiss" (user investigates in UI)
- **Explanation**: "The {domain} integration failed to load. Check the HA logs for the specific
  error and consider removing and re-adding the integration."

### breaking_change — Deprecated config key
- **Cause**: HA updated and a config key or platform the user's YAML uses is now invalid
- **Urgency**: High — HA may reject configuration.yaml on next restart
- **Investigation**: query_knowledge for the translation_key; read_config to check YAML
- **Action**: requires_hitl=true, action="dismiss" with detailed YAML fix advice
- **Explanation**: Include the specific YAML change needed.

## Investigation guidance
- Prefer query_knowledge over reading logs for known integration issues
- If breaks_in_ha_version is set, the issue is time-sensitive — flag it clearly
- If severity is "critical", always set requires_hitl=true and action="reboot" unless
  investigation clearly shows otherwise
- Save a runbook if you discover a novel translation_key not covered above
