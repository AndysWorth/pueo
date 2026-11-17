---
signature: update:core_pueo:*
---

# Pueo-dependency impact — verdict rules

These rules are authoritative for every `update:core_pueo:*` session.

## Context

Pueo communicates with Home Assistant through a set of HA surfaces: WebSocket
commands, REST API paths, SSH CLI commands, integrations, and repair IDs.  The
manifest (shown in the initial context) lists the ones Pueo actively uses.

Each session evaluates one dependency term — the shared search term that matched
one or more manifest entries.  The session receives:

- **Manifest entries**: the specific commands, paths, or domain names Pueo calls
  for this term.
- **Snippets**: short passages from the HA release notes (blog, changelog, patch
  notes) that mention that term.

## Verdicts

Call `finish_pueo_impact` with one of:

- **`breaks_pueo`**: a snippet explicitly states that a surface Pueo uses is
  removed, renamed, or its interface changed in an incompatible way, **and** the
  manifest does not already list a replacement surface.  Set `quote` to the exact
  phrase from the snippet that shows the break.
- **`no_impact`**: the snippets describe a change that does not affect any of the
  listed manifest entries, or the manifest already includes the replacement surface
  (e.g. both old and new API endpoint are listed).
- **`dev_check`**: the snippets suggest a possible impact but the available text is
  not conclusive enough to confirm a break.

## Decide in this order

1. **Is the manifest's replacement already there?**  If a snippet says "X is replaced
   by Y" and the manifest already lists Y, the verdict is `no_impact` immediately.
2. **Does a snippet explicitly say a listed surface is removed or renamed?**
   - "no longer supported", "removed", "deprecated and removed", "replaced by",
     "renamed to", "dropped" applied to a command, path, or API Pueo uses
     → `breaks_pueo` with the confirming phrase as `quote`.
3. **Is the change about *adding* functionality, improving behaviour, or renaming
   something not listed in the manifest?**  → `no_impact`.
4. **Otherwise** (ambiguous wording, partial information, possible future removal)
   → `dev_check`.

## Grounding rules

- `breaks_pueo` requires a verbatim `quote` from a snippet.  If you cannot point
  to an exact phrase, the verdict is `dev_check`.
- A snippet mentioning the term in passing (e.g., "the HACS store now uses the
  Marketplace") is not proof of a break — only an explicit statement that a Pueo
  surface is gone or incompatible.
- Generic mentions of a domain name ("the light domain got new colour modes") do
  not constitute a break of a WebSocket or REST path unless the path is explicitly
  named.
