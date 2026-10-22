---
signature: device:offline:*
---

# Device offline investigation — cloud-push integrations

## Triggers
A device (or group of devices sharing a config entry) has been unavailable for longer than
the grace period. Common integrations: econet, ecobee, nest, august, ring, smartthings,
myq, rachio, and any cloud-push integration that maintains a persistent connection.

## Key distinction: cloud-push vs local/network

**Cloud-push thread death** (most common cause for single-vendor devices):
- HA log shows `Unexpected disconnect`, `MQTT disconnect`, `WebSocket error`, `TimeoutError`,
  or `Uncaught thread exception` near the offline-since time.
- The integration's push thread died. HA does not auto-restart it. The integration only
  recovers on its next hourly poll (`:02` or similar) — which could be 58 minutes away.
- **Fix: `reload_integration(domain=<domain>)`** restarts the thread immediately.
- After reload: check entity states or call `get_entity_history` to confirm devices came back.

**Vendor-side cloud outage**:
- Multiple unrelated integrations affected, or status page shows incident.
- No HA log errors.
- **No automated fix**: `outcome="monitoring"`. Tell the user to wait.

**Local/network device offline** (Zigbee, Z-Wave, local LAN device):
- Device is powered off, firmware updating, or out of range.
- Sibling devices on the same integration remain online.
- **No automated fix**: `outcome="needs_action"`. Recommend checking power and range.

**Integration config entry error**:
- `get_integration_diagnostics` shows a config entry in `failed_setup` or `setup_error` state.
- Recommend `reload_integration` (gated) or re-authentication in HA UI.

## Investigation steps

1. `read_logs(lines=40, log_source="ha_core")` — look for disconnect, Traceback, MQTT error,
   or "Uncaught thread exception" near the offline-since time.
2. If log confirms cloud-push thread death: call `reload_integration(domain=<domain>)`.
3. If log is clean: `get_integration_diagnostics(domain=<domain>)` — check entry state.
4. If multiple vendors offline: check `fetch_url` against a vendor status page (if known).
5. `get_entity_history` on a representative entity to confirm the offline-since time and
   count prior blips (3+ blips in 7 days = recurring cloud instability).
6. `get_logbook` to see the sequence of state changes.

## Recurring outages

If `get_entity_history` shows 3+ offline episodes in 7 days and each starts around the same
time of day (e.g. 07:15–07:30 local time), the cause is likely a recurring vendor-cloud event
(daily MQTT server rotation, certificate renewal, etc.). Document this in `remember()` as an
instance fact and include it in the summary so the user knows this is a pattern.

## Sibling device heuristic

If some devices under the same config entry are online and others are offline, the cause is
local (power, range, firmware). If all devices under the entry are offline, the cause is the
integration or vendor cloud.

## Network presence (NetAlertX)

When the initial context includes a `NetAlertX network presence:` block, use it as
the first disambiguating signal before reading logs:

- **`present on LAN`** — the device is physically reachable on the local network.
  HA saying "unavailable" while the device is on the LAN means the fault is in the
  integration or vendor cloud, not in the device itself. Proceed with
  cloud-push thread death or config entry diagnostics steps above.

- **`absent from LAN`** — the device is not visible in any recent ARP scan.
  The fault is a physical layer issue (power off, Wi-Fi loss, firmware update, out
  of range). No integration reload will fix this. Set `outcome="needs_action"` and
  tell the user to check power, Wi-Fi signal, or physical presence.

- **`not found in NetAlertX`** — the device has no MAC or IP mapping in NetAlertX's
  inventory, which may mean it has never been scanned or uses a MAC that NetAlertX
  hasn't seen. Fall back to log analysis.

- Presence block absent or `netalertx_status` ≠ `ok` — NetAlertX is not available.
  Fall back to log analysis and sibling heuristics.
