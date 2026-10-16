---
signature: repair:*:breaking_change
---
# breaking_change — Deprecated configuration key

**What it means:** HA updated and a config key or YAML platform the user's configuration
uses is now invalid. HA may reject `configuration.yaml` on the next restart.

**Urgency:** High — HA may fail to start after the next restart.

## Investigation

- `query_knowledge("{translation_key} {domain}")` — check for known migration steps.
- `read_config()` — look for the deprecated key in `configuration.yaml`.
- Note any affected keys and their replacements.

## Action

- `requires_hitl=true, action="dismiss"` with specific YAML fix instructions.
- Include the exact deprecated key, the replacement key, and the YAML change needed.

**Explanation template:**
"Home Assistant detected a deprecated configuration key: `{key}` in the `{domain}`
integration. This must be updated before your next HA restart to avoid a config error.
Change `{old_key}` to `{new_key}` in `configuration.yaml`."
