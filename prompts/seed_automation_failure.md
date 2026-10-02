# Automation or Script Failure Investigation

Trigger: automation didn't fire, automation not triggering, script failed, automation stopped,
automation ran but action didn't execute, condition blocked automation, trigger not matching

## Approach

1. Call get_automation_traces(entity_id="automation.<name>") — list the last 10 runs.
   - Look for runs with state="stopped" and script_execution="failed", "aborted", or "error".
   - If no runs exist, the automation has never fired — check the trigger configuration.
2. If runs exist, pick the most recent failed run_id and call
   get_automation_traces(entity_id="automation.<name>", run_id="<id>") for the step trace.
   - trigger/* — did the trigger match? If result shows no match, the trigger condition is wrong.
   - condition/* — did any condition evaluate to false? That blocked execution.
   - action/* — which action step failed, and with what error?
3. Call read_config() to read the automation YAML and compare it with the trace results.
4. For scripts, use entity_id="script.<name>" — the same get_automation_traces tool works.
5. If the trace shows a template error, call render_ha_template to test the template live.
6. Call finish_chat (or finish_repair) with the specific failing step and the root cause.

## Common patterns

- **Condition blocked**: trace shows condition/0 result=false. Read config to see which
  condition and why it evaluated false (entity state, time window, template).
- **Trigger never matched**: zero runs or state=manual for all runs. Check trigger platform,
  entity_id in trigger, and whether the entity state actually changes.
- **Action error**: action/N shows an error. Check the service call, entity_id spelling, or
  whether the entity exists with check_entity_status.
- **Template error in condition/action**: render_ha_template can reproduce the exact error live.
