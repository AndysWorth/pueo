# ADR 045 — Companion Provisioning: Deterministic Code, Always-Approve, Backup Chain

**Status:** Accepted  
**Date:** 2026-10-17

## Context

Pueo recommends two HACS companion integrations:

- **Spook** (`frenck/spook`): fires repairs for unknown entity references, which Pueo diagnoses. Requires a config entry; an empty schema config flow is sufficient.
- **ha-upgrade-advisor** (`brianegge/ha-upgrade-advisor`): provides breaking-change analysis before updates. Requires a config entry with a `agent_id` (a HA conversation agent) and specific options Pueo must control.

`setup.sh` previously only opened the HACS browser page, leaving three steps undone: download, restart HA, then complete the config flow. On a real instance, both integrations were downloaded via HACS with no config entry, so they had no effect. Pueo detected this and sent an info card saying "No action required" — which was incorrect for integrations Pueo itself recommends.

## Decision

### Deterministic code, no LLM

Every step of the provisioning chain is fixed:
1. `execute_remote_backup()` → `record_backup_slug()` — the safety invariant; `/config/custom_components` is a write.
2. HACS WS `hacs/repositories/add` — only if the repo is not already in HACS (idempotent).
3. HACS WS `hacs/repository/download {repository: <id>}` — only if not yet downloaded.
4. `ha core restart` via `execute_ha_core_restart(skip_backup=True)` — only if the integration is not yet loaded. **The TCP down-watch is started before the blocking SSH call** (see ADR feedback: `feedback-repair-executor-inert-ssh`).
5. REST `POST /api/config/config_entries/flow {"handler": domain}` → submit the flow input.
6. For upgrade_advisor: REST `POST /api/config/config_entries/options/flow {"handler": entry_id}` → submit Pueo's fixed options.
7. Verify: re-read config entries and confirm the domain is present.

The function `plan_steps(state) -> list[Step]` is a pure function over the observed state (`CompanionState`). It is idempotent: running setup again picks up wherever a previous run stopped.

There is no `AgentLoop`, no tool registration in any registry, and no MCP exposure. The LLM's role is limited to seeing the companion's state in the profile summary and telling the user to set it up from Settings (via the existing `format_profile_summary` text). It never has a tool that triggers provisioning.

This matches the ADR 042 precedent for the sidebar install: a code-driven write under the backup chain, after user consent.

### Always-approve — even at level 4 (FULL_AUTONOMOUS)

Downloading and installing third-party code from the internet, then restarting HA, is never auto-executed. Unlike an HA Core update (which has a signed chain of custody) or a config entry reload (which is reversible), a HACS download installs arbitrary Python from GitHub. The gate is bypassed at the call site (same style as the update-confidence bypass in `_finish_update_analysis()`), not inside `AutonomyGate`.

For the `--mode companion-setup` path (invoked by `setup.sh` or directly), consent is given interactively at the terminal. For the `CARD_TYPE_COMPANION_SETUP` dashboard path (S4), the card always requires explicit user approval.

### upgrade_advisor options Pueo sets

| Option | Value | Reason |
|---|---|---|
| `scan_on_update` | `False` | Pueo triggers analysis inside the update WorkItem, keeping it serialised. The upstream default (On) would start concurrent analyses. |
| `scan_hacs` | `True` | Pueo wants HACS-component breaking-change data. |
| `post_upgrade_check` | `True` | Pueo reads `read_post_upgrade_report` after a Core update. |
| `create_repairs` | `False` | Otherwise the advisor's repairs reach `repair_poll` and duplicate the update analysis card. |
| `include_addons` | `False` | Not reliably supported upstream. |
| `include_automations` | `True` | Pueo wants automation-impact data. |

The user picks the conversation agent. The built-in `conversation.home_assistant` is excluded because it cannot do structured reasoning. Cloud agents cost money. Small local models may not follow the upgrade_advisor's structured output requirement. The setup card and `--mode companion-setup --list-agents` surface these trade-offs.

### HACS calls use sync websockets in asyncio.to_thread

`hacs_add_repository`, `hacs_download`, and `hacs_list_repositories` use `websockets.sync.client` wrapped in `asyncio.to_thread`. This follows the same pattern as `utils/knowledge/hacs_scraper.py` (which already calls `hacs/repositories/list` this way). `asyncio.run()` inside `asyncio.to_thread()` raises a nested-loop error (see `feedback-asyncio-run-in-to-thread`); the sync client library avoids it.

### One-shot mode: `--mode companion-setup`

Modelled on `--mode ha-sidebar`. The supervisor is not running, so the work queue is absent — `run_companion_setup` calls `execute_remote_backup` and `execute_ha_core_restart` directly. Arguments: `--companion <domain>`, `--agent-id <id>` (upgrade_advisor only), `--dry-run` (print steps, no writes), `--list-agents` (list available conversation agents and exit).

## Consequences

- `utils/ha/companion_setup.py` is the authority on provisioning logic. Dashboard (S4) and `setup.sh` (S5) both call through it.
- New REST client methods: `start_config_flow`, `submit_config_flow`, `start_options_flow`, `submit_options_flow`.
- New WS client methods: `list_conversation_agents`, `hacs_list_repositories`, `hacs_add_repository`, `hacs_download`.
- All four are added to `HARestClientProtocol`, `HAWebSocketClientProtocol`, `FakeHARestClient`, `FakeHAWebSocketClient`.
- No new config keys, no migrations.
- `_notify_hacs_unloaded` (S4) will split: known companion domains get an actionable setup card; other HACS domains keep the current info card.
- `format_profile_summary` (S4) will be generalised to treat a companion as "present" when it is loaded or in `hacs_unloaded_domains`.
- `setup.sh` (S5) will replace "Open browser?" with inline provisioning using the new mode.
