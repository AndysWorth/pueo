"""Tests for main.py module-level helpers."""

import os
from unittest.mock import MagicMock, patch

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


def test_write_pid_file_cleanup_removes_file(tmp_path):
    """atexit cleanup removes the PID file when our PID is still in it."""
    import atexit
    import main as m

    fake_dirs = _FakeDirs(tmp_path)
    pid_file = tmp_path / "pueo.pid"

    registered = []
    original_register = atexit.register

    def _capture(fn, *args, **kwargs):
        registered.append(fn)
        return original_register(fn, *args, **kwargs)

    with patch("atexit.register", side_effect=_capture):
        with patch.object(m._paths, "get_dirs", return_value=fake_dirs):
            m._write_pid_file()

    assert pid_file.exists()
    assert registered, "atexit.register should have been called"

    # Call the cleanup closure directly
    registered[0]()
    assert not pid_file.exists()


def test_write_pid_file_cleanup_ignores_foreign_pid(tmp_path):
    """atexit cleanup does not remove the file if another process wrote a different PID."""
    import atexit
    import main as m

    fake_dirs = _FakeDirs(tmp_path)
    pid_file = tmp_path / "pueo.pid"

    registered = []
    original_register = atexit.register

    def _capture(fn, *args, **kwargs):
        registered.append(fn)
        return original_register(fn, *args, **kwargs)

    with patch("atexit.register", side_effect=_capture):
        with patch.object(m._paths, "get_dirs", return_value=fake_dirs):
            m._write_pid_file()

    # Overwrite with a different PID (simulating another process)
    pid_file.write_text("99999")

    registered[0]()
    # File should remain — it's not our PID
    assert pid_file.exists()
    assert pid_file.read_text().strip() == "99999"
