# ADR 039 — Federated runbook validation via pueo-kb

**Status:** Accepted  
**Date:** 2026-10-08

## Context

A local runbook is auto-promoted from `candidate` to `validated` after
`RUNBOOK_VALIDATE_MIN_SUCCESSES` distinct-episode successes with 0 failures.
Because a typical home rarely encounters the same symptom three times, most
candidates never reach the threshold through local evidence alone.

Pooling evidence across many Pueo instances via the shared pueo-kb repo
(`AndysWorth/pueo-kb`) allows the community to promote runbooks that no
single instance could validate alone.

## Decision

### Identity and keys

- **Instance id:** a random UUID4 generated once and persisted at
  `get_dirs().state_dir/"instance_id"` (see `utils/instance.py`). Anonymous;
  not a config key.
- **Portable runbook id:** `kb_id = "rb_" + sha256(signature)[:12]`.
  Signatures are deterministic across instances (`utils/knowledge/runbook_signature.py`).
  Runbooks with `chat:` signatures are not federatable (signatures are not stable
  across instances).

### Evidence files

Each contributing instance sends a single file,
`evidence/<instance_id>.json`, alongside contributed runbooks in the same PR:

```json
{"instance_id": "...", "pueo_version": "...", "updated_at": "...",
 "runbooks": {"rb_ab12...": {"sha256": "...", "signature": "...",
              "successes": 2, "failures": 0, "episodes": 2}}}
```

The file contains counts only.  Episode ids, host names, and approach text
never leave the instance.  The file is rebuilt from `runbook_usage` on every
contribution, so PRs are idempotent.

### Aggregation (pueo-kb CI)

The `build-manifest.yml` workflow runs `scripts/build_manifest.py` on every
push to `main`.  It regenerates `MANIFEST.json` from runbook frontmatter plus
all evidence files.  Each manifest entry gains:

- `state`: `seed`, `candidate`, `validated`, or `flagged`
- `signature`
- `community_stats`: `{successes, failures, instances}`

**Promotion rule** (evaluated per `kb_id` at its current sha256):

| Condition | State |
|-----------|-------|
| successes ≥ 3 AND distinct instances with a success ≥ 2 AND failures == 0 | `validated` |
| any failure | `flagged` (maintainer must review before re-validating) |
| contributed but below threshold | `candidate` |
| Seeds | always `seed`; never demoted |

Thresholds (`MIN_SUCCESSES=3`, `MIN_INSTANCES=2`) live in the pueo-kb script,
not in Pueo config.

### Ingester

`kb_ingester.py` reads `state` and `community_stats` from every manifest
entry:

- `flagged` entries are skipped entirely.
- Entries are re-ingested when their state changes; the sync-state key encodes
  `id:sha256:state` so a promotion triggers a fresh upsert.
- `kb_state` and `community_stats` integers are written into the ChromaDB
  chunk metadata so authority scoring and labels can differentiate them.

### Authority tiers

| Source | State | Authority | Label |
|--------|-------|-----------|-------|
| `pueo_kb` | `seed` or `validated` | 0.70 | `[COMMUNITY RUNBOOK]` |
| `pueo_kb` | `candidate` | 0.45 | `[COMMUNITY CANDIDATE]` |

Local auto-validation (`_auto_validate_if_eligible`) is unchanged.
`kb_*` ids are promoted only by the pueo-kb CI, not by local evidence.

### PR checks (pueo-kb)

A `validate_pr.yml` workflow rejects PRs that:

- touch more than one `evidence/*.json` file
- edit `MANIFEST.json` directly (CI owns it)
- contain malformed runbook frontmatter or evidence schema

## Trust model

The maintainer's merge is the gate.  Instance ids are anonymous UUIDs; there
is no authentication mechanism.  A single actor could therefore create multiple
fake instance ids to inflate the `instances` count (Sybil attack).

**Acknowledged mitigations:**

1. PRs are human-reviewed before merge; unusual `instances` counts are visible.
2. Seeds are never demoted — they can only be affected by a `flagged` state,
   which still requires a failure row.
3. The `MIN_INSTANCES` threshold requires at least 2 distinct instance ids.
4. The contributor anonymizes approach text but preserves structure, making
   wholesale fabrication harder to hide.

**Not mitigated:** a single actor with two instance ids and a patched binary
can still artificially validate a runbook.  This is acceptable for the current
trust model; if the community grows, a signed-identity scheme can be layered on
later.

## Consequences

- Candidates from pueo-kb appear in agent context at authority 0.45 labelled
  `[COMMUNITY CANDIDATE]`.  This is lower than a local validated runbook (0.75)
  but above a knowledge gap (0.30), so the agent sees them without over-trusting.
- Promotion to `validated` re-ingests the runbook at 0.70, providing a meaningful
  quality signal once the community has cross-instance evidence.
- The trust boundary is explicit: local evidence and community evidence are both
  visible, separately weighted, and labelled for the LLM to reason about.

## Related decisions

- [ADR 038 — Runbook lifecycle](038-runbook-lifecycle.md): local candidate→validated
  flow, `runbook_usage` table, distillation.
- [ADR 030 — Authority-ranked knowledge](030-authority-ranked-knowledge.md): the
  pueo-kb tiers supersede the single `pueo_kb = 0.70` row from ADR 030.
