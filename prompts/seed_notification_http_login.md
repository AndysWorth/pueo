---
signature: notification:http_login
---
# http_login — Failed login attempt notification

## Known device
- **Signal**: `get_device_info` returns a hostname, NetAlertX name, HA device name,
  or non-randomized MAC.
- **Assessment**: routine — user or known automation; no action needed.
- **Action**: `requires_hitl=false, dismiss_now=true`
- **Explanation**: "Login attempt from [device name/hostname] — this is a known device
  on your network (likely your phone or another authorized client)."

## iOS HA companion app
- **Signal**: Message contains `io.robbie.HomeAssistant` and source IP is RFC-1918.
- **Assessment**: MEDIUM — the official iOS companion triggering a login notification
  is normal (e.g. after an app update, token expiry, or re-authentication).
- **Action**: `requires_hitl=false, dismiss_now=true`
- **Explanation**: "Login attempt from the iOS Home Assistant companion app on your
  local network — this is expected behaviour (token refresh or re-login)."

## Unknown device (MAC randomized or no match)
- **Signal**: `get_device_info` returns no name, MAC is randomized, `is_known_device=false`.
- **Assessment**: CRITICAL — potential unauthorized access attempt.
- **Action**: `requires_hitl=true, severity_override="CRITICAL"`
- **Explanation**: "Login attempt from an unrecognized device (IP: X.X.X.X). The MAC
  address indicates a device not in your network inventory. Review your HA logs and
  consider enabling 2FA or restricting access."

## ip-ban — IP address banned
- Same investigation flow as http_login unknown device.
- **Action**: `requires_hitl=true` (HA has banned the IP; user should know).
- **Explanation**: "Home Assistant has banned IP X.X.X.X after repeated failed login
  attempts."
