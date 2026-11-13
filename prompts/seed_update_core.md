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

1. If the initial context already contains a `## Pre-computed BC assessment results`
   section, **skip this step** — the full digest is already in the context.
   Otherwise call `get_update_release_notes(entity_id=<entity>)` to get the pre-built
   digest, which contains:
   - `Backward-incompatible changes` — a verdict table (name | verdict | reason | fix)
     with per-BC assessments already filled in.
   - `Patch changes` — real fix lines from the patch body (installed domains only).
   - `Feature highlights (from screening)` — noteworthy new capabilities.
   - `Flagged changes (informational only)` — do not raise `instance_impact` from flags.
   Do NOT call `fetch_url`, `read_source`, `get_ha_profile`, or
   `check_config_against_breaking_change` — the per-BC assessments have already been run.

2. For any minor or major release: **always** call `get_pueo_command_catalog`.
   Compare every removed, renamed, or replaced integration, API command, or service
   mentioned in the digest against the catalog entries.  For each match, add an entry to
   `pueo_command_risks`.
   A catalog entry whose note says it falls back for the target version (e.g. `hacs/*` →
   `marketplace/*` on 2026.11+) is **not** a risk — do not list it.

3. Call `finish_update_analysis` with:
   - `highlights`: 2–5 one-sentence items from `Feature highlights`.
   - `instance_impact`: derived from BC verdicts only (flags never raise impact).
     Server overrides this from the verdict table, so pass your best read from the digest.
   - When the digest was pre-injected, **do not supply `bc_verdicts`** — that field is
     not part of the tool and the server uses the pre-run assessments.
   - When the digest was fetched (assessment pass did not run), supply `bc_verdicts`
     taken directly from the verdict table.  Values: `action_needed` / `no_action` /
     `user_check` / `not_assessed`.  Old values `yes` / `no` / `unknown` also accepted.

## Output guidance

- `bc_verdicts`: one entry per BC — read them from the verdict table in the digest.
  Flags are **not** BCs; do not list them here.
- `affected_config_keys`: only keys confirmed present in this install's config.
- `proposed_config_fixes`: only fixes grounded in tool output from this session.
  The per-BC assessment pass already contributed its grounded fixes; omit this field
  unless you found an additional confirmed fix in this session.
- `prerequisites`: any manual steps before applying (e.g. "Update Supervisor first").
- `confidence`: 0.9+ only when the digest was `complete` and all BCs were assessed.
  Lower confidence if the digest was incomplete or notes unavailable.

## Forbidden tools in this session

- `read_source` — the digest already has what you need.
- `get_ha_profile config_entries` — not needed for BC assessment.
- Single-word or domain-only grep patterns (`mqtt`, `switch`, `light`) — too broad to be
  meaningful; use specific key or service patterns from the BC body.
