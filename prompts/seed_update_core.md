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

1. `get_update_release_notes(entity_id=<entity>)` — fetch notes for the target version.
   - Read the full text. Identify the "Breaking changes" section (usually near the top).
   - Produce a numbered checklist of ALL breaking changes mentioned, including ones that may
     not affect this install. Mark each as "affects this install: yes/no/unknown" — verify below.

2. For each potentially-affecting breaking change that mentions a YAML config key:
   `check_config_against_breaking_change(config_key, description)` — searches
   `/config/configuration.yaml`, `/config/packages/`, and `/config/integrations/`.
   - When matches are returned, classify each matched line as `likely affected`,
     `likely safe`, or `unclear`. Count the check as a finding only if at least one
     line is `likely affected` or `unclear`. If all lines are `likely safe`, state it
     as a non-finding.

3. For breaking changes that affect service calls or entity platforms (common in minor
   releases): `check_automation_references(pattern, description)` — searches
   `automations.yaml`, `automations/*.yaml`, and `packages/**/*.yaml`.
   Apply the same line classification rule as step 2.

4. `query_knowledge(query_type="procedural")` — cross-check renamed/removed config keys
   against the `ha_best_practices` deprecated-API table.

5. For any minor or major release (e.g. 2026.10.x → 2026.11.0): **always** call
   `get_pueo_command_catalog`. Compare every removed, renamed, or replaced integration,
   API command, or service mentioned in the release notes against the catalog entries.
   For each match, add one entry to `pueo_command_risks` naming the affected command/path
   and the Pueo feature that uses it (e.g. `"hacs/* WS commands → HACS discovery and
   companion provisioning"`). If no catalog entries match the notes, `pueo_command_risks`
   stays empty.
   For patch releases (e.g. 2026.11.0 → 2026.11.1), call only when the notes explicitly
   mention CLI or API changes.

6. `read_file("/config/configuration.yaml")` only if you need to verify a specific config key
   flagged by a breaking change.

## Output guidance

- `affected_config_keys`: only keys confirmed present in this install's config.
- `proposed_config_fixes`: concrete YAML diff the user must apply before updating.
- `prerequisites`: any manual steps before applying (e.g. "Update Supervisor first",
  "Create a manual backup").
- `confidence`: 0.9+ only when you have read the full release notes and checked every
  relevant config key. Lower confidence if notes were short or unavailable.
