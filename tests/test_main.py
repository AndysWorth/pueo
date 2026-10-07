"""Tests for main.py module-level helpers."""

import os
from unittest.mock import patch

import pytest


class _FakeDirs:
    def __init__(self, state_dir):
        self.state_dir = state_dir


def test_write_pid_file_creates_file(tmp_path):
    import main as m

    fake_dirs = _FakeDirs(tmp_path)
    with patch.object(m._paths, "get_dirs", return_value=fake_dirs):
        m._write_pid_file()

    pid_file = tmp_path / "pueo.pid"
    assert pid_file.exists()
    assert pid_file.read_text().strip() == str(os.getpid())


def test_write_pid_file_creates_parent_dirs(tmp_path):
    import main as m

    nested = tmp_path / "a" / "b"
    fake_dirs = _FakeDirs(nested)
    with patch.object(m._paths, "get_dirs", return_value=fake_dirs):
        m._write_pid_file()

    assert (nested / "pueo.pid").read_text().strip() == str(os.getpid())


def test_write_pid_file_no_crash_on_write_error(tmp_path):
    import main as m

    fake_dirs = _FakeDirs(tmp_path)
    with patch.object(m._paths, "get_dirs", return_value=fake_dirs):
        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            m._write_pid_file()  # must not raise


def test_write_pid_file_does_not_register_atexit(tmp_path):
    """_write_pid_file must NOT register an atexit handler.

    bin/pueo is the sole authority on PID file lifecycle; atexit cleanup was
    causing the PID file to be deleted while the process was still running
    (e.g. daemon threads keeping the process alive after sys.exit was called
    from the SIGTERM handler).
    """
    import atexit
    import main as m

    fake_dirs = _FakeDirs(tmp_path)
    registered = []

    def _capture(fn, *args, **kwargs):
        registered.append(fn)

    with patch("atexit.register", side_effect=_capture):
        with patch.object(m._paths, "get_dirs", return_value=fake_dirs):
            m._write_pid_file()

    assert not registered, "_write_pid_file must not call atexit.register"


# ---------------------------------------------------------------------------
# _raise_fd_limit (#769)
# ---------------------------------------------------------------------------


class TestRaiseFdLimit:
    def _patch(self, monkeypatch, soft, hard, set_exc=None):
        import resource

        calls: list[tuple] = []

        def _set(which, limits):
            if set_exc is not None:
                raise set_exc
            calls.append(limits)

        monkeypatch.setattr(resource, "getrlimit", lambda which: (soft, hard))
        monkeypatch.setattr(resource, "setrlimit", _set)
        return calls

    def test_raises_soft_limit_to_target(self, monkeypatch):
        import main

        calls = self._patch(monkeypatch, 256, 10240)
        assert main._raise_fd_limit() == (256, main._FD_TARGET)
        assert calls == [(main._FD_TARGET, 10240)]

    def test_caps_at_hard_limit(self, monkeypatch):
        import main

        calls = self._patch(monkeypatch, 256, 1024)
        assert main._raise_fd_limit() == (256, 1024)
        assert calls == [(1024, 1024)]

    def test_unlimited_hard_uses_target(self, monkeypatch):
        import resource

        import main

        calls = self._patch(monkeypatch, 256, resource.RLIM_INFINITY)
        assert main._raise_fd_limit() == (256, main._FD_TARGET)
        assert calls == [(main._FD_TARGET, resource.RLIM_INFINITY)]

    def test_leaves_higher_limit_alone(self, monkeypatch):
        import main

        calls = self._patch(monkeypatch, 8192, 10240)
        assert main._raise_fd_limit() is None
        assert calls == []

    @pytest.mark.parametrize("exc", [ValueError("no"), OSError("no")])
    def test_survives_setrlimit_error(self, monkeypatch, exc):
        import main

        self._patch(monkeypatch, 256, 10240, set_exc=exc)
        assert main._raise_fd_limit() is None


def test_dashboard_limit_concurrency_constant():
    import main

    assert 0 < main.DASHBOARD_LIMIT_CONCURRENCY <= 256


# ---------------------------------------------------------------------------
# _is_update_wake_worthy
# ---------------------------------------------------------------------------


class TestIsUpdateWakeWorthy:
    def _fn(self, ev: dict) -> bool:
        import main

        return main._is_update_wake_worthy(ev)

    def test_no_old_state_is_worthy(self):
        ev = {"event_type": "state_changed", "data": {}}
        assert self._fn(ev)

    def test_state_value_change_is_worthy(self):
        ev = {
            "data": {
                "old_state": {"state": "off", "attributes": {}},
                "new_state": {"state": "on", "attributes": {}},
            }
        }
        assert self._fn(ev)

    def test_attribute_only_change_not_worthy(self):
        ev = {
            "data": {
                "old_state": {
                    "state": "on",
                    "attributes": {"in_progress": False, "latest_version": "2026.9.0"},
                },
                "new_state": {
                    "state": "on",
                    "attributes": {"in_progress": True, "latest_version": "2026.9.0"},
                },
            }
        }
        assert not self._fn(ev)

    def test_latest_version_change_is_worthy(self):
        ev = {
            "data": {
                "old_state": {
                    "state": "on",
                    "attributes": {"latest_version": "2026.9.0"},
                },
                "new_state": {
                    "state": "on",
                    "attributes": {"latest_version": "2026.10.0"},
                },
            }
        }
        assert self._fn(ev)


# ---------------------------------------------------------------------------
# _is_ai_agent_ha_automation_event
# ---------------------------------------------------------------------------


class TestIsAiAgentHaAutomationEvent:
    def _fn(self, ev: dict) -> bool:
        import main

        return main._is_ai_agent_ha_automation_event(ev)

    def test_automation_triggered_with_ai_entity(self):
        ev = {
            "event_type": "automation_triggered",
            "entity_id": "automation.ai_agent_auto_lights",
        }
        assert self._fn(ev)

    def test_automation_triggered_with_ai_entity_in_data(self):
        ev = {
            "event_type": "automation_triggered",
            "data": {"entity_id": "automation.ai_agent_auto_notify"},
        }
        assert self._fn(ev)

    def test_automation_triggered_non_ai_entity(self):
        ev = {
            "event_type": "automation_triggered",
            "entity_id": "automation.my_custom_automation",
        }
        assert not self._fn(ev)

    def test_state_changed_ai_automation(self):
        ev = {
            "event_type": "state_changed",
            "entity_id": "automation.ai_agent_auto_scene",
        }
        assert self._fn(ev)

    def test_state_changed_ai_automation_in_data(self):
        ev = {
            "event_type": "state_changed",
            "data": {"entity_id": "automation.ai_agent_auto_camera"},
        }
        assert self._fn(ev)

    def test_state_changed_regular_automation(self):
        ev = {
            "event_type": "state_changed",
            "entity_id": "automation.my_lights",
        }
        assert not self._fn(ev)

    def test_lovelace_updated_returns_false(self):
        ev = {"event_type": "lovelace_updated"}
        assert not self._fn(ev)

    def test_unrelated_event_returns_false(self):
        ev = {"event_type": "state_changed", "entity_id": "light.bedroom"}
        assert not self._fn(ev)
