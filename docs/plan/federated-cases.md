# Federated Case Library

Part of the [Roadmap](../roadmap.md) · Milestone 9.

---

### Problem

Each Pueo instance only learns from the failures in its own home. HA failures follow community-wide patterns: deprecated integrations, breaking changes in specific releases, hardware-specific issues on common platforms like the HA Yellow. Pooling anonymized repair episodes across instances makes every instance smarter without sharing private data.

---

### Architecture

**Public GitHub repo: `pueo-cases`** (separate from this repo)
- Each merged PR adds one or more anonymized episode YAML files
- Human review gates every contribution — no automated merging
- Episodes follow the anonymized export schema from Milestone 8

**Pueo's two roles:**

| Role | Description |
|------|-------------|
| Contribute | Submit local episodes via a PR to `pueo-cases`; user reviews the redacted YAML before submitting |
| Consume | Weekly pull of merged cases → embed → upsert into `community_cases` ChromaDB collection (created in Phase 14 but empty until now) |

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 53 | Case submission: dashboard "Prepare for submission" flow — review anonymized YAML, edit redactions, `gh pr create` to `pueo-cases` |
| 54 | Case ingest: weekly pull of merged YAML from `pueo-cases` → embed with `nomic-embed-text` → upsert into `community_cases` ChromaDB collection |
| 55 | Eval scenario generation: each newly ingested case produces a `.yaml` scenario file in `evals/scenarios/community/`; `run_evals.py` picks them up automatically |

---

### Submission Flow (item 53)

1. User selects episode from dashboard episodes tab
2. Pueo renders the anonymized YAML (from `--mode export-episodes` logic) with editable redaction fields
3. User reviews and confirms redactions; adds optional human-readable `description` field
4. Pueo runs `gh pr create` against `pueo-cases` repo with the YAML as the PR body attachment
5. Dashboard marks episode as `submitted`; links to the PR URL

---

### Ingest Flow (item 54)

- Weekly `launchd` job: `python main.py --mode refresh-knowledge` (extends existing RAG refresh from Phase 14)
- Pull merged PRs from `pueo-cases` since last ingest timestamp
- Parse each YAML → embed → upsert into `community_cases` with metadata: `source_pr`, `ingest_date`, `trigger_type`
- Log ingest count; surface in `--mode backup-status` style summary

---

### Eval Scenario Generation (item 55)

For each ingested case, generate a scenario file:
```yaml
name: community_<pr_number>_<slug>
source: pueo-cases/pr/<pr_number>
trigger: ha_log          # from episode.trigger
input_log_line: ...      # first symptom from episode.symptoms
expected_is_valid: false
expected_severity: ...   # from episode fix metadata
issue_keywords: [...]    # extracted from hypothesis_chain
fix_must_parse: true
```

Scenarios land in `evals/scenarios/community/` and are ignored by git (the directory is tracked but individual files are generated). `run_evals.py` discovers them automatically via glob.

---

### Done when

- One real episode submitted, reviewed, merged to `pueo-cases`, pulled back locally, and retrievable via `query_knowledge`
- `evals/run_evals.py` picks up community scenario files automatically and includes them in the score table
- Ingest pipeline runs on weekly schedule alongside RAG refresh
- Dashboard submission flow tested end-to-end against a real `gh pr create` (can use a test fork of `pueo-cases`)
