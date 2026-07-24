# HA Resource Stewardship

Part of the [Roadmap](../roadmap.md) · Milestone 4.5.

---

### Problem

Pueo's safety invariant requires a confirmed backup before every write. The HA Yellow has a constrained disk — each new backup risks exhausting available space, which causes `ha backups new` to fail, breaking the entire pipeline. Additionally, without disk and memory visibility, Pueo cannot anticipate or prevent HA instability caused by resource exhaustion.

---

### Feature 1 — Disk & Memory Sensing (item 29)

Poll `ha host info` via SSH on a configurable interval. Extract disk and memory fields from the JSON response. Surface alerts in the HITL dashboard when thresholds are crossed.

**New config keys:**

| Key | Default | Meaning |
|-----|---------|---------|
| `RESOURCE_POLL_INTERVAL_SECONDS` | 300 | How often to check disk/memory |
| `HA_DISK_WARN_GB` | 5 | HITL alert threshold |
| `HA_DISK_CRITICAL_GB` | 2 | Block new backups; surface as CRITICAL |
| `HA_MEM_WARN_MB` | 256 | HITL alert threshold |

**Behaviour:**
- HITL dashboard card when disk < WARN or memory < WARN
- `execute_remote_backup()` blocks early (before SSH round-trip) when disk < CRITICAL — raises `DiskCriticalError` with the current free-space value in the message
- Polling runs as an `asyncio.create_task()` alongside the existing monitoring loop

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

### Done when

- `ha host info` is polled on schedule; disk/memory alerts appear in the HITL dashboard when thresholds are crossed; `execute_remote_backup()` blocks when disk < CRITICAL
- Every new backup triggers an SFTP offload to Pueo; SHA-256 verified; inventory updated in SQLite
- HA retains at most `BACKUP_RETAIN_ON_HA` backups; no backup deleted from HA without a confirmed local copy
- `--mode backup-status` prints a clean inventory table
- All new config keys have tests in `TestConfigDefaults`
- SFTP transfer has `FakeSSHClient` tests covering success and checksum-failure paths
- Migration tested against real `ha_agent_state.db`
