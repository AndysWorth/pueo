"""Migrate Pueo runtime state from CWD-relative checkout paths to platform directories.

Usage: python main.py --mode migrate-paths [--dry-run]

For each path config key, detects data at the old CWD-relative location and moves it
to the platform-appropriate absolute path from paths.get_dirs(). Updates config.yaml
to record the new absolute paths.

Safe by design:
  - Never moves a file when the destination already exists (warns instead).
  - Dry-run mode prints what would change without touching anything.
  - Never moves config.yaml itself (credentials; user must do this consciously).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import yaml

from paths import get_dirs


# (config_key, subcategory, getter)
# getter returns the new absolute Path from PueoDirectories
_PATH_KEYS: list[tuple[str, str, str]] = [
    # (yaml_dotted_key, PueoDirectories attribute, subdirectory)
    ("agent.db_path", "state_dir", "ha_agent_state.db"),
    ("agent.log_file", "log_dir", "pueo.log"),
    ("agent.notify_watch_dir", "state_dir", "hitl"),
    ("agent.backup_local_dir", "data_dir", "backups"),
    ("agent.pueo_archive_dir", "data_dir", "archives"),
    ("agent.chromadb_path", "data_dir", "chromadb"),
    ("agent.update_release_notes_cache_dir", "cache_dir", "ha_release_notes"),
    ("agent.rag_hacs_cache_dir", "cache_dir", "hacs_changelogs"),
    ("agent.rag_ha_docs_cache_dir", "cache_dir", "ha_docs"),
    ("agent.ha_source_cache_dir", "cache_dir", "ha_source"),
    ("agent.case_ingest_cache_dir", "cache_dir", "case_ingest"),
]

# Old relative defaults used before Phase 2, expanded relative to resources_dir
_OLD_RELATIVE_DEFAULTS: dict[str, str] = {
    "agent.db_path": "ha_agent_state.db",
    "agent.log_file": "pueo.log",
    "agent.notify_watch_dir": "hitl",
    "agent.backup_local_dir": "backups",
    "agent.pueo_archive_dir": "archives",
    "agent.chromadb_path": "chromadb",
    "agent.update_release_notes_cache_dir": ".cache/ha_release_notes",
    "agent.rag_hacs_cache_dir": ".cache/hacs_changelogs",
    "agent.rag_ha_docs_cache_dir": ".cache/ha_docs",
    "agent.ha_source_cache_dir": ".cache/ha_source",
    "agent.case_ingest_cache_dir": ".cache/case_ingest",
}


def _get_nested(d: dict, dotted_key: str) -> Optional[str]:
    """Get a value from a nested dict using a dotted key like 'agent.db_path'."""
    parts = dotted_key.split(".", 1)
    if len(parts) == 1:
        return d.get(parts[0])
    sub = d.get(parts[0])
    if not isinstance(sub, dict):
        return None
    return _get_nested(sub, parts[1])


def _set_nested(d: dict, dotted_key: str, value: str) -> None:
    """Set a value in a nested dict using a dotted key, creating intermediate dicts."""
    parts = dotted_key.split(".", 1)
    if len(parts) == 1:
        d[parts[0]] = value
        return
    if parts[0] not in d or not isinstance(d[parts[0]], dict):
        d[parts[0]] = {}
    _set_nested(d[parts[0]], parts[1], value)


def migrate_paths(
    config_path: Path,
    dry_run: bool = False,
) -> None:
    """Detect old checkout-relative data and move it to platform directories.

    Args:
        config_path: Path to config.yaml.
        dry_run: If True, print what would happen but don't move anything.
    """
    dirs = get_dirs()
    resources_dir = dirs.resources_dir  # the repo root — where old data lived

    cfg: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

    moved: list[str] = []
    skipped_both_exist: list[str] = []
    no_old_data: list[str] = []
    already_correct: list[str] = []
    updated_keys: list[str] = []

    prefix = "[DRY RUN] " if dry_run else ""

    for dotted_key, dirs_attr, subpath in _PATH_KEYS:
        new_path = getattr(dirs, dirs_attr) / subpath

        # Determine old path: what config.yaml currently has, or the old default
        configured = _get_nested(cfg, dotted_key)
        if configured:
            old_path = Path(configured)
            if not old_path.is_absolute():
                old_path = resources_dir / old_path
        else:
            old_default = _OLD_RELATIVE_DEFAULTS.get(dotted_key, "")
            old_path = resources_dir / old_default if old_default else None  # type: ignore[assignment]

        if old_path is None:
            no_old_data.append(dotted_key)
            continue

        # Normalize trailing slashes for comparison
        old_path = old_path.resolve() if old_path.exists() else old_path

        if (
            old_path == new_path.resolve()
            if new_path.exists()
            else old_path == new_path
        ):
            already_correct.append(dotted_key)
            continue

        old_exists = old_path.exists()
        new_exists = new_path.exists()

        if not old_exists:
            no_old_data.append(dotted_key)
            # Still update config.yaml to point at the new location
            _set_nested(cfg, dotted_key, str(new_path))
            updated_keys.append(dotted_key)
            continue

        if old_exists and new_exists:
            print(
                f"{prefix}⚠  {dotted_key}: both {old_path} and {new_path} exist — "
                "leaving in place; resolve manually."
            )
            skipped_both_exist.append(dotted_key)
            continue

        # Move old → new
        print(f"{prefix}→  {dotted_key}: {old_path} → {new_path}")
        if not dry_run:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_path), str(new_path))
        moved.append(dotted_key)
        _set_nested(cfg, dotted_key, str(new_path))
        updated_keys.append(dotted_key)

    # --- user_tools/ → state_dir/tools/ (no config key; hardcoded special case) ---
    old_tools = resources_dir / "user_tools"
    new_tools = dirs.state_dir / "tools"
    old_tools_py = (
        [p for p in old_tools.glob("*.py") if p.name != "__init__.py"]
        if old_tools.exists()
        else []
    )
    if old_tools_py:
        if new_tools.exists() and any(new_tools.glob("*.py")):
            print(
                f"{prefix}⚠  user_tools/: both {old_tools} and {new_tools} have .py files — "
                "leaving in place; resolve manually."
            )
            skipped_both_exist.append("user_tools")
        else:
            print(
                f"{prefix}→  user_tools/ → {new_tools} ({len(old_tools_py)} tool file(s))"
            )
            if not dry_run:
                new_tools.mkdir(parents=True, exist_ok=True)
                for tool_py in old_tools_py:
                    shutil.move(str(tool_py), str(new_tools / tool_py.name))
            moved.append("user_tools")

    # Write updated config.yaml
    if updated_keys and not dry_run:
        with open(config_path, "w") as f:
            yaml.dump(
                cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False
            )
        print(f"\nUpdated config.yaml with {len(updated_keys)} absolute path(s).")

    # Summary
    print(f"\n{'DRY RUN ' if dry_run else ''}Migration summary:")
    print(f"  Moved:              {len(moved)}")
    print(f"  No old data:        {len(no_old_data)}")
    print(f"  Already correct:    {len(already_correct)}")
    print(f"  Both exist (skipped): {len(skipped_both_exist)}")

    if skipped_both_exist:
        print("\nSkipped (both old and new locations have data):")
        for key in skipped_both_exist:
            print(f"  {key}")
        print("Resolve these manually, then run migrate-paths again.")

    if dry_run:
        print("\nRe-run without --dry-run to apply.")

    if not dry_run and not moved and not skipped_both_exist:
        print("\nNothing to migrate — data already at platform locations.")


def run_migrate(config_path: Path) -> None:
    """Entry point called from main.py --mode migrate-paths."""
    dry_run_env = os.environ.get("PUEO_MIGRATE_DRY_RUN", "").lower() in (
        "1",
        "true",
        "yes",
    )
    migrate_paths(config_path, dry_run=dry_run_env)
