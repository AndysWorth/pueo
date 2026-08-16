# HA Resource Stewardship

Part of the [Roadmap](../roadmap.md) · Milestone 4.5. **✅ Complete (2026-07-27) — PRs #61, #62, #63, #65**

---

### Problem

Pueo's safety invariant requires a confirmed backup before every write. The HA Yellow has a constrained disk — each new backup risks exhausting available space, which causes `ha backups new` to fail, breaking the entire pipeline. Additionally, without disk and memory visibility, Pueo cannot anticipate or prevent HA instability caused by resource exhaustion.

---

### Feature 1 — Disk & Memory Sensing (item 29)

Poll `ha host info` via SSH on a configurable interval. Extract disk and memory fields from the JSON response. Surface alerts in the dashboard when thresholds are crossed.

**New config keys:**

| Key | Default | Meaning |
|-----|---------|---------|
| `RESOURCE_POLL_INTERVAL_SECONDS` | 300 | How often to check disk/memory |
| `HA_DISK_WARN_GB` | 5 | approval alert threshold |
| `HA_DISK_CRITICAL_GB` | 3 | Block new backups; surface as CRITICAL |
| `HA_MEM_WARN_MB` | 256 | approval alert threshold |

**Behaviour:**
- dashboard card when disk < WARN or memory < WARN
- `execute_remote_backup()` blocks early (before SSH round-trip) when disk < CRITICAL — raises `DiskCriticalError` with the current free-space value in the message
- Polling runs as an `asyncio.create_task()` alongside the existing monitoring loop

**HA Supervisor interaction — why `HA_DISK_CRITICAL_GB` must stay well above 1 GB:**

The HA Supervisor has its own independent disk space guard at **1 GB free**. When free space falls below this threshold the Supervisor hard-blocks *all* Supervisor-managed operations with a `blocked from execution, not enough free space` error — including:

- `ha backups new` (which Pueo calls via `execute_remote_backup()`)
- HA Core updates, App installs, Git repo pulls, and observer tasks

This means if `HA_DISK_CRITICAL_GB` is set at or below 1 GB, Pueo's own block would never fire first — the Supervisor would refuse the backup before Pueo's guard could act, breaking the backup-safety invariant silently.

The minimum safe value is `1 GB + largest expected backup size`. A typical HA backup is 1–2 GB, so **the default is 3.0 GB** — leaving a 2 GB write buffer above the Supervisor's floor. Installations with larger backups (many add-ons, significant media) should raise this value.

Disk management is also layered: the Supervisor shows a persistent UI notification "Available space is less than 1GB!" before the hard block fires, and has known false-positive bugs where the block fires even with adequate space (stale measurements / wrong mount-point reads). The `ha repair` CLI command can reset a stale space measurement.

The full three-tier picture for HA disk free:

| Free space | Who acts | Effect |
|---|---|---|
| < `HA_DISK_WARN_GB` (default 5 GB) | Pueo | approval warning card; triggers backup offload + retention cleanup |
| < `HA_DISK_CRITICAL_GB` (default 3 GB) | Pueo | `DiskCriticalError`; repair pipeline aborted |
| < 1 GB | HA Supervisor | All Supervisor operations hard-blocked, including `ha backups new` |

**Before implementing:** Run `ha host info` on the live HA instance and lock in the exact JSON field names for `disk_free`, `disk_total`, `memory_free`, `memory_total`. HAOS field names have changed across versions.

---

### Feature 2 — Backup Inventory Tracking (item 30)

Extend SQLite `backup_registry` with complete backup inventory: size, location, and timestamps. Add a new migration version.

**New columns:**

| Column | Type | Meaning |
|--------|------|---------|
| `size_bytes` | INTEGER | From `ha backups list` output |
| `location` | TEXT | `'ha'` / `'pueo'` / `'both'` |
| `offloaded_at` | REAL | Unix timestamp of successful SFTP transfer |
| `deleted_from_ha_at` | REAL | Unix timestamp of confirmed HA-side delete |

**On startup:** Reconcile `ha backups list` output against SQLite. Mark any slug present on HA but missing from SQLite as `location = 'ha'`; mark any slug in SQLite-only as orphaned (log warning, do not delete automatically).

**Before implementing:** Check whether the running HA version uses `ha backup list` or `ha backups list`. This has changed across HAOS releases.

---

### Feature 3 — Backup Offloading (item 31)

After `execute_remote_backup()` confirms a slug, SFTP-pull the `.tar` file to Pueo's local machine.

**New config keys:**

| Key | Default | Meaning |
|-----|---------|---------|
| `BACKUP_OFFLOAD_ENABLED` | `true` | Enable/disable offloading |
| `BACKUP_LOCAL_DIR` | `./backups/` | Local directory for offloaded backups |

