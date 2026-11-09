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
   - `Backward-incompatible changes` with installed-integration BCs at the top, each with
     an `Evidence:` block showing entity counts, token matches, and automation/dashboard hits.
   - `Flagged changes (from screening)` — changes flagged by the pre-screening pass
   - `Feature highlights (from screening)` — noteworthy new capabilities
   - `Farewell` / removed section
   Do NOT call `fetch_url`, `read_source`, or `get_ha_profile config_entries` — the digest
   already contains the relevant content.

2. For every BC in the digest (bc_installed and bc_general), read its `Evidence:` block
   and judge: `yes` (this install is affected), `no` (confirmed not affected), or `unknown`.
   - `yes`: evidence shows matching entities, automation/dashboard hits, or config tokens.
   - `no`: evidence shows zero entities, no automation hits, and no dashboard hits for the
     domains or tokens in question.
   - `unknown`: evidence is insufficient (no token matches recorded, general BC with no
     entity scope, or BC concerns a device/cloud service that can't be verified offline).
   Call `check_config_against_breaking_change` or `check_automation_references` **only**
   when the Evidence block says `tokens_checked` missed something specific that you need
   to verify (e.g. a YAML key the evidence didn't search for). Do not call these tools
   speculatively or to confirm what the evidence already shows.

3. `query_knowledge(query_type="procedural")` — cross-check renamed/removed config keys
   against the `ha_best_practices` deprecated-API table.

4. For any minor or major release: **always** call `get_pueo_command_catalog`.
   Compare every removed, renamed, or replaced integration, API command, or service
   mentioned in the digest against the catalog entries.  For each match, add an entry to
   `pueo_command_risks`.
   A catalog entry whose note says it falls back for the target version (e.g. `hacs/*` →
   `marketplace/*` on 2026.11+) is **not** a risk — do not list it.

5. Pass `highlights`: 2–5 one-sentence items from the digest's `Feature highlights`
   section that matter to this install (`"<domain>: <feature>"`).

## Output guidance

- `bc_verdicts`: **required**. One entry per BC (bc_installed + bc_general). Every BC must
  have an entry — missing ones are filled as `unknown` automatically.
- `affected_config_keys`: only keys confirmed present in this install's config.
- `proposed_config_fixes`: concrete YAML diff the user must apply before updating.
- `prerequisites`: any manual steps before applying (e.g. "Update Supervisor first",
  "Create a manual backup").
- `confidence`: 0.9+ only when the digest was `complete` and you assessed every BC.
  Lower confidence if the digest was incomplete or notes unavailable.

## Forbidden tools in this session

- `read_source` — the digest already has what you need.
- `get_ha_profile config_entries` — not needed for BC assessment.
- Single-word or domain-only grep patterns (`mqtt`, `switch`, `light`) — too broad to be
  meaningful; use specific key or service patterns from the BC body.
