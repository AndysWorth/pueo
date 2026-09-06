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
