# Security Notification Investigation

Trigger: failed login notification, suspicious device, unknown IP, http_login alert

## Approach

1. Call read_logs(200) to extract the source IP address from the notification or log entry.
2. Call investigate_device("<ip>") — returns MAC address, OUI vendor, randomized-MAC flag,
   reverse DNS hostname, NetAlertX device name, and DHCP hostname from router leases.
3. Call query_netalertx("events") to check for prior scan appearances and known device history.
4. Interpret the enriched data and call finish_chat with:
   - Device identity (vendor, hostname, known/unknown status)
   - Whether this is a recognized household device or a genuinely unknown source
   - Recommended action (dismiss / investigate further / block)
