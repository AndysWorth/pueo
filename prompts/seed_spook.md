---
signature: repair:spook:*
---
# Spook repair digest — method

Use this runbook when one or more Spook repair issues share the same `translation_key`.
Spook's first load can create dozens of issues at once; Pueo groups them by type and
sends a single card.

## What Spook repairs mean

| `translation_key` | Meaning | Typical fix |
|---|---|---|
| `dead_entity` | Entity referenced in an automation, script, dashboard, or template no longer exists | Remove or update the reference |
| `missing_entity` | Entity that Spook tracks is not present in the entity registry | Check if the integration providing it was removed or renamed |
| `domain_*` | A domain-level issue (deprecated service, renamed domain) | Update the automation/script to use the current domain or service |
| `invalid_*` | Invalid YAML, template syntax, or action in an automation | Open the automation editor and fix the flagged node |
| `deprecated_*` | Automation or service call uses a deprecated API | Migrate to the replacement as described in Spook's issue text |

## Investigation

1. `get_spook_issues` — fetch full issue details including affected entity ids, domains,
   automation or script titles, and dashboard paths.
2. Check whether the affected entities still exist: the issue data from `get_spook_issues`
   includes the entity id and any referencing automations or scripts — read it directly.
3. If the issue involves an automation or script, `read_config()` to see the relevant
   YAML block.
4. State the root cause explicitly before calling the terminal tool.

## Action

- Most Spook issues require a user-side YAML or UI edit. Set `requires_hitl=true` and
  `action="dismiss"`.
- Include the affected entity ids, the automation or script names, and the exact change
  the user must make.
- Group all issues with the same `translation_key` into one card — do not send N cards.
- If you cannot determine the fix from the issue data alone, say so in `human_explanation`
  and ask the user to check the Spook dashboard at `/config/repairs`.
