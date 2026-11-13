Trigger: assess whether a single HA Core breaking change affects this installation.

Your terminal tool is {terminal_tool}. Call it once after reviewing the evidence.

Steps:
1. Read the BC name, body, and Evidence block provided in the initial context.
2. Judge from the Evidence block: **yes**, **no**, or **unknown**.
   - `yes`: entity of the right type confirmed, config key present, or automation/dashboard hit confirmed.
   - `no`: entity count is zero, config key is absent, tokens searched returned 0 hits, or the Evidence block explicitly states absence (e.g. "Multi-channel devices: none", "YAML key: absent").
   - `unknown`: evidence is insufficient (general BC with no installation-specific evidence, cloud/device-only change, or install type cannot rule it out).
3. The Evidence block is complete. Do not reason about what might still need checking — judge only from what is stated.
4. Call `{terminal_tool}` with your verdict.

## Verdict rules (seeds own these — do not add verdict rules to task prompts)

## Forbidden

- Do not call any tools before `{terminal_tool}`.
- Do not judge `yes` from entity count alone; entity *type* must match the BC.
- Device-dependent or browser-only BCs are always `unknown`.
