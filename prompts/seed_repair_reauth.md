---
signature: repair:*:config_entry_reauth
---
# config_entry_reauth — Integration needs re-authentication

**What it means:** An integration's OAuth token has expired, its API key was revoked,
or the account password changed.

**Urgency:** Low to Medium — the integration stops working but HA continues running.

## Investigation

- No need to read config files.
- Check domain logs: `read_logs(lines=20, log_source="ha_core")` — look for auth errors
  mentioning the domain.

## Action

- `requires_hitl=true, action="dismiss"`
- Dismissing the repair issue prompts the re-authentication flow on next HA UI page load.

**Explanation template:**
"The {domain} integration's credentials have expired (OAuth token or API key is no longer
valid). Dismissing this repair issue will prompt you to re-authenticate when you next open
the Home Assistant UI."
