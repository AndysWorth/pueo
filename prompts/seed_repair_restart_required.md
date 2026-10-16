---
signature: repair:*:restart_required
---
# restart_required — Component update applied, restart needed

**What it means:** A Home Assistant component (Core, HACS integration, or custom integration)
was updated and requires a HA Core restart to activate the new version.

**Do NOT assume this is always a HA Core update.** HACS integrations and custom components
also produce `restart_required` issues after Pueo or the user applies an update.

## Step 1 — Identify what was updated

- Check `issue_id` in the initial_context: it often contains the HACS repo id
  (e.g. `hacs/restart_required_605635573_tags/v5.11.1`).
- `run_ha_command("ha core info")` — compare `version` vs `version_latest`.
  If they differ, a Core update was just applied. If they match, a HACS or custom component
  was updated.
- If the issue_id contains a HACS repo number, note the integration name from context
  (the update manager will have logged it).

## Step 2 — Confirm restart is safe

- `run_ha_command("ha core check")` — confirm config is valid.
- If the config check fails, set `requires_hitl=true, action="dismiss"` with an explanation
  that the config must be fixed before restarting.
- If the config check passes, a restart is safe.

## Step 3 — Decide action

- Always set `requires_hitl=true, action="restart"`.
- For a HACS/custom integration restart: note the integration name in the explanation.
- For a Core restart: note the version being activated.

**Explanation template:**
"[Integration name / Home Assistant Core] was updated and needs a restart to activate the
new version. HA is running correctly right now — this restart is safe to approve."

## Note on restart autonomy

The AutonomyGate decides whether to auto-restart or send a card. Do NOT check autonomy
levels in this runbook — just set `requires_hitl=true, action="restart"` and let the
gate handle it.
