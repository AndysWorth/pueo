---
signature: update:supervisor:*
---

# Home Assistant Supervisor update analysis

**Context:** Supervisor updates are medium-risk. They apply without a Core restart, but a
Supervisor restart is performed automatically. Supervisor updates rarely carry breaking changes
for end users; most are internal plumbing fixes or add-on API improvements.

## Verdict rules

- **Routine patch/minor bump, no breaking add-on API changes**:
  `safe_to_update=true`, `instance_impact="none"`, `create_hitl_card=true`, `risk_score="low"`.
- **Breaking change to the add-on API or storage layout**:
  `safe_to_update=false`, `create_hitl_card=true`, `risk_score="medium"`.
- **Release notes unavailable**: `safe_to_update=true`, `create_hitl_card=true`,
  recommend manual review.

`create_hitl_card` is always `true` for Supervisor updates.

## Investigation steps

1. `get_update_release_notes(entity_id=<entity>)` — fetch notes for the target Supervisor
   version. Supervisor release notes are on GitHub (home-assistant/supervisor). If the WS
   body is a stub (< 200 chars or only `<ha-alert>` content), fetch from the release URL.

2. Check the "Breaking changes" section. Supervisor breaking changes typically affect:
   - Add-on API endpoints or configuration schemas
   - Volume mount paths (`/app_configs` vs `/addon_configs`)
   - Auth API changes

3. If the release notes mention add-on API changes:
   `run_ha_command("ha apps list --raw-json")` to see which add-ons are installed and
   may be affected.

## Output guidance

- `risk_score`: "low" for routine bumps; "medium" for add-on API changes.
- `prerequisites`: note the automatic Supervisor restart; add manual steps only when
  an add-on configuration migration is needed.
- `confidence`: 0.9+ only when the full release notes were read.
