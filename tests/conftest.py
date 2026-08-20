"""Shared pytest fixtures for Pueo test suite."""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

# Ensure the project root (pueo/) is on sys.path so agent modules are importable
# when pytest is invoked from any working directory.
sys.path.insert(0, str(Path(__file__).parent.parent))

_DEFAULT_CONFIG_YAML = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"

_DEFAULT_COMMAND_RESULTS = {
    "ha backup new": (0, "Slug: test-slug-abc\n", ""),
    "ha core check": (0, "", ""),
    "ha core restart": (0, "", ""),
    "mkdir": (0, "", ""),
    "mv": (0, "", ""),
    "cp": (0, "", ""),
}


@pytest.fixture
def fake_ssh_client():
    from utils.ha.ssh_client import FakeSSHClient

    return FakeSSHClient(
        file_contents={"/config/configuration.yaml": _DEFAULT_CONFIG_YAML},
        command_results=_DEFAULT_COMMAND_RESULTS,
    )


@pytest.fixture
def fake_llm_client():
    from utils.llm.ollama_client import FakeLLMClient
    from agents.ha_agent_core import DiagnosticsReport

    report = DiagnosticsReport(
        is_valid=True,
        severity="NONE",
        identified_issues=[],
        recommended_fix_yaml=None,
    )
    return FakeLLMClient(report.model_dump_json())


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
    # Ensure config is in sys.modules so tests can safely call
    # importlib.reload(sys.modules["config"]) without a KeyError.
    if "config" not in sys.modules:
        import config  # noqa: F401
    yield cfg_path
    cfg_path.write_text("")  # reset to empty so reloaded modules get defaults
    _reload_all_modules()


@pytest.fixture(autouse=True)
def _patch_timeline_db(monkeypatch, tmp_path):
    """Redirect write_timeline_event to a per-test temp DB so tests never pollute the real DB."""
    import utils.core.timeline

    tmp_db = str(tmp_path / "test_timeline.db")
    with sqlite3.connect(tmp_db) as conn:
        conn.execute(
            "CREATE TABLE timeline_events "
            "(id INTEGER PRIMARY KEY, ts REAL, level TEXT, "
            "source TEXT, message TEXT, detail_json TEXT)"
        )
    monkeypatch.setattr(utils.core.timeline, "DB_PATH", tmp_db)


def _reload_all_modules():
    agent_modules = [
        "config",
        "agents.ha_agent_core",
        "agents.ha_agent_advanced",
        "agents.ha_agent_sandbox_engine",
        "agents.ha_log_monitor",
    ]
    for name in agent_modules:
        if name in sys.modules:
            try:
                importlib.reload(sys.modules[name])
            except Exception:
                pass
