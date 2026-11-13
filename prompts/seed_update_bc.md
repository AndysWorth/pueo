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
- **`no_action`**: confirmed not affected — nothing in the evidence matches what
  the BC changes.
- **`user_check`**: whether the BC applies depends on something outside Home
  Assistant's configuration that only the user knows.  Set `user_question` — a
  concrete yes/no question the user can answer.

**`not_assessed` is set by code, never by you.**

## Decide in this order

1. **Does the BC depend on something the evidence cannot see?**  That is: which
   browsers or tablets open the UI, how Home Assistant is reached from outside
   the home (public IP, VPN, reverse proxy), device firmware versions,
   cloud-service accounts, BLE/Zigbee radio capabilities.  An "Outside the
   evidence (only the user knows)" line means yes.  If yes → `user_check`
   with a question about exactly that thing, and stop: steps 2–4 do not apply.
   Zero entities, "core component (no entities)", a "Config keys present" line
   and 0 search hits do **not** settle these: the evidence never contains
   browser or network-access facts, so nothing in it proves or disproves them.
2. **Python environment or pip-dependency BCs** on an HAOS install → `no_action`.
   HAOS manages Python.
3. **Is there an evidence line proving this install uses the thing that
   changes?**  Proof is one of: a "Config refs" / "Dashboard refs" line, a
   "Tokens checked (N/M hit)" line with N > 0, or a "Config keys present" line
   naming the YAML key the BC changes.  Only then → `action_needed`, and the
   `reason` must quote that line.  A "Config keys present" line that only names
   the component itself (`frontend`, `http`, `auth`) is not proof: it shows the
   component is configured, not that the configuration uses what changes.
4. **Otherwise → `no_action`.**  This covers:
   - the entity type the BC names shows 0 under "By type" / "No entities of
     type", however many other entities the integration has;
   - searches returned 0 hits;
   - the integration has entities, but no line shows they use the changed
     option, attribute, effect or naming.

## Grounding rules

- Entities existing for an integration is never proof on its own.  The
  integration being installed is why the BC is being assessed, not evidence
  that it applies.
- If the `action` you would write is conditional — "update **any** automations
  that …", "add X to entities **that** …" — you have not confirmed anything is
  affected.  The verdict is `no_action`.
- A BC about dashboard card options or YAML syntax is settled by the token
  search; it is not a browser question.
- A BC that tells users to change a value in automations or scripts is also
  settled by the token search.  "Searched … — 0 hits" means no automation,
  script or dashboard uses it → `no_action`, not a question for the user.
- Never write a reason that contradicts the verdict.  If the reason says the
  affected type is absent, the verdict is `no_action`.

## `when` guidance

- `before_update`: the action must happen before applying the update (config
  edit, schema migration, uninstall of a conflicting integration).
- `after_update`: the action is a follow-up after the update is applied (rename
  entities, update automations to use a renamed service, enable a capability that was not available before).

## Config-fix grounding

A `config_fix` is valid only when its `old` text appears verbatim in a tool
output this session.  Never invent config text that was not observed.
