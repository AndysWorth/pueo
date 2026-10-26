---
signature: device:offline:post_restart
---

# Devices offline after a Home Assistant restart

## Triggers
Right after Home Assistant restarts (Core update, host reboot, manual restart), Pueo's
device check finds devices from one or more integrations unavailable. All of them are
investigated together in one session. The initial context lists the HA start time,
any update or reboot events around it, and every affected device with its integration
and entity_ids.

## What a restart does to device state
While HA is running, an entity keeps its last known state. A device that the user
switches off at the wall can keep looking fine in HA for hours, until HA reloads the
integration. A restart reloads every integration, so all the devices that were
already off show up as `unavailable` at once. That is why a restart can expose many
devices from unrelated integrations together.

The restart itself is not evidence of a fault in any one integration.

## Decision rule
- **Several unrelated integrations, and no setup errors in the logs**: the devices
  were most likely already off before the restart (on purpose, unplugged or
  seasonal). Set `outcome="monitoring"`. In the summary, list the devices and tell
  the user to answer **"I turned these off"** on the card if they did. Pueo then
  stops alerting on them until they come back online.
- **One integration whose config entry failed to load** (diagnostics show
  `setup_error`, `setup_retry` or `failed_setup`, or the log shows the integration's
  setup failing after the start time): this is a real post-restart fault, often
  caused by the update that was just applied. Report that integration, and
  recommend `reload_integration` or checking the integration's release notes. Treat
  the other integrations' devices as already off.
- **Every device of one integration offline, while its siblings on other
  integrations are fine, and NetAlertX shows them present on LAN**: the integration
  didn't reconnect after the restart. `reload_integration` for that domain is
  reasonable.

Don't diagnose cloud-server rotations, MQTT drops or daily sleep cycles from a
single post-restart sighting. One restart gives you one data point.

## Investigation steps
1. Read the restart events in the initial context. Note whether the restart came
   from an update, a host reboot or nothing Pueo recorded.
2. `get_integration_diagnostics(domain=<domain>)` once per affected integration to
   check whether its config entry loaded.
3. `read_logs(lines=60, log_source="ha_core", domain=<domain>)` once per affected
   integration and look only for its setup errors after the start time.
4. If one device stands out, use `get_entity_history` on one of the entity_ids
   listed in the context (never guess an entity_id) to see whether it was off
   before the restart.
5. Call `finish_device_availability` once for the whole group.

## Verification question
When you conclude the devices were already off, ask:
"Were these devices switched off before the restart?" and leave `fact_if_yes` empty.
The card's "I turned these off" button records the per-device answer.
