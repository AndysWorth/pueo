# Integration or Entity Error Investigation

Trigger: integration failing, sensor unavailable, entity error, connection error, API outage

## Approach

1. Call query_knowledge("<integration_name> error") — check for known breaking changes or deprecations.
2. Call read_logs(200) — the actual exception and traceback are in the HA supervisor journal.
3. Call fetch_ha_docs("<domain>", "sensor.py") then fetch_ha_docs("<domain>", "const.py") to check
   current API expectations vs. what the config provides.
4. Distinguish root cause before answering:
   - ConnectionError / NameResolutionError / MaxRetryError / 504 = external API outage.
     Set is_actionable=false. The integration will self-recover. No config change needed.
   - COOPSAPIError / InvalidConfig / SchemaError = likely requires a config change.
5. To confirm an external outage has resolved, call fetch_url("<api_endpoint>").
6. Call finish_chat with a root-cause statement and recommended action (or "no action needed").
