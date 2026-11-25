---
signature: device:offline:lifx
---
# LIFX — device offline investigation

## How LIFX works in HA

- **One config entry per bulb.** Older integrations used a shared entry; that is long
  gone. Each bulb is its own entry with a stable entry id.
- **LAN UDP polling.** HA polls each bulb every 10 seconds over the local network.
  Discovery runs every 15 minutes via UDP broadcast, Zeroconf, HomeKit, and DHCP.
- **"No response from \<ip\> after 11 attempts"** in the LIFX log section means the
  bulb's LAN UDP channel has stopped responding. The bulb is physically present (11 × 10 s
  ≈ 110 s elapsed), but its firmware is not answering.
- **Reload by entry id, not domain.** `reload_integration(domain="lifx")` reloads one
  arbitrary LIFX entry, not the one that is offline. Always use the entry id from the
  initial context.

## Restart / identify buttons

The LIFX `button.restart` and `button.identify` entities are controls that the bulb
itself must respond to. They become `unavailable` when the bulb is offline and cannot be
used to recover it. Never suggest them as a fix when the bulb is already unreachable.

## Matter twin

If the initial context shows a twin via Matter (`Same device via other integrations:`),
the bulb has power and network — the fault is in the LIFX LAN path only.

## Decision rules

**NetAlertX shows the bulb `present on LAN`** (or the Matter twin is online)
**AND the LIFX log shows "No response after 11 attempts"**:
1. The bulb's LAN UDP channel is hung. Reload **the LIFX entry id from the initial
   context** once: `reload_integration(domain_or_entry_id="<entry id>", reason="LIFX LAN UDP channel unresponsive")`.
2. Wait for the reload to finish and check the entity state.
3. If still unavailable: `outcome="needs_action"`. Tell the user to power-cycle the
   bulb. If a Matter twin is online, note that the bulb continues to work via Matter.
4. If the reload fixed it: `outcome="fixed"`.

**The entry is in `setup_error` after a reload**:
- Reload again with the same entry id. A cold restart of the LAN channel sometimes needs
  two reloads.
- If still in `setup_error`: `outcome="needs_action"`. Do not suggest re-adding the entry;
  that would create a duplicate and lose the entity history.

**NetAlertX shows the bulb `absent from LAN`** (and no Matter twin is online):
- The bulb has lost power or Wi-Fi. No reload will fix it.
- `outcome="needs_action"`. Tell the user to check power and the Wi-Fi band (LIFX uses
  2.4 GHz only).

**No NetAlertX and no twin evidence in initial context**:
- Call `check_entity_status(entity_id="<lifx entity id>")` to look for a Matter twin
  that did not appear in the initial context.
- If the result shows `device_twins` with a twin whose integration is `matter` and whose
  entity state is **not** `unavailable`: the bulb has power and network — treat as
  "present on LAN" and apply the LAN decision rule above (reload the entry id).
- Otherwise (no twin found, or the twin is also `unavailable`):
  - Read the LIFX log section with `read_logs(lines=100, log_source="ha_core", domain="lifx")`.
  - Look for "No response after 11 attempts", a disconnect, or a Traceback near the
    offline-since time.
  - If log shows "No response": reload the entry id, as above.
  - If log is clean: check the entry state with `get_integration_diagnostics`; if it shows
    `setup_error`, reload the entry id.

## Prediction

- After a reload: `{"kind": "recovers_after_reload", "within_minutes": 15}`.
- If the user will power-cycle (you told them to): `{"kind": "recovers_without_action", "within_minutes": 30}`.
- Bulb absent from LAN: `{"kind": "stays_offline", "within_minutes": 60}`.
