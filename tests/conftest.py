"""Shared pytest fixtures for Pueo test suite."""

import importlib
import sys

import pytest
import yaml


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """
    Yields a writable Path for a temp config.yaml.
    After the test, reloads config and all agent modules so their
    module-level constants reset to the default state.

    Usage:
        def test_something(isolated_config):
            isolated_config.write_text(yaml.dump({...}))
            importlib.reload(sys.modules["config"])
            import config
            assert config.HA_HOST == "..."
    """
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setenv("PUEO_CONFIG", str(cfg_path))
    yield cfg_path
    _reload_all_modules()


def _reload_all_modules():
    agent_modules = [
        "config",
        "ha_agent_core",
        "ha_agent_advanced",
        "ha_agent_sandbox_engine",
        "ha_log_monitor",
    ]
    for name in agent_modules:
        if name in sys.modules:
            try:
                importlib.reload(sys.modules[name])
            except Exception:
                pass
