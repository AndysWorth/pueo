Trigger: assess whether a single HA Core breaking change affects this installation.

Your terminal tool is {terminal_tool}. Call it once after reviewing the evidence.

Steps:
1. Read the BC name, body, and Evidence block provided in the initial context.
2. Judge from the Evidence block: **yes**, **no**, or **unknown**.
   - `yes`: entity of the right type confirmed, config key present, or automation/dashboard hit confirmed.
   - `no`: entity count is zero, config key is absent, and no automation/dashboard hits.
   - `unknown`: evidence is insufficient (no token matches, general BC, cloud/device-only change).
3. If the Evidence block says a specific key was **not checked** and you need to verify it,
   call `check_config_against_breaking_change` or `check_automation_references` once.
   Do not call these speculatively — the Evidence block already covers common patterns.
4. If `affects=yes` and a concrete YAML fix is needed, include `config_fix` with the
   exact `old` text from a tool output this session (grounding check).
5. Call `{terminal_tool}` with your verdict.

## Verdict rules (seeds own these — do not add verdict rules to task prompts)

## Forbidden

- Do not call `query_knowledge`, `read_logs`, `run_ha_command`, or `fetch_url`.
- Do not call `get_ha_profile` to list all config entries — use it only for a targeted lookup.
- Do not judge `yes` from entity count alone; entity *type* must match the BC.
- Device-dependent or browser-only BCs are always `unknown`.
