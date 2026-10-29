---
signature: notification:*
---

# HA Notification Investigation — method

Use this runbook when no symptom-specific runbook matches.

## Investigation priorities
1. For security notifications (http_login, ip-ban): always call `get_device_info` first.
2. For config errors: logs contain the specific YAML section and error.
3. For integration failures: logs show the Python exception; `query_knowledge` for the
   integration.
4. Dismiss benign notifications (known logins, informational alerts) to keep panel clean.
5. For custom/unknown notification types: summarize in plain English, assess risk.