**Sequence:**
1. SFTP pull `/backup/<slug>.tar` → `BACKUP_LOCAL_DIR/<slug>.tar`
2. SHA-256 checksum of transferred file; compare against HA-side read (re-read remote if no hash in API response)
3. Update `location = 'both'` in `backup_registry`
4. If transfer fails: log warning, leave `location = 'ha'`, do not abort the repair cycle — the offload is best-effort; the backup still exists on HA

**Never delete from HA without `location = 'both'` confirmed.**

---

### Feature 4 — Retention Policy & Cleanup (item 32)

**New config keys:**

| Key | Default | Meaning |
|-----|---------|---------|
| `BACKUP_RETAIN_ON_HA` | 2 | Most-recent backups to keep on HA |
| `BACKUP_RETAIN_LOCAL_DAYS` | 30 | Days to keep local copies |

**Cleanup rules:**
- After each successful offload: if HA backup count > `BACKUP_RETAIN_ON_HA`, delete the oldest slugs from HA that are confirmed `location = 'both'`
- Nightly: purge local backups older than `BACKUP_RETAIN_LOCAL_DAYS`; update inventory records
- Never delete the most-recent backup from anywhere
- `python main.py --mode backup-status` — prints inventory table: slug, size, age, HA copy, Pueo copy
- Dashboard: backup inventory tab (slug list, size, HA ✓/✗, Pueo ✓/✗, age)

---

### Implementation notes (item 29, 2026-07-24) — PR #61

- `poll_disk_and_memory(ssh_client)` in `ha_agent_advanced.py`: runs `ha host info --raw-json` over SSH; extracts `disk_free` (float GB) and computes `mem_available_mb` from `/proc/meminfo MemAvailable`
- `DiskCriticalError` exception raised by `execute_remote_backup()` when `disk_free < HA_DISK_CRITICAL_GB`; polling loop surfaces dashboard cards for WARN thresholds
- Config keys added: `RESOURCE_POLL_INTERVAL_SECONDS` (300), `HA_DISK_WARN_GB` (5), `HA_DISK_CRITICAL_GB` (3), `HA_MEM_WARN_MB` (256)
- Verified `ha host info` field names on live HAOS 18.1: `disk_free`, `disk_total`, `disk_used` (float GB); memory from `/proc/meminfo` (no memory fields in `ha host info`)

### Implementation notes (item 30, 2026-07-24) — PR #62

- SQLite migration v5: adds `size_bytes INTEGER`, `location TEXT` (`'ha'`/`'pueo'`/`'both'`), `offloaded_at REAL`, `deleted_from_ha_at REAL` columns to `backup_registry`
- `reconcile_backup_inventory(ssh_client)` on startup: calls `ha backups list --raw-json`; marks slugs present on HA but missing from SQLite as `location='ha'`; logs orphan warning for SQLite-only slugs
- Confirmed backup list command: `ha backups list --raw-json` (NOT `ha backup list`)

### Implementation notes (item 31, 2026-07-24) — PR #63

- `offload_backup_to_local(slug, ssh_client)` in `ha_agent_advanced.py`: SFTP-pulls `/backup/<slug>.tar` to `BACKUP_LOCAL_DIR/<slug>.tar`; computes SHA-256 of transferred file and re-reads remote to verify; updates `location='both'` on success
- Config keys added: `BACKUP_OFFLOAD_ENABLED` (true), `BACKUP_LOCAL_DIR` (`./backups/`)
- Transfer failure logs a warning and leaves `location='ha'`; does not abort the repair cycle
- `backups/` directory added to `.gitignore`

### Implementation notes (item 32, 2026-07-27)

- `enforce_ha_retention(ssh_client)` in `ha_agent_advanced.py`: queries `backup_registry` for all non-deleted HA slugs; if count > `BACKUP_RETAIN_ON_HA`, deletes oldest slugs where `location='both'`; skips most-recent; logs warning on SSH error but continues
- `purge_local_backups()` in `ha_agent_advanced.py`: deletes `.tar` files older than `BACKUP_RETAIN_LOCAL_DAYS`; protects the single most-recently-offloaded slug
- Both wired into `ha_agent_advanced.main()` and `ha_agent_sandbox_engine.main()` after `offload_backup_to_local()`
- `print_backup_status()` prints slug/size/age/HA/Pueo table to stdout
- `--mode backup-status` in `main.py` calls `init_local_database()` then `print_backup_status()`
- `/backups` route in `web/dashboard.py` with `backups.html` template; nav link in `base.html`
- PR #65

### Done when

- `ha host info` is polled on schedule; disk/memory alerts appear in the dashboard when thresholds are crossed; `execute_remote_backup()` blocks when disk < CRITICAL
- Every new backup triggers an SFTP offload to Pueo; SHA-256 verified; inventory updated in SQLite
- HA retains at most `BACKUP_RETAIN_ON_HA` backups; no backup deleted from HA without a confirmed local copy
- `--mode backup-status` prints a clean inventory table
- All new config keys have tests in `TestConfigDefaults`
- SFTP transfer has `FakeSSHClient` tests covering success and checksum-failure paths
- Migration tested against real `ha_agent_state.db`
