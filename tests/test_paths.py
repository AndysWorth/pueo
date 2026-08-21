"""Tests for paths.py — centralized path abstraction."""

import dataclasses
from pathlib import Path

import pytest

from paths import PueoDirectories, get_dirs


class TestPueoDirectories:
    def test_frozen(self):
        dirs = get_dirs()
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
            dirs.data_dir = Path("/tmp/new")  # type: ignore[misc]

    def test_resources_dir_is_repo_root(self):
        dirs = get_dirs()
        # paths.py lives at repo root; resources_dir must be the same directory
        assert (dirs.resources_dir / "paths.py").exists()
        assert (dirs.resources_dir / "config.py").exists()

    def test_create_all_makes_directories(self, tmp_path):
        dirs = PueoDirectories(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            runtime_dir=tmp_path / "run",
            resources_dir=Path(__file__).parent.parent,
        )
        dirs.create_all()
        for field in dataclasses.fields(dirs):
            if field.name == "resources_dir":
                continue
            assert getattr(dirs, field.name).exists(), f"{field.name} was not created"

    def test_create_all_idempotent(self, tmp_path):
        dirs = PueoDirectories(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            runtime_dir=tmp_path / "run",
            resources_dir=Path(__file__).parent.parent,
        )
        dirs.create_all()
        dirs.create_all()  # must not raise

    def test_str_includes_all_dirs(self):
        dirs = get_dirs()
        summary = str(dirs)
        for field in dataclasses.fields(dirs):
            assert field.name in summary


class TestGetDirs:
    def test_returns_pueo_directories(self):
        dirs = get_dirs()
        assert isinstance(dirs, PueoDirectories)

    def test_env_var_overrides_config_dir(self, tmp_path, monkeypatch):
        override = str(tmp_path / "my_config")
        monkeypatch.setenv("PUEO_CONFIG_DIR", override)
        dirs = get_dirs()
        assert dirs.config_dir == Path(override)

    def test_env_var_overrides_data_dir(self, tmp_path, monkeypatch):
        override = str(tmp_path / "my_data")
        monkeypatch.setenv("PUEO_DATA_DIR", override)
        dirs = get_dirs()
        assert dirs.data_dir == Path(override)

    def test_env_var_overrides_state_dir(self, tmp_path, monkeypatch):
        override = str(tmp_path / "my_state")
        monkeypatch.setenv("PUEO_STATE_DIR", override)
        dirs = get_dirs()
        assert dirs.state_dir == Path(override)

    def test_env_var_overrides_cache_dir(self, tmp_path, monkeypatch):
        override = str(tmp_path / "my_cache")
        monkeypatch.setenv("PUEO_CACHE_DIR", override)
        dirs = get_dirs()
        assert dirs.cache_dir == Path(override)

    def test_env_var_overrides_log_dir(self, tmp_path, monkeypatch):
        override = str(tmp_path / "my_logs")
        monkeypatch.setenv("PUEO_LOG_DIR", override)
        dirs = get_dirs()
        assert dirs.log_dir == Path(override)

    def test_env_var_overrides_runtime_dir(self, tmp_path, monkeypatch):
        override = str(tmp_path / "my_run")
        monkeypatch.setenv("PUEO_RUNTIME_DIR", override)
        dirs = get_dirs()
        assert dirs.runtime_dir == Path(override)

    def test_docker_env_vars_produce_docker_paths(self, monkeypatch):
        """Simulates the Docker ENV block in the Dockerfile."""
        monkeypatch.setenv("PUEO_CONFIG_DIR", "/config")
        monkeypatch.setenv("PUEO_DATA_DIR", "/data")
        monkeypatch.setenv("PUEO_STATE_DIR", "/state")
        monkeypatch.setenv("PUEO_CACHE_DIR", "/cache")
        monkeypatch.setenv("PUEO_LOG_DIR", "/logs")
        dirs = get_dirs()
        assert dirs.config_dir == Path("/config")
        assert dirs.data_dir == Path("/data")
        assert dirs.state_dir == Path("/state")
        assert dirs.cache_dir == Path("/cache")
        assert dirs.log_dir == Path("/logs")

    def test_platform_defaults_are_absolute(self):
        dirs = get_dirs()
        for field in dataclasses.fields(dirs):
            path = getattr(dirs, field.name)
            assert path.is_absolute(), f"{field.name} is not absolute: {path}"

    def test_resources_dir_not_overridable_by_env(self, tmp_path, monkeypatch):
        """resources_dir is always the repo root, never an env var."""
        monkeypatch.setenv("PUEO_RESOURCES_DIR", str(tmp_path))
        dirs = get_dirs()
        # resources_dir should still be the repo root (where paths.py lives)
        assert (dirs.resources_dir / "paths.py").exists()


class TestPackageInstallable:
    def test_package_metadata_available(self):
        """Package is installed (editable or otherwise) and metadata is accessible."""
        from importlib.metadata import metadata, PackageNotFoundError

        try:
            meta = metadata("pueo")
        except PackageNotFoundError:
            pytest.skip("pueo package not installed; run 'pip install -e .'")
        assert meta["Name"] == "pueo"
        assert meta["Version"] is not None

    def test_entry_point_registered(self):
        """The 'pueo' console script entry point is declared."""
        from importlib.metadata import entry_points, PackageNotFoundError

        try:
            eps = entry_points(group="console_scripts")
        except Exception:
            pytest.skip("entry_points() not available")
        names = {ep.name for ep in eps}
        assert "pueo" in names, f"'pueo' entry point not found in {names}"

    def test_key_packages_importable(self):
        """Top-level modules and sub-packages that ship with the package are importable."""
        import config  # noqa: F401
        import interfaces  # noqa: F401
        import paths  # noqa: F401
        import agents  # noqa: F401
        import utils  # noqa: F401
