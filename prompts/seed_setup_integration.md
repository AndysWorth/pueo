---
signature: setup:integration:*
---
# Integration setup — method

**Use when:** the user wants a downloaded (usually HACS) integration set up, re-enabled
or re-added, or asks "help me configure <domain>". The goal is a plan the user can
approve: one answer per config-flow step, with the evidence behind each answer.

## 1. Prerequisites — check before proposing anything

- `read_file("/config/custom_components/<domain>/manifest.json")`: confirm the domain,
  `version`, `config_flow: true` (false means YAML-only, so advise only and never write
  YAML for it without the user asking), `dependencies` (each must already be loaded),
  and `requirements`.
- Minimum HA version: HACS repositories keep it in `hacs.json` → `homeassistant`, not in
  the manifest. Compare it with the HA version from `get_ha_profile`. Too old → stop
  and say which HA update is needed.
- A pending `update.<domain>*` entity means update first. The flow you plan for must be
  the flow that will run.

## 2. Existing entries — this decides the path

Call `get_config_entries_all` and filter on the domain. A "downloaded but not loaded"
integration often already has an entry.

| Found | Path |
|---|---|
| No entry | New config flow (steps 3–5) |
| Entry `disabled_by: user` | **Ask why it was disabled.** Default: re-enable it. Its entity IDs, history and area assignments survive, and the integration migrates old entry versions on load. If the user wants a clean start: delete the entry, then run a new flow. |
| Entry `source: ignore` | It was ignored from discovery. Un-ignore, or delete it, before a new flow. |
| Entry loaded with errors | This is not a setup task. Use the integration-error runbook. |

A new flow for a location or device that is already configured, **even a disabled
one**, aborts with `already_configured`, because the unique_id collides. Never propose
a new flow next to a disabled entry for the same thing.

## 3. Learn the flow from the installed code, not from memory

- Read `/config/custom_components/<domain>/translations/en.json` (or `strings.json`).
  `config.step.<id>.data` lists every field label per step, `menu_options` lists menu
  branches, and `config.abort`/`config.error` list the failure keys.
  `options.step` describes the post-setup "Configure" dialog.
- Read `config_flow.py` to learn the step order, the defaults (many come from the HA
  Home zone), and **which submit creates the entry** (`async_create_entry`). A menu
  choice can create an entry immediately. Never submit a step to "see what happens".
- Read the integration's docs (manifest `documentation`) with `fetch_url` when
  `ALLOW_DIAGNOSTIC_WAN` is on.

## 4. Choose each answer from evidence

- Prefer the defaults the flow derives from the HA profile (home coordinates, units,
  time zone) unless the user asked for something else.
- When a field's options come from an external service, look the value up the same way
  the integration does (`fetch_url` to that API) and cite the result.
- **Secrets** (API keys, passwords, tokens): never invent them and never read them from
  `secrets.yaml`. Mark the field "user supplies". A plan that needs a secret is
  advise-only, and the user completes it in the UI at
  `https://my.home-assistant.io/redirect/config_flow_start/?domain=<domain>`.
- Start minimal: one entry that proves the integration works. Extra entries (more
  locations, zones, accounts) come after it verifies.

## 5. After the entry exists

- **Options:** review polling intervals and anything that stores data under `/config`
  or downloads media. On small hosts, prefer the lower setting.
- **Areas:** HA's "assign area" dialog after setup is UI-only and not part of the flow.
  List area assignments as a follow-up (`get_area_layout` shows the areas).
- **Recorder and noise:** name the high-churn or never-used entities the user may want
  to disable or exclude from the recorder.
- **Dashboards and automations:** suggest a card. Offer an automation only via
  `propose_automation`.

## 6. Verify and roll back

- Verified = the new entries are `loaded`, at least one entity per entry has a real
  state (not `unavailable`/`unknown`) after the first update interval, and
  `read_logs` shows no new errors from the domain's logger.
- Rollback = delete the new entries. The backup taken before the write restores
  `.storage` if more is needed. A delete-and-re-add loses entity history, so say so
  before proposing one.
