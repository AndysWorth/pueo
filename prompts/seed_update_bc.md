---
signature: update:core_bc:*
---

# BC assessment — verdict rules

These rules are authoritative for every `update:core_bc:*` assessment session.

## Verdict rules

- **`yes`**: a tool output this session confirms a matching entity type, config key, or
  automation/dashboard reference. Entity *count* for the integration alone is not enough.
- **`no`**: entity count is zero, the config key named in the BC is absent, and no
  automation or dashboard hits were found.
- **`unknown`**: the BC concerns a device capability (e.g. firmware, BLE, Zigbee radio),
  a browser UI behaviour, or a cloud-service account — none of which can be verified
  offline. Also use `unknown` when evidence is genuinely ambiguous.

## Integration-specific rules

- **MQTT YAML entities**: only `yes` when `mqtt:` is a top-level key in `configuration.yaml`
  *and* the entity sub-key (e.g. `mqtt.sensor:`, `mqtt.binary_sensor:`) is present.
  `mqtt` as a loaded component (default_config) does not imply YAML entities.
- **Philips Hue / effect**: `yes` only when `effect: null` or `effect: None` appears in
  an automation YAML tool output from this session. Automation *count* on the domain is
  not evidence of the specific key.
- **Matter climate/cover/TVOC**: check entity type breakdown. `yes` only when climate,
  cover, or TVOC sensor entities are present (not just lights or switches).
- **Device tracker scanner platforms**: `yes` only when a tracker uses a LAN scanner
  platform (e.g. `nmap_tracker`, `ping`, `bluetooth`) — `mobile_app` trackers are unaffected.
- **Calendar / Sun state triggers**: `yes` only when automations use `at` offsets on
  `sun.sun` state changes or `calendar.*` state triggers. Plain `sun.sun` state triggers
  without offset are unaffected.
- **Frontend / Auth / homeassistant**: these are core components with no entities.
  Use `unknown` unless `check_config_against_breaking_change` finds a specific key.
- **Python dependencies / HAOS**: HAOS manages Python; `no` for HAOS installs unless
  a custom Python component is confirmed in the config.

## Config-fix grounding

A `config_fix` is valid only when its `old` text appears verbatim in a tool output
this session. Never invent config text that was not observed.
