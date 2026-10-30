# ADR 052 — Family-scoped runbook entries

**Status:** Accepted — amends ADR 051  
**Date:** 2026-10-30

## Context

ADR 051 (atomic runbook entries) stores learned entries under the concrete session
signature, e.g. `update:addon:matter_server`.  This keying ensures entries are
precise but means they never cross subjects: the lesson "add-on slugs in initial
context may be wrong" learned from a `matter_server` session is invisible when
the next session triggers on `mosquitto_broker`.  Over 36 sessions with 21
distinct concrete signatures, the maximum was 4 entries per signature, and no
runbook had ever reached the `validated` threshold (3 verified successes).

**F8 evidence (2026-10-30 review):** the slug lesson and the reboot-slot rule are
the only useful entries in the DB.  Both are subject-free (they apply across all
add-on and OS updates respectively) yet both are stored under a concrete signature
and are invisible to sibling sessions.

## Decision

**Scope rule.**  In `_write_entries`, compute the session subject's tokens
(last segment of the signature + `subject_key`).  If an item's text contains
none of those tokens, write it under the family pattern — the signature with its
last segment replaced by `*` (e.g. `update:addon:*`).  Procedure entries follow
the same rule.  Concrete entries whose text *does* contain a subject token remain
under the concrete signature.

**Sibling promotion.**  After writing a new concrete entry, scan live sibling
entries (same prefix, different last segment).  Any pair that `_same_entry` deems
equivalent is merged into a family-pattern entry: success counts summed,
`episode_refs` unioned, both subject rows retired with `promoted_to` pointing to
the merged entry.

**Retrieval.**  `_fetch_signature_chunks` builds the learned chunk by calling
`_load_entries_for_session` (exact + family-pattern), rendering via
`_render_approach`, and truncating to 1 200 tokens.  The rendered
`agent_strategies` row (Chroma + `/runbooks`) is still kept via
`render_learned_runbook`; calling it with the family pattern signature writes a
family-pattern row there too.

**Family-wide validation.**  `_auto_validate_if_eligible` and
`_auto_validate_if_eligible_standalone` detect when the strategy's signature
ends with `:*` and count verified successes/failures across all sessions whose
`runbook_usage.signature` matches the prefix, rather than limiting to the
specific `strategy_id`.

**Schema V44.**  `runbook_entries` gains a `promoted_to TEXT` column (idempotent
`ALTER TABLE`, no data migration).

**`/runbooks` learning-health table.**  `_entry_counts` in `kb_meta_analyzer`
adds the family-pattern row's counts to each concrete sibling's count, so the
table reflects how much the signature actually benefits from.

## Consequences

- Learned lessons that apply across a domain (OS updates, add-on updates, device
  availability) accumulate faster and reach the validated threshold across sibling
  sessions rather than each sibling starting from zero.
- Subject-specific entries (e.g. a device-specific quirk that names the device)
  continue to live under the concrete signature and are never diluted into the
  family.
- `render_learned_runbook` is now called for both the concrete signature and the
  family pattern after any write.
- The `distill_procedure` function accepts a new `subject_key` parameter so
  procedure notes follow the same scope rule.
- Downstream code that calls `_write_entries` must handle the new return type
  `(list[tuple[str, str]], set[str])` instead of `list[tuple[str, str]]`.
