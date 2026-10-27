---
signature: device:offline:*
---

# Device offline investigation

## Triggers
A device (or group of devices sharing a config entry) has been unavailable for longer than
the grace period. The initial context lists each device with its entity_ids, its
offline-since time, and any recurrence facts or previous investigations for it. Only use
the entity_ids listed there, and never guess one.

## Before anything else: was HA just restarted?
While HA is running, an entity keeps its last known state. A device that the user switched
off can keep looking fine for hours, until a restart or integration reload shows it as
`unavailable`. If the offline-since time is within a few minutes of an HA restart, Core
update or host reboot in the context, the device was most likely **already off** before
the restart.
- Set `outcome="monitoring"`, and tell the user to answer **"I turned these off"** on the
  card if they did. Pueo then stops alerting on the device until it is next seen online.
- Don't reload the integration, and don't diagnose a cloud or network fault from that one
  sighting.

## What the evidence can tell you

**The integration's connection dropped** (cloud-push or local push):
- The log for this domain shows a disconnect, `MQTT disconnect`, `WebSocket error`,
  `TimeoutError` or `Uncaught thread exception` near the offline-since time.
- `reload_integration(domain=<domain>)` restarts the connection. Afterwards, check an
  entity's state with `get_entity_history` to confirm the devices came back.

**The config entry failed**:
- `get_integration_diagnostics` shows the entry in `setup_error`, `setup_retry` or
  `failed_setup`, or the log shows the integration's setup failing.
- Recommend `reload_integration` (gated) or re-authentication in the HA UI.

**The device itself is off or unreachable**:
- Siblings on the same config entry are still online, or NetAlertX shows the device
  absent from the LAN.
- No reload will help. Set `outcome="needs_action"` and recommend checking power and
  Wi-Fi, unless the device is one the user switches off (TVs, media players, seasonal
  lights), in which case use `outcome="monitoring"`.

**Vendor outage**:
- A status page fetched with `fetch_url` reports an incident, or several unrelated cloud
  integrations dropped at the same time with no local errors.
- No automated fix. Set `outcome="monitoring"`.

TVs and media players (Cast, DLNA, `androidtv`, `roku`) go unavailable when powered off or
asleep. That is expected, not a fault.

If the evidence supports none of these, say the cause is unknown. Don't fill the gap with a
plausible story.

## Recurrence facts are counts, not causes
A recurrence fact such as "was offline 4× in 14 days (typical duration 600 min, usually
starting 22:30)" records how often the device dropped and for how long. It says nothing
about why. Use it to check whether this episode fits the usual pattern, and say so in the
summary. Never turn it into a cause (a vendor schedule, a sleep cycle, a cloud glitch)
that no tool output shows.

## Investigation steps
1. Check the initial context for a restart, update or reboot just before the offline-since
   time (see above).
2. `read_logs(lines=40, log_source="ha_core", domain=<domain>)`: look for a disconnect,
   Traceback or setup error near the offline-since time.
3. If the log is clean: `get_integration_diagnostics(domain=<domain>)` and check the entry
   state.
4. `get_entity_history(entity_id=<listed entity_id>, hours=168)` on one representative
   entity to see when it went offline and how often in the last week.
5. If the log shows a dropped connection and the device is still offline:
   `reload_integration(domain=<domain>)`.
6. If several vendors are offline at once: `fetch_url` against a known vendor status page.

## Sibling device heuristic
If some devices under the same config entry are online and others are offline, the cause is
local to those devices (power, range, firmware). If all of them are offline, look at the
integration or the vendor cloud.

## Network presence (NetAlertX)
When the initial context includes a `NetAlertX network presence:` block, check it before
reading logs:
- **`present on LAN`**: the device is reachable, so the fault is in the integration or the
  vendor cloud. Continue with the log and diagnostics steps.
- **`absent from LAN`**: power, Wi-Fi or physical presence. No reload will fix it.
- **`unknown (no MAC/IP mapping)`** or **`not found in NetAlertX`**, or no presence block:
  fall back to logs and siblings. Treat "unknown" as inconclusive — do not assume physical
  fault.

## Prediction (lets Pueo check your conclusion itself)
Give `finish_device_availability` a `prediction` whenever your conclusion implies what the
devices will do next. Pueo watches the devices and verifies the session from what actually
happens, without waiting for the user.
- You reloaded the integration because its connection dropped:
  `{"kind": "recovers_after_reload", "within_minutes": 15}`.
- The connection dropped or the vendor has an outage, and you expect it to come back by
  itself: `{"kind": "recovers_without_action", "within_minutes": <N>}`, where N comes from
  the evidence (for example the typical duration in a recurrence fact).
- The device is off, unplugged or absent from the LAN:
  `{"kind": "stays_offline", "within_minutes": 60}`.

Leave `prediction` out when the cause is unknown.

## Verification question
Ask one yes/no question the user can answer from what they know, e.g. "Was the Kasa plug
switched off at the wall?". Set `fact_if_yes` only to a fact the evidence and that answer
together establish, such as "Kitchen TV (cast) is switched off at night by the user."
