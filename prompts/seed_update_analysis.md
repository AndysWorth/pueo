---
signature: update:*:*
---

# Runbook: HA Update Breaking-Change Analysis — method

Use this runbook when no component-specific runbook matches.

## Standard investigation steps

1. `get_update_release_notes` — fetch notes for the target version. Short body (<500 chars)
   → stub; note it and set `safe_to_update=true` with a manual-review recommendation.
2. Look for sections: "Breaking changes", "Deprecations", "Migration notes".
3. For each breaking change: `check_config_against_breaking_change(config_key, description)`.
4. For CLI changes: `get_pueo_command_catalog` and cross-reference.
5. When release notes are unavailable: `safe_to_update=true`, all lists empty,
   `create_hitl_card=true`, recommend manual review.
