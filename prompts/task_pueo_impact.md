Trigger: assess whether a release-note mention of a Pueo dependency confirms a break.

Your terminal tool is {terminal_tool}. Call it once after reviewing the snippets.

Steps:
1. Read the manifest entries and snippets provided in the initial context.
2. Apply the verdict rules of the pre-injected runbook to the snippets.
3. Call `{terminal_tool}` with `verdict` and a one-sentence `reason`.
   If `verdict` is `breaks_pueo`, set `quote` to the exact phrase from a snippet
   that confirms the break.

## Forbidden

- Do not call any tools before `{terminal_tool}`.
- Do not call `{terminal_tool}` without `verdict`.
- `breaks_pueo` without a verbatim `quote` will be rejected; use `dev_check` instead.
