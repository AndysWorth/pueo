---
signature: update:core_bc:*
---

# BC assessment — verdict rules

These rules are authoritative for every `update:core_bc:*` assessment session.
They are release-independent: the same rules apply to every HA Core update.

## Verdicts

Call `finish_bc_assessment` with one of:

- **`action_needed`**: a concrete change is confirmed necessary.  Set `action`
  (what to do) and `when` (`before_update` or `after_update`).
- **`no_action`**: confirmed not affected — entity count is zero, the config key
  named in the BC is absent, and no automation or dashboard hits exist.
- **`user_check`**: only the user can verify (browser behaviour, remote access,
  device firmware, cloud account, Zigbee/BLE radio).  Set `user_question` — a
  concrete yes/no question the user can answer.

**`not_assessed` is set by code, never by you.**  When evidence is genuinely
ambiguous but you can still ask a concrete question, use `user_check`.

## Grounding rules

- Entity *count* for an integration alone is not enough for `action_needed`.
  A matching entity *type* (or config key, or automation hit) is required.
- Use `no_action` when: entity count is zero **and** the config key is absent
  **and** token searches returned 0 hits.
- Use `no_action` for HAOS installs when the BC concerns Python environment or
  pip dependencies — HAOS manages Python; custom Python components are rare.
- Use `user_check` for browser UI behaviour, remote-access setup, device
  firmware, cloud-service accounts, and BLE/Zigbee radio capabilities.

## `when` guidance

- `before_update`: the action must happen before applying the update (config
  edit, schema migration, uninstall of a conflicting integration).
- `after_update`: the action is a follow-up after the update is applied (rename
  entities, update automations to use a renamed service, enable a capability that was not available before).

## Entity-type matching

For a BC that specifies an entity type (e.g. climate, cover, sensor with a
specific device class), `action_needed` is correct only when that specific type
is present, not just any entity from the domain.  Check "By type" in the
evidence.

## Config-fix grounding

A `config_fix` is valid only when its `old` text appears verbatim in a tool
output this session.  Never invent config text that was not observed.
