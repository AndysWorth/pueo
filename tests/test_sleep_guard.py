"""Tests for utils/system/sleep_guard.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# host_sleep_seconds
# ---------------------------------------------------------------------------


class TestHostSleepSeconds:
    def _fn(self, wall_prev, mono_prev, wall_now, mono_now) -> float:
        from utils.system.sleep_guard import host_sleep_seconds

        return host_sleep_seconds(wall_prev, mono_prev, wall_now, mono_now)

    def test_no_drift_returns_zero(self):
        # wall and mono advance by the same amount — no sleep
        assert self._fn(100.0, 50.0, 200.0, 150.0) == pytest.approx(0.0)

    def test_sleep_detected(self):
        # wall advances 900 s, mono only 300 s — 600 s asleep
        result = self._fn(1000.0, 500.0, 1900.0, 800.0)
        assert result == pytest.approx(600.0)

    def test_negative_clamped_to_zero(self):
        # Negative difference due to clock jitter — must not return negative
        result = self._fn(100.0, 50.0, 101.0, 51.5)
        assert result == pytest.approx(0.0)

    def test_exact_threshold_boundary(self):
        from utils.system.sleep_guard import HOST_SLEEP_THRESHOLD_SECONDS

        # Exactly at threshold
        result = self._fn(0.0, 0.0, HOST_SLEEP_THRESHOLD_SECONDS + 10, 10.0)
        assert result == pytest.approx(HOST_SLEEP_THRESHOLD_SECONDS)


# ---------------------------------------------------------------------------
# SleepGuard — non-darwin (no-op)
# ---------------------------------------------------------------------------


class TestSleepGuardNonDarwin:
    def _make(self) -> Any:
        from utils.system.sleep_guard import SleepGuard

        return SleepGuard()

    def test_supported_false_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        from utils.system.sleep_guard import SleepGuard

        assert SleepGuard.supported() is False

    def test_start_noop_on_unsupported(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        guard = self._make()
        guard.start()  # must not raise and must not spawn anything
        assert not guard.is_active()

    def test_stop_noop_when_not_active(self):
        guard = self._make()
        guard.stop()  # must not raise


# ---------------------------------------------------------------------------
# SleepGuard — darwin (mocked caffeinate)
# ---------------------------------------------------------------------------


class TestSleepGuardDarwin:
    def _make(self) -> Any:
        from utils.system.sleep_guard import SleepGuard

        return SleepGuard()

    @pytest.fixture()
    def mock_popen(self, monkeypatch):
        proc = MagicMock()
        proc.pid = 99999
        proc.poll.return_value = None  # running
        popen_cls = MagicMock(return_value=proc)
        monkeypatch.setattr("utils.system.sleep_guard.subprocess.Popen", popen_cls)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            "utils.system.sleep_guard._CAFFEINATE", Path("/usr/bin/caffeinate")
        )
        return popen_cls, proc

    def test_start_spawns_caffeinate(self, mock_popen, monkeypatch):
        import os

        monkeypatch.setattr(
            "utils.system.sleep_guard.Path.exists",
            lambda self: True,
        )
        popen_cls, proc = mock_popen
        guard = self._make()
        guard.start()
        args = popen_cls.call_args[0][0]
        assert args[0] == "/usr/bin/caffeinate"
        assert args[1] == "-i"
        assert args[2] == "-w"
        assert args[3] == str(os.getpid())

    def test_start_idempotent(self, mock_popen, monkeypatch):
        monkeypatch.setattr("utils.system.sleep_guard.Path.exists", lambda self: True)
        popen_cls, proc = mock_popen
        guard = self._make()
        guard.start()
        guard.start()  # second call must not spawn another process
        assert popen_cls.call_count == 1

    def test_is_active_after_start(self, mock_popen, monkeypatch):
        monkeypatch.setattr("utils.system.sleep_guard.Path.exists", lambda self: True)
        _popen_cls, proc = mock_popen
        guard = self._make()
        guard.start()
        assert guard.is_active() is True

    def test_stop_terminates_process(self, mock_popen, monkeypatch):
        monkeypatch.setattr("utils.system.sleep_guard.Path.exists", lambda self: True)
        _popen_cls, proc = mock_popen
        guard = self._make()
        guard.start()
        guard.stop()
        proc.terminate.assert_called_once()
        assert guard.is_active() is False

    def test_stop_noop_when_not_active(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        guard = self._make()
        guard.stop()  # must not raise


# ---------------------------------------------------------------------------
# apply_sleep_policy
# ---------------------------------------------------------------------------


class TestApplySleepPolicy:
    def test_enabled_calls_start(self, monkeypatch):
        started = []
        stopped = []
        monkeypatch.setattr(
            "utils.system.sleep_guard._guard",
            type("G", (), {"start": lambda s: started.append(1), "stop": lambda s: stopped.append(1)})(),  # type: ignore[call-arg]
        )
        from utils.system.sleep_guard import apply_sleep_policy

        apply_sleep_policy(True)
        assert started == [1]
        assert stopped == []

    def test_disabled_calls_stop(self, monkeypatch):
        started = []
        stopped = []
        monkeypatch.setattr(
            "utils.system.sleep_guard._guard",
            type("G", (), {"start": lambda s: started.append(1), "stop": lambda s: stopped.append(1)})(),  # type: ignore[call-arg]
        )
        from utils.system.sleep_guard import apply_sleep_policy

        apply_sleep_policy(False)
        assert started == []
        assert stopped == [1]
