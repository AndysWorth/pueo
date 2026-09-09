# Runbook: Lovelace unregistered entity investigation

## Trigger
An entity appears in a Lovelace dashboard but is absent from the HA entity registry
(not returned by config/entity_registry/list). It may or may not have live state.

## Root causes and investigation steps

### 1. Sub-platform entity (most common benign case)
Some integrations create entities in a *different* domain via HA's sub-platform mechanism.
Example: the `sun` integration creates `binary_sensor.sun_rising` via the `sun.binary_sensor`
sub-platform. The entity has live state but never appears in the registry under its integration
domain.

**Detection**: Call `get_ha_components()` and check whether `<integration>.<entity_domain>`
appears in the components list. Also call `check_entity_status(entity_id)` — if
`sub_platform_match` is true, this is the cause.

**Action**: Benign. Do NOT create a card. Log as resolved silently.

### 2. YAML entity without unique_id
Entities defined in YAML (e.g., `sensor:`, `binary_sensor:`, `input_boolean:`) without a
`unique_id` field are valid and functional — they just never enter the entity registry.
They appear in `hass.states` but not the entity registry.

**Detection**: `check_entity_status` returns `has_state=True` and `in_registry=False`.
No sub-platform match. No config entry for the domain.

**Action**: Create a `ha_config_issue` card advising the user to add `unique_id` to the
YAML definition. Propose a unique_id value based on the entity_id (replace `.` with `_`).
This is a low-priority advisory — the entity works fine without it, but the unique_id
enables renaming, disabling, and area assignment from the UI.

### 3. Not-loaded config entry
The config entry exists (visible in `get_config_entries_all()`) but its `state` is not
`"loaded"` — it may be `"not_loaded"`, `"failed_unload"`, or `"setup_error"`.

**Detection**: `check_entity_status` shows `config_entries_with_domain` with a non-loaded
entry. Cross-check with `get_config_entries_all()` filtering by domain.

**Action**: Create a `ha_config_issue` card noting the integration is not loaded and advising
the user to check the integration settings in HA UI → Settings → Integrations.

### 4. Misconfigured or removed entity
The entity_id does not match any live state, no config entry covers its domain, and no
sub-platform explanation exists. The dashboard reference is likely stale or wrong.

**Detection**: `check_entity_status` returns `has_state=False`, `in_registry=False`,
no sub-platform match, no relevant config entries.

**Action**: Create a `ha_config_issue` card noting the entity appears to be missing and
advising the user to update the dashboard card to reference a valid entity_id.

## Grouping strategy
Group entities that share the same root cause and the same config entry (if applicable)
into a single finding. This reduces notification noise.

## Card key format
`ha_config_issue:<sorted entity_ids joined by ":">` — enables stable reconciliation across
polling cycles.
