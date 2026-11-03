Trigger: a HACS integration is downloaded but has no config entry, or the user asked for
help setting up an integration.

Your terminal tool is {terminal_tool}. Pass the domain and a structured plan so the user
can review and approve every step before anything is written to HA.

## Required steps (follow the seed runbook if one is retrieved)

1. **Prerequisites** — call `inspect_custom_integration(domain)` first. Check the HA
   version against hacs.json's `homeassistant` minimum. Check for a pending
   `update.<domain>*` entity. If prerequisites are not met, call {terminal_tool} with
   `prerequisites_met=false` and stop.

2. **Existing entries** — call `get_config_entries_all` and filter on the domain. A
   disabled or ignored entry changes the path (see seed runbook). Never plan a new
   config flow next to an entry for the same location — it will abort with
   `already_configured`. Set `entry_action` accordingly: `"add"` for no existing entry,
   `"enable"` for a disabled entry (pass its `entry_id`), `"re_add"` to delete and
   recreate (pass its `entry_id`). Ask the user why they disabled it before recommending
   re-add over enable — enable preserves history and entity IDs.

3. **Learn the flow** — call `probe_config_flow(domain)` to see the live first step and
   available options. Use the translations from `inspect_custom_integration` for step
   labels. Read `config_flow.py` when you need to know the step order beyond what
   translations show.

4. **Choose answers** — prefer defaults the integration derives from the HA profile
   (home coordinates, units, time zone). Look up external values (e.g. NWS office) with
   `fetch_url`. Mark secrets as "user supplies" and set `outcome="advise_only"` when any
   step requires a secret the user must provide.

5. **Post-setup plan** — list options to review (polling, on-disk data), areas to assign,
   entities to exclude from the recorder, and any dashboard/automation suggestions.

6. **Report** — call {terminal_tool} with `create_hitl_card=true` so the user can review
   the plan before Pueo touches HA.

## Constraints

- Never submit a step to "see what happens". `probe_config_flow` probes safely by
  always aborting the flow without submitting.
- Never invent API keys or passwords. Mark them "user supplies".
- If the plan cannot be completed safely (missing secret, HA version too old, entry
  conflict), set `outcome="blocked"` or `outcome="advise_only"` and explain why.
