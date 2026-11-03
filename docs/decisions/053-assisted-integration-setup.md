# ADR 053 — Assisted integration setup

**Status:** Accepted  
**Date:** 2026-11-03

## Context

Pueo detects HACS integrations that are downloaded but unconfigured
(`hacs_unloaded_domains` in `HAEnvironmentProfile`).  Before this ADR, Pueo
only sent an informational `hacs_unloaded:<domain>` card with an Acknowledge
button.  No session ran, no signature was assigned, and nothing was learned.

The companion provisioning path (`utils/ha/companion_setup.py`) covers Spook
and ha-upgrade-advisor only.  Its config-flow driver submits a single
pre-known step without schema introspection, which does not generalise to
arbitrary integrations.

Integration setup differs from repair in three ways:
1. The config-flow UI is the authoritative source of truth (not YAML).
2. Some fields require secrets the user must supply; Pueo must never invent
   credentials.
3. The same integration can exist in several states — no entry, disabled
   entry, or stale entry — each requiring a different action.

## Decision

**Research first, drive on approval.**  When the user clicks "Help me set
this up" on a `hacs_unloaded` card, Pueo runs `run_setup_investigation()`
(`utils/ha/integration_setup_investigation.py`), an `AgentLoop` session that:

1. Reads the local integration files via `inspect_custom_integration`:
   `manifest.json`, `hacs.json`, and `strings.json`/`translations/en.json`
   from `/config/custom_components/<domain>/`.
2. Probes the live config flow via `probe_config_flow` — starts the flow,
   returns the first-step summary, and **always aborts in `finally`** via
   REST DELETE.  The probe never submits any step because a menu submit can
   create an entry immediately (e.g. `noaa_it_all`'s `hurricane` option).
3. Checks prerequisites: HA min version (from `hacs.json` and the manifest),
   existing or disabled entries, in-progress flows.
4. Builds a `finish_setup_plan` payload: `steps[]` with `step_id`, `answers{}`,
   `rationale`, and `evidence_step`; `entry_action`; `needs_user_input[]` for
   any secret fields.

When `needs_user_input` is non-empty the card is **advise-only**: the step
list is shown as a checklist with a My-HA deep link
(`https://my.home-assistant.io/redirect/config_flow_start/?domain=<d>`), and
there is no Approve button.

**Always requires approval.**  Like companion provisioning (ADR 045), the
executor requires user approval at every autonomy level including
`FULL_AUTONOMOUS`.

**Three entry actions:**

| `entry_action` | Condition | Execution |
|---|---|---|
| `add` | No existing entry | Start a new config flow |
| `re_add` | Entry exists but user chose to replace it | Delete entry, then start a new config flow |
| `enable` | A disabled entry exists | WS `config_entries/disable` with `disabled_by: null`; no config flow |

**Execution chain** (`_execute_integration_setup` in `web/dashboard.py`):

1. Fallible preconditions (entry state matches `entry_action`, plan complete)
   — run **before** the backup.
2. `execute_remote_backup()` → `record_backup_slug()` (mandatory, never
   skipped).
3. For `add`/`re_add`: `drive_config_flow()` (`utils/ha/config_flow_driver.py`)
   submits each step from the plan.  On an unexpected step, errors, or an
   `abort` response, the executor stops and reports without rolling back (no
   HA state was written yet if the flow did not reach `create_entry`).
4. Options flow runs if `options{}` is non-empty.
5. After a 10-second settle: `_verify_integration_setup` confirms the entry
   is `loaded`, at least one entity is not `unavailable`/`unknown`, and no
   new domain errors appear in `ha core logs`.
6. `_verify_card_execution` marks `session_outcomes` success or failure,
   triggering the existing verdict distillation lane.

**`drive_config_flow` is the shared authority.**  `utils/ha/config_flow_driver.py`
handles `menu` steps (selects the matching option), `form` steps (maps each
field from the plan answers), unexpected steps (aborts), and `create_entry`/
`abort` terminal responses.  `companion_setup._run_config_flow` was refactored
to delegate to it so there is exactly one implementation of config-flow
walking.

**Signatures and runbooks.**  Setup sessions sign as
`setup:integration:<domain>` (`sig_setup()`).  The generic method seed is
`seed_setup_integration.md` (`setup:integration:*`).  Per-domain verdicts
live in seeds such as `seed_setup_noaa_it_all.md`.  The ADR 052 family-scope
logic promotes cross-domain lessons (e.g. "check hacs.json for min HA
version") to `setup:integration:*`.

## Consequences

- HACS-unloaded cards gain a "Help me set this up" button that triggers a
  full research + plan session.
- The probe is safe for integrations that create entries on menu submit,
  because it always aborts before submitting anything.
- `drive_config_flow` is now the single implementation of config-flow walking,
  shared by setup and companion provisioning.
- Secret-requiring integrations produce an advise-only card; Pueo never
  invents credentials.
- Disabled entries are detected and re-enabled without a fresh config flow.
- The existing verdict distillation lane produces per-domain and
  cross-domain runbooks after verified sessions.
