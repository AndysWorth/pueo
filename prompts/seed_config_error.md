# HA Configuration Error Investigation

Trigger: HA config invalid, yaml error, ha core check failing, configuration.yaml problem

## Approach

1. Call read_config to get the current configuration.yaml content.
2. Call fetch_ha_docs("<domain>", "config_flow.py") for the relevant integration to check
   expected schema and required fields.
3. Call run_ha_command("ha core check") if not already done to get the exact validation error.
4. Identify the specific key or section causing the error and propose a targeted fix.
5. Call finish_chat with the root cause, the specific offending config section, and the
   recommended correction.
