# HA Update Manager

Part of the [Roadmap](../roadmap.md) · Phase 12.

---

### Problem

Home Assistant ships breaking changes, CLI renames, and config deprecations regularly. Pueo currently has no way to detect that an update is available, evaluate whether the update is safe for this installation, or execute the update with the safety invariant intact. Without this capability, users must update manually and discover integration breakage after the fact.

Pueo is also self-exposed: the SSH CLI commands it runs (`ha core check`, `ha apps info`, `ha backup new`, etc.) can be renamed or removed between HA versions — as already happened with `ha addons` → `ha apps`. A Core update that silently breaks Pueo's command catalog leaves the system blind.

---

### Architecture notes

**REST vs SSH for update detection.** HA exposes update availability as `update.*` state entities accessible via `GET /api/states`. These are REST-pollable without WebSocket. SSH `ha core info --raw-json` also returns `version_latest` and `update_available`. Both methods are used: REST polling for the monitor loop (low overhead), SSH for the detailed pre-update audit.

**New `HARestClient`.** All REST calls from Pueo's machine to `http://HA_HOST:8123/api/` go through a new `HARestClient` implementing `HARestClientProtocol` (defined in `interfaces.py`). Follows the same optional-injection pattern as `SSHClientProtocol`: functions accept `ha_rest_client: Optional[HARestClientProtocol] = None`. `FakeHARestClient` is the test double.

**Add-on updates via Supervisor HTTP API.** There is no `ha apps update` CLI command. Add-on updates use `POST http://supervisor/store/addons/<slug>/update` with `Authorization: Bearer $SUPERVISOR_TOKEN` — the same curl-over-SSH pattern used in `installer.py` step 8.

**No dry-run.** `ha core update` has no `--check` or `--dry-run` flag. Pre-update safety comes entirely from the breaking-change analysis (item 34) and HITL approval (item 35). The `--backup` flag on `ha core update` is intentionally NOT used because Pueo's safety invariant already triggers `execute_remote_backup()` before issuing the update command — two consecutive backups would waste disk.

**Update entity naming.** HA 2022.4+ surfaces updates as `update.*` entities:
- `update.home_assistant_core_update`
- `update.home_assistant_os_update`
- `update.home_assistant_supervisor_update`
- `update.<addon_name>_update` (one per installed add-on)

Entity state is `"on"` (update available) or `"off"` (up to date). Attributes: `installed_version`, `latest_version`, `release_summary`, `release_url`, `in_progress`, `update_percentage`.

**Prerequisite.** Items 29–32 (Resource Stewardship) must be complete before this phase begins. `execute_remote_backup()` must already block on `DiskCriticalError`, and disk free must be visible before presenting an update HITL card.

---

### Feature 1 — `HARestClient` + Update Entity Polling (item 33)

New `utils/ha_rest_client.py` with `HARestClient`. New protocol `HARestClientProtocol` in `interfaces.py`.

**New config keys:**

| Key | Source | Default | Meaning |
|-----|--------|---------|---------|
| `HA_API_PORT` | `config.yaml` | `8123` | Port for the HA REST API |
| `HA_API_TOKEN` | environment variable | — | Long-Lived Access Token (see credential note below) |
| `HA_UPDATE_NOTIFY_ON_AVAILABLE` | `config.yaml` | `true` | HITL notification when any update entity flips to `on` |
| `HA_UPDATE_CHECK_INTERVAL_HOURS` | `config.yaml` | `24` | How often the monitor loop polls for updates (0 = off) |

**Credential vs. config.yaml — why the distinction matters.**
Normal settings (ports, intervals, hostnames) are written to `config.yaml` because they describe the environment Pueo runs in and are safe to store as plaintext. Tokens and API keys are different: they grant access to live systems and must never appear in a file on disk, even a gitignored one — files can be included in backups, read by other processes, or accidentally committed if `.gitignore` is misconfigured.

