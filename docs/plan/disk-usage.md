# Disk Usage Tab

## Phase Deliverables

| Item | Description |
|------|-------------|
| DU-1 | `utils/disk_usage.py`: dataclasses, SSH helpers, `fetch_disk_breakdown()`, cache, `DiskUsagePoller` |
| DU-2 | `config.py` key `DISK_USAGE_POLL_INTERVAL_SECONDS` (default 300); `config.yaml.default` |
| DU-3 | `web/templates/disk.html`: 4-section layout, disk gauge, mini-bars, refresh button |
| DU-4 | `web/templates/base.html` nav link; `web/dashboard.py` `GET /disk` + `POST /disk/refresh` routes |
| DU-5 | `main.py` supervisor registration of `disk_usage_poll` loop |
| DU-6 | Tests: `test_utils.py` (35 tests), `test_dashboard.py` (5 tests), `test_config.py` (2 tests) |

## Context

HA disk is at 83% used (10.8/13.6 GB). Previously there was no UI way to see what was consuming space; the Backups tab shows backup files but not the HA database (94.5 MB), custom components (52.8 MB), or addon data (20.6 MB). This tab gives ongoing operational visibility into disk pressure.

**Why not start from `/`?** The HA Core container bind-mounts the same 13.6 GB partition to many paths simultaneously (`/homeassistant`, `/backup`, `/addon_configs`, `/share`, `/media`, `/ssl` all share the same storage). System directories (`/usr` 129 MB, `/bin` 1.7 MB, etc.) are read-only OS files managed by HAOS — not user-actionable. The meaningful view is 4 user-actionable groups.

## SSH Commands (verified on live HA 2026.8.0 / HAOS 18.2)

```bash
# Total disk stats — reuses _parse_host_info from utils/resource.py
ha host info  # → YAML: disk_free, disk_total, disk_used in GB

# Per-path sizes — single round-trip for all 4 sections
# (python3 not available on HAOS host; parse entirely in Pueo's Python)
du -sh /homeassistant/* /backup/* /addon_configs/* /share /media /ssl 2>/dev/null
# → "<size>\t<path>" per line

# Addon slug → friendly name mapping
ha apps list --raw-json  # Note: 'ha addons' is deprecated; use 'ha apps'
# → {"result":"ok","data":{"addons":[{"name":"...","slug":"..."}]}}

# DB table breakdown (graceful fallback if sqlite3 unavailable)
sqlite3 /homeassistant/home-assistant_v2.db \
  "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 10;" 2>/dev/null
# → "table_name|bytes" per line — NOTE: sqlite3 not available on HAOS 18.2 (confirmed)
```

## Architecture

### `utils/disk_usage.py`

**Dataclasses:**
- `DiskItem`: path, name, size_bytes, size_human, is_empty (≤ 8192 bytes), pct_of_section
- `DiskSection`: title, items (sorted desc), total_bytes, total_human, is_empty
- `DiskBreakdown`: sections (4), disk_used/total/free_gb, disk_used_pct, fetched_at, db_tables (None if sqlite3 unavailable)

**Private helpers:**
- `_parse_size_to_bytes(s)` — handles "94.5M", "4.0K", "1.7G" (1024-based busybox format)
- `_bytes_to_human(n)` — formats bytes to "94.5 MB" etc.
- `_parse_du_output(s)` — tab-separated; space-separated fallback; skips blank/malformed
- `_parse_sqlite3_output(s)` — pipe-separated "name|bytes"; returns sorted list
- `_fetch_addon_names(ssh)` — `ha apps list --raw-json`; returns `{}` on any error
- `_fetch_db_tables(ssh)` — `sqlite3 ... dbstat`; returns `None` if unavailable/fails
- `_build_section(title, path_sizes, addon_names=None)` — builds sorted `DiskSection`

**Public API (mirrors `utils/resource.py`):**
- `fetch_disk_breakdown(ssh)` — 4 SSH calls, returns `DiskBreakdown`
- `update_disk_breakdown(b)` / `get_disk_breakdown()` — module-level cache
- `DiskUsagePoller(ssh, interval_seconds)` — supervisor-compatible loop; publishes `disk_usage` SSE event

### 4 Logical Sections

| Section | Paths | Notes |
|---------|-------|-------|
| HA Config & Database | `/homeassistant/*` | Includes DB (94.5 MB), WAL (4.1 MB), custom_components (52.8 MB) |
| Backups | `/backup/*` | Individual tar files (~58 MB each on this host) |
| Addon Data | `/addon_configs/*` | Slug→name via `ha apps list --raw-json` |
| Shared Storage | `/share`, `/media`, `/ssl` | Currently empty; monitored for growth |

### Dashboard routes

- `GET /disk` — reads `get_disk_breakdown()` cache (no SSH call); renders `disk.html`
- `POST /disk/refresh` — fresh SSH query, updates cache, returns `{"ok": true}`

Both routes use lazy import of `utils.disk_usage` inside the function body (existing dashboard pattern).

### Template (`disk.html`)

Layout:
1. Header: title + "Updated Xs ago" + Refresh button
2. Inline result div (hidden; shown after refresh — same JS pattern as backups.html preflight button)
3. Overall disk gauge card with green/orange/red coloring based on warn/critical thresholds
4. Four section cards with per-item rows (name | size | 100px mini-bar)
5. DB table breakdown: `<details>/<summary>` under the `home-assistant_v2.db` row; shows "sqlite3 not available" when `db_tables is None`

### Supervisor

`disk_usage_poll` loop registered in `main.py` alongside `resource_poll`. Appears in the Overview loop health table. Runs every `DISK_USAGE_POLL_INTERVAL_SECONDS` (default 300s).

## Live HA Data (2026-08-07)

```
/homeassistant/home-assistant_v2.db        94.5 MB
/homeassistant/custom_components           52.8 MB
/homeassistant/home-assistant_v2.db-wal     4.1 MB
/homeassistant/tts                         644 KB
/homeassistant/zigbee.db                   368 KB
/backup/17ddb25f.tar                       58.7 MB
/backup/automatic_backup_2026_8_0_...tar   58.4 MB
/addon_configs/db21ed7f_netalertx_fa       15.2 MB
/addon_configs/db21ed7f_netalertx           5.4 MB
/share, /media, /ssl                       empty
Disk: 10.8 GB used / 13.6 GB total (83%), 2.2 GB free
```

## Verification

1. CI gate: `black --check . && flake8 ... && mypy ... && bandit ... && pytest --cov --cov-fail-under=90 --ignore=tests/integration`
2. Start supervisor: `python main.py` — confirm `disk_usage_poll` appears in Overview loop table
3. Navigate to `/disk` — section cards visible; `home-assistant_v2.db` is largest item; "NetAlertX Full Access" and "NetAlertX" appear in Addon Data; Shared Storage shows "(empty)"
4. Click Refresh — POST `/disk/refresh` returns `{"ok": true}`, page reloads, "Updated 0s ago" shown
5. Disk gauge: at 83% with 2.2 GB free (below 5 GB warn threshold) → shows orange
6. DB tables note: "sqlite3 not available on this HAOS host" shown under `home-assistant_v2.db`

## Notes

- `sqlite3` is NOT available on HAOS 18.2 — the DB table breakdown always shows "not available" on this installation. The code still attempts the sqlite3 command so it would work on future HAOS versions or other installations that do have it.
- No new migration needed — this feature is entirely read-only SSH queries.
- Rollback: revert the commit; no schema changes to undo.
