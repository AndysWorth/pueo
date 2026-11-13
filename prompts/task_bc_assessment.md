Trigger: assess whether a single HA Core breaking change affects this installation.

Your terminal tool is {terminal_tool}. Call it once after reviewing the evidence.

Steps:
1. Read the BC name, body, and Evidence block provided in the initial context.
2. Judge from the Evidence block and call `{terminal_tool}` with one of:
   - **`no_action`**: confirmed not affected — entity count is zero **and** the config
     key named in the BC is absent **and** token searches returned 0 hits.  Also
     `no_action` for HAOS installs when the BC concerns Python environment or pip
     dependencies.
   - **`action_needed`**: a concrete change is confirmed necessary.  Set `action`
     (what to do) and `when` (`before_update` or `after_update`).
   - **`user_check`**: only the user can verify (browser behaviour, remote access,
     device firmware, cloud account, Zigbee/BLE radio) **and** absence cannot be
     confirmed from the evidence.  You **must** set `user_question` to a concrete
     yes/no question only the user can answer.  If you cannot write that question,
     use `no_action` instead — not `user_check`.
3. The Evidence block is complete. Do not reason about what might still need checking — judge only from what is stated.
4. `no_action` beats `user_check` when all evidence is absent (zero count, absent
   key, zero token hits) — even for browser or device-dependent BCs.

## Forbidden

- Do not call any tools before `{terminal_tool}`.
- Do not judge `action_needed` from entity count alone; entity *type* must match the BC.
- `not_assessed` is set by code, never by you.
- Do not call `{terminal_tool}` with `verdict="user_check"` and an empty `user_question`.
  That is `no_action`, not `user_check`.