`HA_API_TOKEN` is therefore read by `config.py` via `os.getenv("HA_API_TOKEN")`, not from `config.yaml`. This follows the same pattern as `ANTHROPIC_API_KEY` (Phase 17). `config.py` is still the single source of all settings — it just has two input paths: YAML for normal settings, environment for credentials. If `HA_UPDATE_CHECK_INTERVAL_HOURS > 0` and the key is absent from the environment, `config.py` raises a clear error at startup rather than silently failing later during an API call. When `HA_UPDATE_CHECK_INTERVAL_HOURS = 0` (update checking disabled), the missing key is ignored.

`config.yaml.default` documents `HA_API_TOKEN` with a comment explaining it must be set as an environment variable, not in the file. `setup.sh` prompts the user to export it in their shell profile (e.g. `~/.zshenv`) rather than writing it to `config.yaml`. The triple-update rule from ADR 001 still applies: `config.py` + `config.yaml.default` + `setup.sh`.

**`HARestClientProtocol` interface** (in `interfaces.py`):
```python
async def get_states(self, prefix: str | None = None) -> list[dict]: ...
async def get_state(self, entity_id: str) -> dict: ...
async def call_service(self, domain: str, service: str, payload: dict) -> dict: ...
```

**`get_update_status()` function** — reads all `update.*` entities; returns list of `UpdateStatus` dataclasses:
```python
@dataclass
class UpdateStatus:
    component: str            # "core", "os", "supervisor", or add-on slug
    entity_id: str
    installed_version: str
    latest_version: str
    update_available: bool
    release_url: str | None
    release_summary: str | None
    in_progress: bool
```

**`--mode update-check`** — one-shot: prints a table of all components and their update status, runs the breaking-change analysis (item 34) for any Core update available, and exits. Does not modify anything.

**Monitor loop integration** (in `ha_log_monitor.py`) — a periodic `asyncio.create_task()` alongside the existing log-tail loop. Every `HA_UPDATE_CHECK_INTERVAL_HOURS`, call `get_update_status()`; if any `update_available = true`, send a HITL notification and set an in-memory flag so the notification is not repeated until the update entity clears.

---

### Feature 2 — Breaking Change Analysis (item 34)

LLM analysis of release notes against the current installation. Advisory only — never a hard gate.

**Release notes fetch.** For any Core update, fetch the GitHub release page for the target version. URL pattern: `https://github.com/home-assistant/core/releases/tag/<version>`. Cache the response as plaintext in `HA_UPDATE_RELEASE_NOTES_CACHE_DIR` (new config key, default `.cache/ha_release_notes/`). One WAN fetch per version; subsequent calls read cache. This fetch happens during `--mode update-check` or when a HITL card is generated — never during an active repair cycle.

**New `UpdateReadinessReport` Pydantic schema:**
```python
class UpdateReadinessReport(BaseModel):
    target_version: str
    safe_to_update: bool          # advisory
    breaking_changes: list[str]   # changes that may affect this install
    affected_config_keys: list[str]  # keys in current config.yaml flagged in breaking changes
    pueo_command_risks: list[str] # Pueo SSH commands appearing in breaking changes
    recommendation: str           # plain-English summary
```

**LLM call context:**
- System prompt: explain the task and that the analysis is advisory
- Current `configuration.yaml` content (truncated to `MAX_PROMPT_TOKENS`)
- Release notes plaintext (truncated to remaining token budget)
- Pueo's SSH command catalog as a fixed list in the prompt

**Output:** `UpdateReadinessReport` is attached to the HITL update card (item 35) and printed by `--mode update-check`.

**New config key:**

| Key | Default | Meaning |
|-----|---------|---------|
| `HA_UPDATE_RELEASE_NOTES_CACHE_DIR` | `.cache/ha_release_notes/` | Local cache for fetched release notes |

---

### Feature 3 — HITL Update Approval Card (item 35)

Always CRITICAL risk for Core and OS updates regardless of autonomy level. Add-on updates are MEDIUM risk and may auto-execute at autonomy level 4.

