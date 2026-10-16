---
signature: update:hacs_integration:*
---
# HACS integration update analysis

**Context:** HACS integrations often have no formal release notes. The `release_url` field
is frequently null; use `get_update_release_notes` with the version string and check for
a GitHub release via `fetch_ha_docs` if notes are unavailable.

## Key patterns

**HACS update with no release notes**
- `safe_to_update=true`, `instance_impact="none"`, `create_hitl_card=true`
- Recommendation: "No release notes available for this HACS integration. Safe to approve,
  but review the integration's GitHub repository for any manual migration steps."

**HACS update with incompatibility note in HA Core release notes**
- Check whether the HACS integration is affected by any recent HA Core breaking changes.
- If affected: `instance_impact="high"`, note the specific conflict.

**Integration is downloaded but not loaded (unloaded)**
- The update is still safe to apply; restart after update has no runtime impact.
- Note this in the explanation: "This integration is downloaded but not set up in HA."

## After a HACS update is applied

A `restart_required` repair issue will appear. The restart_required runbook handles
that case — do not merge the restart decision into this analysis.
