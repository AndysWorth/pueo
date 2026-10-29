---
signature: repair:*:*
---

# HA Repair Issue Investigation — method

Use this runbook when no symptom-specific runbook matches.

## Investigation guidance

- Phase 1: `query_knowledge` with the translation_key to find a symptom-specific runbook.
- If a symptom runbook is retrieved, follow it. Do not override it with your own rule.
- If no runbook matches: check domain logs, look up the translation_key, and state the
  root cause before deciding on action.
- `breaks_in_ha_version` set → time-sensitive; flag clearly.
- For a novel translation_key: call `request_escalation` if you have no clear diagnosis.
