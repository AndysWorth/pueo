---
signature: repair:spook:*
---
# Spook repair digest — method

Use this runbook when one or more Spook repair issues share the same `translation_key`.
Spook's first load can create issues at once; Pueo groups them by translation key and
sends a single card per group.

Known cleanup keys (`empty_areas`, `unused_labels`, `unused_blueprints`,
`orphaned_statistics`) receive a deterministic card built directly from
`translation_placeholders` — no LLM investigation needed; this runbook applies only
when the LLM path is triggered, which is only for `unknown_engines` and any other
unrecognised keys.

## What Spook repair keys mean

| `translation_key` | Meaning | Typical action |
|---|---|---|
| `empty_areas` | An area has no entities assigned to it | Delete the area or move entities into it |
| `unused_labels` | A label is not assigned to any entity | Delete the label |
| `unused_blueprints` | A blueprint exists but no automation uses it | Delete the blueprint |
| `orphaned_statistics` | Long-term statistics exist for a sensor that no longer exists | Let Spook clean up via its repair flow |
| `unknown_engines` | A voice pipeline references a speech/wakeword engine that is not installed (STT, TTS, wake word) | Check whether the app is installed; if removed on purpose, delete or reassign the pipeline |

## Investigation (for `unknown_engines` and other unrecognised keys)

1. `query_knowledge` — look up the translation_key to retrieve any prior runbook.
2. The initial context already contains `translation_placeholders` from the HA repair
   issue data (injected by the poll). Read them directly.
3. For `unknown_engines`:
   a. `run_ha_command("ha apps list")` — check whether the referenced STT, TTS or
      wake-word app is listed and running.
   b. `get_ha_components` — verify whether the matching HA integration is loaded.
   c. If neither the app nor the integration exists, the pipeline references a removed
      engine. Advise deleting or reassigning the voice pipeline.
4. For any other unknown key: `get_spook_issues` to retrieve full issue details, then
   read the placeholders and Spook's issue text to determine what cleanup is needed.
5. State the root cause explicitly before calling the terminal tool.

## Verdict rules (seeds own these; do not add to agent_core.md)

- `unknown_engines`: if the referenced engines are absent (no matching app, no loaded
  integration), set `requires_hitl=true`, `action="dismiss"`, and advise the user to
  delete or reassign the voice pipeline that references those engines.
- `unknown_engines`: if the app exists and is running, the issue is a transient Spook
  startup false positive — `requires_hitl=false` and dismiss the repair issue.
- For any cleanup key where every referenced item is fixable via Spook's repair flow:
  set `requires_hitl=true` and explain each item clearly.
- If the issue count is high (> 5) and all items are the same type, describe the pattern
  and recommend bulk cleanup rather than listing each item individually.
