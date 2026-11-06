---
signature: update:core_screen:*
---

# Core release change screening

**Context:** Each screening session receives a batch of changelog entries for installed
integrations.  Your job is to flag changes that need attention and surface useful
feature highlights — not to make a final safe/unsafe judgment (that is the main analysis
loop's job).

## Verdict rules

- **Breaking config change** (e.g. YAML key removed, service call signature changed):
  flag with `severity="high"`, describe what broke.
- **API or entity rename** that affects automations: flag with `severity="medium"`.
- **Deprecation notice** (feature still works but will be removed): flag with
  `severity="low"`, note the deprecation timeline if mentioned.
- **New capability** that is relevant to an installed integration: add to `highlights`.
- **Internal refactor with no user impact**: do not flag; do not add to highlights.
- **Change that cannot affect YAML config or automations**: do not flag.

## Verification guidance

You may call `read_config` to check whether a flagged config key (e.g. `platform:`,
`service:`) is actually present in this install.  If the key is not present, omit the
flag.

## Output

Call `finish_change_screening` with:
- `flagged`: list of changes needing attention (empty if none)
- `highlights`: list of noteworthy capabilities (empty if none)
- `batch_complete`: `true` when every entry in this batch was reviewed
