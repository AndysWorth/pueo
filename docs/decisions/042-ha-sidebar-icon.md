# ADR 042 — HA Sidebar: Custom Iconset, Pixel-Traced PNG, `extra_module_url`

**Status:** Accepted  
**Date:** 2026-10-14

## Context

`setup.sh` optionally adds Pueo to the Home Assistant sidebar as a Webpage dashboard so the dashboard is always one click away. The sidebar entry needs an icon.

HA sidebar icons must be **iconset names** (`mdi:…` or a custom `prefix:name`). Plain image URLs are not accepted: only the `path` of a single-colour filled SVG shape is rendered. The project's existing icon (`web/static/nav_32.svg`) is a 32×32 RGBA PNG embedded in an SVG `<image>` tag — not a vector path.

## Decision

### Custom iconset rather than `mdi:owl`

A generic MDI icon (e.g. `mdi:owl`) could be used but would look identical to any other app using that icon. A custom `pueo:owl` iconset gives Pueo a distinct, recognisable entry in the HA sidebar.

### Pixel-traced PNG path

Rather than drawing a new SVG or adding a vector asset to the repo, the existing `nav_32.svg` is decoded at install time:

1. `png_from_nav_svg()` — extracts the base64-encoded PNG from the SVG `xlink:href`.
2. `decode_png_rgba()` — stdlib-only DEFLATE/filter decoder; 8-bit RGBA, non-interlaced only. Raises `ValueError` for other formats.
3. `icon_path_from_rgba()` — thresholds pixels (alpha > 128, luminance < 180) and emits one `M x y h w v 1 h -w Z` rect per opaque-dark horizontal run. `viewBox "0 0 32 32"`.
4. `build_iconset_js()` — emits a JS module that registers `window.customIconsets["pueo"]` returning the single path for any icon name.

The result looks pixel-faithful and slightly blocky at 24 px. A redrawn vector icon is a possible future improvement; no new SVG asset is committed now.

### `frontend: extra_module_url` not a Lovelace resource

A Lovelace resource (`/api/lovelace/resources`) is only loaded when a dashboard is open. That means the sidebar icon would appear blank until the user opens a dashboard. `frontend: extra_module_url` in `configuration.yaml` loads the module globally on every page, so the icon is always present.

`add_extra_module_url()` performs a text-level edit to avoid round-tripping YAML through PyYAML (which cannot preserve `!include`/`!secret` tags). It handles five cases: URL already present (idempotent), existing `extra_module_url:` list (append), `frontend:` without the key (insert), no `frontend:` block (append). It returns `None` for inline or flow-style `frontend:` entries that it cannot safely edit; the caller prints a manual snippet and continues.

### Setup-time config write under the backup chain

`install_ha_sidebar()` follows the project's safety invariant:

1. SFTP-write `/config/www/pueo/pueo-icons.js` (new, inert file — no backup needed).
2. Read `/config/configuration.yaml`. If unchanged or `add_extra_module_url` returns `None`, skip to the dashboard step.
3. `execute_remote_backup()` → `record_backup_slug()` before writing.
4. Write the modified config. Run `ha core check`; restore the original on failure and abort.
5. On pass: `execute_ha_core_restart(skip_backup=True)`. Set `_config_restart_triggered = True`.
6. SFTP-write `.storage/lovelace_dashboards` and `.storage/lovelace.pueo-dashboard`. Idempotent: reads the existing registry and only writes when changed.
7. If storage was changed and no restart was triggered in step 5: `execute_ha_core_restart(skip_backup=True)`.

### Direct `.storage/` writes instead of WebSocket dashboard API

HA 2026.10.1 does not expose a `lovelace/dashboards/create` WebSocket command (returns `unknown_command`). Dashboard creation is done by writing directly to the `.storage/` persistence layer instead:

- `/config/.storage/lovelace_dashboards` — the dashboard registry (list of all named dashboards).
- `/config/.storage/lovelace.pueo-dashboard` — the per-dashboard config (iframe strategy + URL).

Both files are SFTP-written over the existing SSH transport. HA reads these files on startup, so a restart is triggered whenever either file changes and no restart has already been triggered by step 5.

`setup.sh` section 5.6 wraps the `--mode ha-sidebar` invocation in `|| warn …` so a failure never aborts the rest of setup.

## Consequences

- The sidebar icon is derived from `nav_32.svg` at install time. Changing the nav icon requires re-running `--mode ha-sidebar`.
- Adding `extra_module_url` requires an HA Core restart. The backup chain ensures the restart is safe.
- Rollback: restore the backup slug, or remove the `frontend.extra_module_url` line and `/config/www/pueo/`, delete the `/config/.storage/lovelace_dashboards` entry for `pueo-dashboard`, delete `/config/.storage/lovelace.pueo-dashboard`, and revert the commit.
- The `add_extra_module_url` parser handles the common YAML layouts but returns `None` for uncommon ones; manual instructions are printed in that case.
- No HA WebSocket connection is required — the orchestrator uses SSH only.
