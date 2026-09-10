# HA Notification Investigation Runbook

This runbook covers investigation of Home Assistant persistent notifications.

## http_login — Failed login attempt

### Known device
- **Signal**: get_device_info returns a hostname, NetAlertX name, HA device name, or non-randomized MAC
- **Assessment**: routine — user or known automation; no action needed
- **Action**: requires_hitl=false, dismiss_now=true
- **Explanation**: "Login attempt from [device name/hostname] — this is a known device on your
  network (likely your phone or another authorized client)."

### Unknown device (MAC randomized or no match)
- **Signal**: get_device_info returns no name, MAC is randomized, is_known_device=false
- **Assessment**: CRITICAL — potential unauthorized access attempt
- **Action**: requires_hitl=true, severity_override="CRITICAL"
- **Explanation**: "Login attempt from an unrecognized device (IP: X.X.X.X). The MAC address
  indicates a device not in your network inventory. Review your HA logs and consider enabling
  2FA or restricting access."

## ip-ban — IP address banned
- Same investigation flow as http_login
- **Action**: requires_hitl=true (HA has banned the IP; user should know)
- **Explanation**: "Home Assistant has banned IP X.X.X.X after repeated failed login attempts."

## invalid_config — Configuration file error
- **Signal**: HA rejected configuration.yaml on load or reload
- **Investigation**: Check logs for the specific YAML section and error message
- **Action**: requires_hitl=true, action includes the specific broken section and fix
- **Explanation**: "Home Assistant detected a configuration error in [section]. The specific
  error is: [error from logs]. Fix the YAML before restarting HA."

## integration_disabled / component_load_failed
- **Investigation**: Check logs for the exception during integration setup
- **Action**: requires_hitl=true with specific diagnostic steps
- **Explanation**: "The [integration] integration failed to load: [error]. Try removing and
  re-adding the integration, or check the HA community forum for this specific error."

## Custom alert / informational
- **Assessment**: Usually benign — user-configured automations or scripts
- **Action**: requires_hitl=false, dismiss_now=true
- **Explanation**: Summarize the notification message in plain English

## Investigation priorities
1. For security notifications (http_login, ip-ban): always call get_device_info first
2. For config errors: logs contain the specific YAML section and error
3. For integration failures: logs show the Python exception; query_knowledge for the integration
4. Dismiss benign notifications (known logins, informational alerts) to keep the panel clean
