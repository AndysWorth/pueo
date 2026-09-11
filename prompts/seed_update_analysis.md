# Runbook: HA Update Breaking-Change Analysis

## When to use
Use this runbook when analysing an available Home Assistant Core, OS, or add-on update for breaking changes that may affect this installation.

## Standard investigation steps

### 1. Fetch release notes
Call `get_update_release_notes` with the target version. If the notes body is short (<500 chars), it is likely a stub — note this and set `safe_to_update=true` with a recommendation to review manually.

### 2. Identify breaking changes
Look for sections titled "Breaking changes", "Deprecations", or "Migration notes". Extract each as a separate item.

### 3. Cross-reference with this installation
For each breaking change:
- Call `check_config_against_breaking_change(config_key, description)` for any config key mentioned.
- If a YAML key is mentioned (e.g. `recorder:`, `homeassistant:`), check if it appears in `/config/configuration.yaml`.
- If an integration is mentioned, call `run_ha_command("ha apps list")` or check installed integrations from context.

### 4. Check Pueo command catalog
Call `get_pueo_command_catalog` and check if any catalog command appears in the release notes' CLI section (e.g. renamed command, removed flag). This is especially important for OS updates that may change the `ha` CLI.

### 5. Known patterns by scenario

**Core update with no breaking changes**
- `safe_to_update=true`, `instance_impact="none"`, `create_hitl_card=true`
- Recommendation: "No breaking changes. Safe to approve."

**Core update with deprecated config key found in this install**
- `safe_to_update=false`, `instance_impact="high"`, `create_hitl_card=true`
- Include the deprecated key in `affected_config_keys` and a fix in `proposed_config_fixes`
- Recommendation: "Apply the config fix before approving this update."

**OS-only update**
- OS updates rarely have YAML breaking changes. Focus on CLI changes that affect Pueo.
- `safe_to_update=true` unless CLI risks found, `create_hitl_card=true`

**Breaking CLI change affecting Pueo**
- Add the command to `pueo_command_risks`
- `safe_to_update=false`, `instance_impact="high"`, `create_hitl_card=true`
- Recommendation: "A Pueo SSH command has been renamed or removed in this version. Manual review required before approving."

**HACS integration incompatibility mentioned in notes**
- Check if the integration is installed via `run_ha_command("ha apps list")`
- If installed: `instance_impact="high"`, add to `affected_config_keys`
- If not installed: `instance_impact="none"`

### 6. When release notes are unavailable
Set `safe_to_update=true`, all lists empty, `instance_impact="none"`, `create_hitl_card=true`.
Recommendation: "Release notes not available. Review manually before approving."