**Card content:**

| Section | Content |
|---------|---------|
| Component | Name, current → target version |
| Release summary | From `release_summary` attribute or cached release notes |
| Breaking changes | `UpdateReadinessReport.breaking_changes` list (advisory banner) |
| Affected config keys | Highlighted if non-empty |
| Pueo command risks | Listed if non-empty — flagged as "Pueo may need attention after update" |
| Disk free | From resource stewardship sensors; red if below WARN threshold |
| Actions | **Approve**, **Defer** (per component independently) |

**Approval scope.** Approving Core does not approve OS or add-ons. Each component requires its own approval. Deferring a component suppresses its HITL card for 24 hours.

**No auto-dismiss.** The HITL card remains open until explicitly approved or deferred.

---

### Feature 4 — Safe Update Execution + Post-Update Validation (item 36)

Only runs after explicit HITL approval from item 35.

**Execution sequence:**

1. `execute_remote_backup()` → `record_backup_slug()` — safety invariant, as always
2. `ha core update --no-progress` via SSH (suppress progress spinner for non-TTY)
3. Poll `ha core info --raw-json` every 15 seconds until `version == latest_version` and `update_available == false`, or 8-minute timeout
4. If timeout: surface HITL alert "Update may still be in progress — check HA UI" — do NOT attempt rollback (HA Supervisor manages its own rollback state)
5. On success: run `ha core check`, fetch 100 log lines, LLM triage (`LogEvaluation`)
6. Post-update HITL card: update complete, config valid/invalid, log triage summary, Pueo self-check results (item 37)

**OS update.** Same sequence using `ha os update --no-progress`. OS updates typically require a reboot; poll for HA to come back online (TCP connect to `HA_HOST:8123`) rather than polling `ha os info`.

**Add-on updates.** Via SSH-executed curl against the Supervisor API:
```bash
curl -sf -X POST \
  -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
  http://supervisor/store/addons/<slug>/update
```
Poll `ha apps info <slug>` until `state: started` with the new version, or 3-minute timeout. MEDIUM risk; may auto-execute at autonomy level 4.

---

### Feature 5 — Pueo Self-Check After Core Update (item 37)

After a Core update completes, Pueo verifies its own integration is intact before declaring success.

**Checks:**
- `ha core check` — validate config is still accepted
- `ha core info --raw-json` — confirms CLI responds
- `ha apps list` — confirms apps CLI responds  
- `ha apps info db21ed7f_netalertx_fa` — confirms NetAlertX CLI path works
- `ha backup new --name pueo_selfcheck_DELETE_ME` — optional; only if disk is well above WARN threshold; delete immediately after slug is confirmed. Skip if disk is constrained.

**LLM cross-reference.** Pass the release notes + Pueo's full SSH command catalog to the LLM and ask: "Do any commands in this catalog appear in the breaking changes or migration notes?" Append results to the post-update HITL card under "Pueo self-check".

**Monitor-loop update detection.** In `ha_log_monitor.py`, add a periodic co-routine that calls `get_update_status()` every `HA_UPDATE_CHECK_INTERVAL_HOURS`. If any update is available, fire a HITL notification and pause re-checking until the update entity clears.

---

### Done when

- `HARestClientProtocol` in `interfaces.py`; `FakeHARestClient` in tests
- `GET /api/states/update.*` polling detects all available updates
- `--mode update-check` prints a version table and advisory breaking-change report
- Monitor loop fires a HITL notification when an update becomes available; does not repeat
- Core and OS update HITL cards always require approval regardless of autonomy level
- Add-on updates are MEDIUM risk; auto-execute at autonomy level 4
- `execute_remote_backup()` runs before every update (safety invariant)
- Post-update: config check, log triage, and Pueo self-check all run and surface results
- All new config keys have tests in `TestConfigDefaults`
- `FakeHARestClient` used in all unit tests; no live HA REST calls in unit suite
