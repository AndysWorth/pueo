# Security Notification Investigation

Trigger: failed login notification, suspicious device, unknown IP, http_login alert

## Approach

1. Call read_logs(200) to extract the source IP address from the notification or log entry.
2. Call get_device_info("<ip>") — returns MAC address, OUI vendor, randomized-MAC flag,
   reverse DNS hostname, NetAlertX device name, DHCP hostname from router leases, and
   **is_pueo_host** (true when the IP is one of Pueo's own local network interfaces).
3. Call query_netalertx("events") to check for prior scan appearances and known device history.
4. Interpret the enriched data and call finish_chat with:
   - Device identity (vendor, hostname, known/unknown status)
   - Whether this is a recognized household device or a genuinely unknown source
   - Recommended action (dismiss / investigate further / block)

## When is_pueo_host is true

If `get_device_info` returns `is_pueo_host: true`, the source is the machine running
Pueo. This is normal during development (dev scripts, manual API calls). Possible causes:
- A dev script running against HA with an expired or wrong token
- Pueo's own HA WebSocket/REST client connecting with a bad token
- A debug session that hit the HA API

**Do not** report this as a suspicious external login. Call finish_chat with:
- Device identity: "Pueo host (<ip>)"
- Outcome: "likely Pueo or dev script — check HA token validity"
- Recommended action: dismiss
