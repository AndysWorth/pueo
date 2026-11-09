---
signature: update:core:*
---

# HA Core update analysis

**Context:** Core updates are the highest-risk category. Always create a HITL card so the user
reviews and approves. Patch releases (e.g. 2026.9.0 → 2026.9.1) rarely break anything; minor
releases (2026.9.x → 2026.10.x) may carry breaking changes.

## Verdict rules

- **Patch release, no breaking changes that affect this install**:
  `safe_to_update=true`, `instance_impact="none"`, `create_hitl_card=true`, `risk_score="low"`.
- **Minor/major release with breaking changes that affect this install**:
  `safe_to_update=false`, `instance_impact="high"`, `create_hitl_card=true`, `risk_score="high"`.
- **Minor/major release, no breaking changes that affect this install**:
  `safe_to_update=true`, `instance_impact="none"`, `create_hitl_card=true`, `risk_score="low"`.
- **Release notes unavailable**: `safe_to_update=true`, `create_hitl_card=true`, recommend manual
  review. Do not claim there are no breaking changes when notes are unreadable.

`create_hitl_card` is always `true` for Core updates regardless of risk level.

## Investigation steps

1. `get_update_release_notes(entity_id=<entity>)` — returns a **pre-built digest** for this
   release.  The digest contains:
   - `Backward-incompatible changes` with installed-integration BCs at the top
   - `Flagged changes (from screening)` — changes flagged by the pre-screening pass
   - `Feature highlights (from screening)` — noteworthy new capabilities
   - `Farewell` / removed section
   Do NOT call `fetch_url` for the blog or changelog — the digest already contains the
   relevant content.  When no digest is available (non-Core update or report not yet built),
   `get_update_release_notes` returns raw notes as before.

2. For every installed-integration BC in the digest that mentions a YAML config key:
   `check_config_against_breaking_change(config_key, description)` — searches
   `/config/configuration.yaml`, `/config/packages/`, and `/config/integrations/`.
   - Classify each matched line as `likely affected`, `likely safe`, or `unclear`.
   - Count as a finding only if at least one line is `likely affected` or `unclear`.

3. For breaking changes that affect service calls or entity platforms:
   `check_automation_references(pattern, description)` — searches automation YAML files.
   Apply the same line classification rule as step 2.

4. `query_knowledge(query_type="procedural")` — cross-check renamed/removed config keys
   against the `ha_best_practices` deprecated-API table.

5. For any minor or major release: **always** call `get_pueo_command_catalog`.
   Compare every removed, renamed, or replaced integration, API command, or service
   mentioned in the digest against the catalog entries.  For each match, add an entry to
   `pueo_command_risks`.
   A catalog entry whose note says it falls back for the target version (e.g. `hacs/*` →
   `marketplace/*` on 2026.11+) is **not** a risk — do not list it.

6. For every installed-integration BC entry in the digest, state explicitly in
   `breaking_changes` whether it affects this instance (yes/no/unknown).

7. Pass `highlights`: 2–5 one-sentence items from the digest's `Feature highlights`
   section that matter to this install (`"<domain>: <feature>"`).

## Output guidance

- `affected_config_keys`: only keys confirmed present in this install's config.
- `proposed_config_fixes`: concrete YAML diff the user must apply before updating.
- `prerequisites`: any manual steps before applying (e.g. "Update Supervisor first",
  "Create a manual backup").
- `confidence`: 0.9+ only when the digest was `complete` and you checked every installed-
  integration BC.  Lower confidence if the digest was incomplete or notes unavailable.
