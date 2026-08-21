"""Tests for --mode migrate-paths (utils/system/migrate_paths.py)."""

import importlib
import sys
from pathlib import Path

import pytest
import yaml


class TestMigratePaths:
    def test_moves_file_from_old_to_new(self, tmp_path):
        """A file at the old checkout-relative path is moved to the new platform path."""
        from utils.system.migrate_paths import migrate_paths

        resources_dir = tmp_path / "checkout"
        resources_dir.mkdir()
        state_dir = tmp_path / "state"
        cache_dir = tmp_path / "cache"
        data_dir = tmp_path / "data"
        log_dir = tmp_path / "logs"

        # Old data at checkout-relative default location
        old_db = resources_dir / "ha_agent_state.db"
        old_db.write_text("old-db-content")

        config_path = resources_dir / "config.yaml"
        config_path.write_text(yaml.dump({}))  # no explicit db_path

        import os

        env = {
            "PUEO_STATE_DIR": str(state_dir),
            "PUEO_DATA_DIR": str(data_dir),
            "PUEO_CACHE_DIR": str(cache_dir),
            "PUEO_LOG_DIR": str(log_dir),
        }
        for k, v in env.items():
            os.environ[k] = v
        try:
            # Patch resources_dir so migrate_paths resolves old paths relative to tmp checkout
            import paths as _paths
            import unittest.mock

            fake_dirs = _paths.PueoDirectories(
                config_dir=tmp_path / "config",
                data_dir=data_dir,
                state_dir=state_dir,
                cache_dir=cache_dir,
                log_dir=log_dir,
                runtime_dir=tmp_path / "run",
                resources_dir=resources_dir,
            )
            with unittest.mock.patch(
                "utils.system.migrate_paths.get_dirs", return_value=fake_dirs
            ):
                migrate_paths(config_path, dry_run=False)
        finally:
            for k in env:
                os.environ.pop(k, None)

        new_db = state_dir / "ha_agent_state.db"
        assert new_db.exists(), "DB should have been moved to state_dir"
        assert new_db.read_text() == "old-db-content"
        assert not old_db.exists(), "Old DB should be gone after move"

        updated = yaml.safe_load(config_path.read_text())
        assert updated["agent"]["db_path"] == str(new_db)

    def test_dry_run_does_not_move(self, tmp_path, capsys):
        """Dry-run mode prints intent but does not move files."""
        from utils.system.migrate_paths import migrate_paths
        import unittest.mock
        import paths as _paths

        resources_dir = tmp_path / "checkout"
        resources_dir.mkdir()
        state_dir = tmp_path / "state"
        old_db = resources_dir / "ha_agent_state.db"
        old_db.write_text("do-not-move")
        config_path = resources_dir / "config.yaml"
        config_path.write_text(yaml.dump({}))

        fake_dirs = _paths.PueoDirectories(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=state_dir,
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            runtime_dir=tmp_path / "run",
            resources_dir=resources_dir,
        )
        with unittest.mock.patch(
            "utils.system.migrate_paths.get_dirs", return_value=fake_dirs
        ):
            migrate_paths(config_path, dry_run=True)

        assert old_db.exists(), "Dry run must not move the file"
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "ha_agent_state.db" in out

    def test_skips_when_both_exist(self, tmp_path, capsys):
        """When old and new both exist, warn and leave both in place."""
        from utils.system.migrate_paths import migrate_paths
        import unittest.mock
        import paths as _paths

        resources_dir = tmp_path / "checkout"
        resources_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        old_db = resources_dir / "ha_agent_state.db"
        old_db.write_text("old")
        new_db = state_dir / "ha_agent_state.db"
        new_db.write_text("new")

        config_path = resources_dir / "config.yaml"
        config_path.write_text(yaml.dump({}))

        fake_dirs = _paths.PueoDirectories(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=state_dir,
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            runtime_dir=tmp_path / "run",
            resources_dir=resources_dir,
        )
        with unittest.mock.patch(
            "utils.system.migrate_paths.get_dirs", return_value=fake_dirs
        ):
            migrate_paths(config_path, dry_run=False)

        # Both should still exist — user must resolve manually
        assert old_db.exists()
        assert new_db.read_text() == "new"
        out = capsys.readouterr().out
        assert "⚠" in out or "both" in out.lower()

    def test_no_old_data_updates_config_path(self, tmp_path):
        """When there is no old data, config.yaml is still updated to the new path."""
        from utils.system.migrate_paths import migrate_paths
        import unittest.mock
        import paths as _paths

        resources_dir = tmp_path / "checkout"
        resources_dir.mkdir()
        state_dir = tmp_path / "state"

        config_path = resources_dir / "config.yaml"
        config_path.write_text(yaml.dump({}))

        fake_dirs = _paths.PueoDirectories(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=state_dir,
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            runtime_dir=tmp_path / "run",
            resources_dir=resources_dir,
        )
        with unittest.mock.patch(
            "utils.system.migrate_paths.get_dirs", return_value=fake_dirs
        ):
            migrate_paths(config_path, dry_run=False)

        updated = yaml.safe_load(config_path.read_text()) or {}
        # db_path should point to the new location even though no file existed
        assert updated.get("agent", {}).get("db_path") == str(
            state_dir / "ha_agent_state.db"
        )

    def test_explicit_absolute_config_path_is_treated_as_old(self, tmp_path):
        """An explicit absolute path in config.yaml that differs from new is treated as old."""
        from utils.system.migrate_paths import migrate_paths
        import unittest.mock
        import paths as _paths

        resources_dir = tmp_path / "checkout"
        resources_dir.mkdir()
        old_db_dir = tmp_path / "somewhere_else"
        old_db_dir.mkdir()
        old_db = old_db_dir / "pueo.db"
        old_db.write_text("explicit-abs-old")

        state_dir = tmp_path / "state"
        config_path = resources_dir / "config.yaml"
        config_path.write_text(yaml.dump({"agent": {"db_path": str(old_db)}}))

        fake_dirs = _paths.PueoDirectories(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=state_dir,
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            runtime_dir=tmp_path / "run",
            resources_dir=resources_dir,
        )
        with unittest.mock.patch(
            "utils.system.migrate_paths.get_dirs", return_value=fake_dirs
        ):
            migrate_paths(config_path, dry_run=False)

        new_db = state_dir / "ha_agent_state.db"
        assert new_db.exists()
        assert new_db.read_text() == "explicit-abs-old"
        assert not old_db.exists()
