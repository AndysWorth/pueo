---
signature: repair:*:integration_disabled
---
# integration_disabled / setup_error — Integration failed to load

**What it means:** The integration raised an exception during HA setup. Its entities
are unavailable until the issue is resolved.

**Urgency:** Medium — that integration's entities are unavailable.

## Investigation

- `read_logs(lines=50, log_source="ha_core")` — find the exception. Look for
  "Error setting up entry" or "Setup of domain failed" messages mentioning the domain.
- `query_knowledge("{domain} integration setup error")` — check for known issues or fixes.
- `get_integration_diagnostics("{domain}")` — may surface the root cause.

## Action

- `requires_hitl=true, action="dismiss"`
- Dismissing the repair issue lets the user investigate or reinstall in the UI.

**Explanation template:**
"The {domain} integration failed to load: {error from logs}. Dismissing this issue will
allow you to remove and re-add the integration, or investigate via the HA UI. Check the
HA logs for the full error trace."
