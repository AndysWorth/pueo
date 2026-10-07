# Automation Authoring

Trigger: create automation, add automation, write automation, make automation, build automation,
create a rule, automate lights, automate when, turn on when, notify me when, schedule automation

## Approach

Use `propose_automation` to draft and validate a new Home Assistant automation. This tool:
1. Validates the trigger, condition, and action via HA's `validate_config` WebSocket endpoint.
2. If validation fails, returns the error — fix the YAML and call `propose_automation` again.
3. On passing validation, raises a HITL card for human approval.
4. On approval: creates a backup, writes the automation via REST, reloads, and verifies the
   entity appears in the state machine.

Never call `apply_fix` or write to `configuration.yaml` for automations — use `propose_automation`.

## Trigger shapes (HA 2026+)

```yaml
# State change
- platform: state
  entity_id: binary_sensor.motion_sensor
  to: "on"

# Time trigger
- platform: time
  at: "22:00:00"

# Sun event
- platform: sun
  event: sunset
  offset: "-00:30:00"

# Numeric state
- platform: numeric_state
  entity_id: sensor.temperature
  above: 25

# Template (flexible)
- platform: template
  value_template: "{{ states('sensor.power') | float > 200 }}"

# Tag scanned
- platform: tag
  tag_id: abc123

# MQTT
- platform: mqtt
  topic: home/button/press
```

## Action shapes (HA 2026+)

```yaml
# Call a service
- action: light.turn_on
  target:
    entity_id: light.living_room
  data:
    brightness: 180

# Delay
- delay: "00:05:00"

# Condition check mid-action
- condition: state
  entity_id: binary_sensor.door
  state: "off"

# Notification
- action: notify.mobile_app_phone
  data:
    message: "Motion detected"

# Scene
- action: scene.turn_on
  target:
    entity_id: scene.evening
```

## Condition shapes

```yaml
# State
- condition: state
  entity_id: binary_sensor.presence
  state: "home"

# Time window
- condition: time
  after: "08:00:00"
  before: "22:00:00"

# Numeric state
- condition: numeric_state
  entity_id: sensor.brightness
  below: 100

# Template
- condition: template
  value_template: "{{ states('input_boolean.guest_mode') == 'off' }}"
```

## Execution modes

| Mode | Behaviour |
|---|---|
| `single` | Ignore new triggers while running — safe default |
| `restart` | Cancel and restart on new trigger |
| `queued` | Queue new triggers |
| `parallel` | Run all triggers concurrently — use sparingly |

## Required call pattern

Before calling `propose_automation`, gather enough information to populate all fields:
- **alias** — human-readable name: "Turn on porch light at sunset"
- **description** — one sentence: "Turns on porch light 30 minutes before sunset"
- **mode** — default to `"single"` unless the user specifies otherwise
- **trigger** — one or more trigger objects
- **action** — one or more action objects
- **condition** — (optional) zero or more condition objects

Call `check_entity_status(entity_id=...)` to confirm entity IDs exist before building
the automation. A validation error for an unknown entity is a hard stop — do not guess names.

After `propose_automation` returns successfully, the card is raised and waiting for user
approval. Call `finish_chat` to report that the draft is ready for review, including
the alias and what the automation will do. Do not call `apply_fix` or `run_ha_command`.

## Common patterns

- **Motion-activated light**: state trigger on `binary_sensor.*` to `"on"` → `light.turn_on`.
  Add a second path or use `wait_for_trigger` to turn off after N minutes of no motion.
- **Presence-based scene**: state trigger on `person.*` to `"home"` → `scene.turn_on`.
- **Time-of-day notification**: time trigger at HH:MM → `notify.*`.
- **Energy threshold alert**: `numeric_state` trigger above threshold → notification.

## If validate_config returns an error

The error message names the specific component (`trigger`, `condition`, or `action`) and a
description of what is wrong. Fix only that component and call `propose_automation` again
with the corrected YAML — do not change the valid components.

## Safety rules

- Only `propose_automation` authors automations — never `apply_fix`, never manual SSH writes.
- Automations Pueo creates use the `pueo_auto_` unique_id prefix.
- `propose_automation` is not available to automated repair loops — only in chat sessions.
