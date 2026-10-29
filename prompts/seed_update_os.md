---
signature: update:os:*
---

# HAOS (Home Assistant Operating System) update analysis

**Context:** OS updates are high-risk: they trigger a full reboot. Always create a HITL card
so the user reviews and approves before applying. OS release notes live on GitHub, not in the
Supervisor WebSocket release-notes endpoint — a WS body that contains only a `<ha-alert>` stub
is not the real notes; fetch from the release URL instead.

## Verdict rules

- **Patch release, no board-specific issues**: `safe_to_update=true`, `instance_impact="none"`,
  `create_hitl_card=true`, `risk_score="low"`. A reboot is always required.
- **Major/minor release or breaking hardware changes**: `safe_to_update=false` (user should
  review), `create_hitl_card=true`, `risk_score="high"`.
- **Release notes unavailable or too short to analyse**: `safe_to_update=true`,
  `create_hitl_card=true`, recommend manual review.

`create_hitl_card` is always `true` for OS updates regardless of risk level.

## Investigation steps

1. `get_update_release_notes(target_version=<latest version>)` — fetch notes for the target
   OS version.
   - If the output is only a `<ha-alert>` block (e.g. "A reboot is required"), the WS endpoint
     returned a stub, not the notes. Call `fetch_url(<Release URL from the initial context>)`;
     it returns the GitHub release body. Do not conclude without reading it.
   - Look for a "Breaking changes" or "Deprecations" section.

2. Board-specific notes: check for mentions of Yellow, CM4, or ODROID in the release notes.
   `run_ha_command("ha host info --raw-json")` to confirm board type when relevant.

3. If the release notes mention a filesystem or bootloader change:
   `run_ha_command("ha os boards rpi firmware")` (CM4 boards) or inspect host info for context.

4. `check_config_against_breaking_change(config_key, description)` for any flagged item.

## Output guidance

- `risk_score`: "low" for patch releases without board-specific issues; "medium" for minor
  releases; "high" for changes that affect this board or storage layout.
- `prerequisites`: always note that a reboot is required. Add any manual pre-reboot steps.
- `confidence`: 0.9+ only when the full release notes were read.
