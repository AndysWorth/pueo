# ADR 048 — NetAlertX as network-layer evidence: pull over push

**Status:** Accepted  
**Date:** 2026-10-22  
**Issues:** #953 (umbrella), #958 (S2b)

---

## Context

Pueo's device-availability monitor (ADR 047) detects when HA reports devices
`unavailable`, but HA alone cannot distinguish between two very different root causes:

1. The device is **physically absent from the network** (power off, Wi-Fi loss, firmware
   update, out of range) — no integration action can fix this.
2. The device is **on the LAN but HA has lost its integration or cloud connection** —
   reloading the integration is sufficient (and may already be the right first step).

Without a network-layer witness, the agent must infer the cause from integration logs and
sibling-device patterns, which is slower and sometimes ambiguous.

NetAlertX (running as an HA app) already scans the LAN via ARP every few minutes and
maintains an inventory of every device it has ever seen, including MAC address, last-seen
time, current IP, and whether the device appeared in the most recent scan
(`devPresentLastScan`). This is exactly the evidence needed.

---

## Decision

**NetAlertX is Pueo's network-layer witness. Pueo is NetAlertX's caretaker.**

The integration uses a **pull model**: Pueo queries the NetAlertX REST API on demand,
rather than receiving pushed events via MQTT or webhooks.

### Why pull, not push

| Criterion | Pull | Push (MQTT/webhook) |
|---|---|---|
| Works when HA is degraded | Yes — direct REST to NetAlertX | No — webhook goes through HA |
| HA writes required | None | Creates automation, writes `automations.yaml` |
| Fits Pueo's read-only investigation model | Yes | No |
| NetAlertX config changes needed | Token only | `MQTT_RUN=enabled` + broker config |
| Resilience to push-path misconfiguration | Not affected | Silent failure |

The existing REST client (`NetAlertXAPIClient`) returns the full device list in a single
call.  Filtering by MAC or IP to find a specific device is O(n) over the device list and
fast enough for on-demand use.

### Device-offline integration (S2b)

`build_presence_context()` in `utils/ha/device_availability_investigation.py`:
1. Resolves each offline device's HA `device_id` to a candidate list via
   `candidates_for_device()` in `utils/ha/network_identity.py`.
2. Calls `NetAlertXAPIClient.get_devices()` once per investigation (not per device).
3. Returns `(presence_text, unmapped_device_ids)` — the presence block prepended to
   `initial_context` and a list of device IDs that had no MAC/IP mapping.

**Mapping order** (best-evidence first):
1. Manual link from `user_prefs` (`netalertx:link:<device_id>` = MAC)
2. Every MAC address in the HA device registry connections
3. IP connections in the HA device registry
4. Config-entry host/address/ip_address (read from `/config/.storage/core.config_entries`
   via `load_config_entry_hosts(ssh_client)`)

Unmapped devices — those with no candidates from any of the four sources — are reported as
`"unknown (no MAC/IP mapping)"` in the presence block rather than `"absent from LAN"`.
`run_device_availability_investigation` calls `executor.set_unmapped_device_ids()` so the
`finish_device_availability` tool includes `unmapped_device_ids` in the card payload.

**Manual links (`/network-links` dashboard page):** Users can permanently associate any HA
device with a NetAlertX device (by MAC) via `POST /network-links/save`. Links are stored in
`user_prefs` as `netalertx:link:<ha_device_id>` = MAC (lowercase). A device-offline card
with `unmapped_device_ids` in its payload shows a "Link network device" button that opens
this page. Unlink via `POST /network-links/unlink`.

The seed `seed_device_offline.md` owns the decision rule:
- `present on LAN` → integration or cloud fault → proceed with thread-death / reload path
- `absent from LAN` → physical layer fault → `outcome="needs_action"`, tell user to check
  power and Wi-Fi
- Unknown mapping or not found → fall back to log analysis

The ws_client is passed from `poll_device_availability` → `_run_device_availability_investigation` → `run_device_availability_investigation` → `build_presence_context` so the MAC resolution uses the live device registry.

### Event feed (S3)

`netalertx/event_poll.py` implements a `netalertx_event_poll` supervisor loop that
polls `/events` on a configurable interval (`NETALERTX_EVENT_POLL_INTERVAL_MINUTES`,
default 5) using a high-water mark (`netalertx:event_hwm` in `user_prefs`) to avoid
re-routing seen events.  New-device appearances (`eveEventType == "New Device"`) are
routed through `NetAlertXEventRouter.route_rest_event()` → `_handle_new_device()` →
HITL notification card (`CARD_TYPE_NETALERTX_NEW_DEVICE`), with deduplication within
a 60-second window.  Device-down/up events from the event feed are logged as context
only; device-offline alerting remains ADR 047's role to avoid double-triggering.

The event poll loop replaces the former MQTT/webhook integration (`aiomqtt` dependency,
Mosquitto listener config, HA automation for webhook callback).  The MQTT credential
config keys (`netalertx.mqtt_subscribe`, `mqtt_user`, `mqtt_password`) have been
removed.  Existing Docker installs no longer require Mosquitto routing (the Docker
installer `_step8_route_mqtt` / `_step9_create_webhook` steps are gone).

---

## Consequences

**Positive:**
- Device-offline investigations immediately have the key disambiguation signal.
- No HA writes, no MQTT dependency, no new HA automations.
- Degrades gracefully: absent token, absent NetAlertX, or API error all result in an
  empty presence block; the investigation continues with log analysis.

**Negative:**
- NetAlertX ARP scans have a latency of up to the scan interval (a few minutes).  A
  device that just came back may not appear as present yet.  The seed acknowledges this
  by using `devPresentLastScan` as a soft signal, not a hard gate.
- MAC resolution requires a working WebSocket connection to HA.  If HA is fully down, the
  presence context will be empty.  This is acceptable because an investigation into a
  down device when HA is also down is escalation territory anyway.

---

## Related

- ADR 047 — Device availability monitor (the consumer of this evidence)
- ADR 043 — Read-only SSH for investigations (presearch pattern)
- ADR 017 — Chat tool parity (ws_client threaded through the investigation chain)
- ADR 028 — Pueo MCP server (`get_network_presence` registered as a read-only MCP tool)
