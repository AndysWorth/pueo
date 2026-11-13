Trigger: assess whether a single HA Core breaking change affects this installation.

Your terminal tool is {terminal_tool}. Call it once after reviewing the evidence.

Steps:
1. Read the BC name, body, and Evidence block provided in the initial context.
2. Apply the verdict rules of the pre-injected runbook to the Evidence block.
3. Call `{terminal_tool}` with `verdict` and a one-sentence `reason`, plus the
   fields that verdict requires (`action` and `when`, or `user_question`).
4. The Evidence block is complete. Do not reason about what might still need checking — judge only from what is stated.

## Forbidden

- Do not call any tools before `{terminal_tool}`.
- Do not call `{terminal_tool}` without `verdict`.
- Do not call `{terminal_tool}` with `verdict="user_check"` and an empty `user_question`.
- `not_assessed` is set by code, never by you.
