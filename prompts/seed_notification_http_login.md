---
signature: notification:http_login
---
# http_login / ip-ban — failed login attempt notification

Covers both `http-login` (failed login) and `ip-ban` (HA banned the IP after repeated
failures). This runbook owns the verdict for these notifications.

## Evidence — in this order
1. **Find the auth line.** Call
   `search_log(log_name="ha_core", pattern="invalid authentication|Login attempt|Banned IP")`.
   The matching line gives the source IP, the requested URL (e.g. `/auth/token`,
   `/api/websocket`) and usually the user agent. Do not rely on `read_logs` with a small
   line count — the auth line is usually not in the last few lines.
2. **Identify the device.** Call `get_device_info(ip)` (mandatory). Read these fields:
   - `inventory_available` / `netalertx_status`: whether the NetAlertX inventory was
     actually read. When `inventory_available=false`, a null `netalertx_name` means
     **"inventory unavailable"**, not "not in inventory".
   - `is_pueo_host`: the IP belongs to the machine running Pueo.
   - `hostname`, `dhcp_hostname`, `ha_device_name`, `netalertx_name`: any of these
     identifies the device.
   - `mac_is_randomized`, `mac_vendor`: context only (see below).

## Verdicts

### Pueo host (`is_pueo_host=true`)
- Likely Pueo itself or a dev script with a stale or wrong HA token.
- `requires_hitl=false`, `dismiss_now=true`. Recommended action: check the HA token
  Pueo and any dev scripts use.

### Known device (any name field set)
- Routine: the user's own client re-authenticating (app update, token expiry).
- `requires_hitl=false`, `dismiss_now=true`. Name the device from the field that
  identified it.

### HA companion app user agent
- **Signal**: the auth line or notification text itself contains a companion-app user
  agent (`io.robbie.HomeAssistant` for iOS, `io.homeassistant.companion.android` for
  Android) and the source IP is RFC-1918.
- Expected behaviour (token refresh or re-login). `requires_hitl=false`,
  `dismiss_now=true`.
- Quote the UA from the log line. Never infer an owner's name from the UA string:
  `io.robbie` is the app's bundle id, not a person.

### Unidentified device on the LAN (RFC-1918 IP, no name field set)
- A randomized MAC alone is **not** evidence of an intruder — it is the iOS/Android
  default for private Wi-Fi addresses.
- When `inventory_available=false`: severity **HIGH**, `requires_hitl=true`. Recommended
  action: identify the device at that IP (router DHCP table, phone Wi-Fi details) and
  restore the NetAlertX connection so future lookups work. Never escalate to CRITICAL on
  this basis.
- When `inventory_available=true` and the device is absent from it: severity HIGH,
  `requires_hitl=true`; recommend identifying the device and reviewing HA users and 2FA.

### External source (non-RFC-1918 IP)
- `severity_override="CRITICAL"`, `requires_hitl=true`. Someone outside the LAN is
  reaching HA's login endpoint. Recommend reviewing exposure (port forwarding, reverse
  proxy), enabling 2FA, and `ip_ban_enabled` / `login_attempts_threshold` in `http:`.

### ip-ban
- Same identification flow. `requires_hitl=true` — HA has banned the IP and the user
  should know, even when the device turns out to be their own (they will need to remove
  it from `ip_bans.yaml` to unblock it).

## Grounding
Every fact in the explanation (UA, URL, device name, vendor) must appear in a tool
output or in the notification text. If the log search returned nothing, say the UA is
unknown.
