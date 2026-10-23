---
signature: update:addon:*
---

# Add-on update analysis

**Context:** Add-on updates are generally low-risk compared to Core/OS. Most add-on patch bumps
with no "Breaking" section in the release notes can be applied without user review.

## Verdict rules

- **No breaking changes, patch/minor bump**: `safe_to_update=true`, `instance_impact="none"`,
  `create_hitl_card=false`.
- **Breaking changes listed, or major version bump**: `safe_to_update=false` (user should review),
  `instance_impact="high"`, `create_hitl_card=true`.
- **Release notes unavailable or too short to analyse**: `safe_to_update=true`,
  `create_hitl_card=true`, recommend manual review.

## Investigation steps

1. `get_update_release_notes(entity_id=<entity>)` — fetch notes for the target version.
   - If body is < 500 chars or returns an error: record as unavailable, set `create_hitl_card=true`.
   - Look for a "Breaking" or "Migration" section. If absent: patch bump is safe.

2. Check integration dependencies:
   - Mosquitto/MQTT add-on update → integrations that use the MQTT broker
     (check `ha apps info mosquitto`) are affected if the broker port or auth config changes.
   - File editor, SSH, or terminal add-ons: rarely break HA but may change config paths.
   - Run `run_ha_command("ha apps info <slug>")` to confirm current state and dependents.

3. If breaking changes mention a config schema change: `read_file("/addon_configs/<slug>/")` to
   check whether the installed config matches the old schema.

4. `check_config_against_breaking_change(config_key, description)` for each flagged item.

## Output guidance

- `risk_score`: "low" for patch bumps without breaking changes; "medium" for major bumps or
  dependency chain risk; "high" only when a known breaking change affects this install.
- `create_hitl_card`: false for low-risk routine patches; true whenever the user should review.
- `prerequisites`: note any required service restarts or config changes before applying.
