#!/usr/bin/env python3
"""Tests for utils/archiver.py — archive helpers and retention enforcement."""

import asyncio
import gzip
import time
from pathlib import Path

import pytest

from utils.ha.ssh_client import FakeSSHClient


class TestArchiveHaLog:
    def test_creates_gzip_in_ha_logs_subdir(self, tmp_path):
        fake_ssh = FakeSSHClient(
            command_results={"stat -c %s": (0, "1048576", "")},
            download_contents={
                "/config/home-assistant.log": b"log line 1\nlog line 2\n"
            },
        )
        from utils.disk.archiver import archive_ha_log

        result = asyncio.run(archive_ha_log(fake_ssh, tmp_path))

        assert result is not None
        assert result.path.suffix == ".gz"
        assert result.path.parent.name == "ha_logs"
        assert result.original_bytes == 1048576
        assert result.compressed_bytes > 0
        assert result.path.exists()
        # Verify the gzip file is valid and contains the right content
        with gzip.open(result.path, "rb") as f:
            content = f.read()
        assert b"log line 1" in content

    def test_returns_none_when_log_empty(self, tmp_path):
        fake_ssh = FakeSSHClient(
            command_results={"stat -c %s": (0, "0", "")},
        )
        from utils.disk.archiver import archive_ha_log

        result = asyncio.run(archive_ha_log(fake_ssh, tmp_path))

        assert result is None

    def test_ssh_failure_returns_none(self, tmp_path):
        fake_ssh = FakeSSHClient(
            command_results={"stat -c %s": (0, "1048576", "")},
            download_error=RuntimeError("SSH connection refused"),
        )
        from utils.disk.archiver import archive_ha_log

        # Should not raise — archive failure is non-fatal
        result = asyncio.run(archive_ha_log(fake_ssh, tmp_path))

        assert result is None

    def test_archive_dir_created_if_missing(self, tmp_path):
        archive_dir = tmp_path / "archives"
        assert not archive_dir.exists()

        fake_ssh = FakeSSHClient(
            command_results={"stat -c %s": (0, "512", "")},
            download_contents={"/config/home-assistant.log": b"hello"},
        )
        from utils.disk.archiver import archive_ha_log

        result = asyncio.run(archive_ha_log(fake_ssh, archive_dir))

        assert result is not None
        assert (archive_dir / "ha_logs").exists()


class TestArchiveJournalDump:
    def test_creates_gzip_in_journal_subdir(self, tmp_path):
        journal_text = "Aug 11 10:00:00 homeassistant systemd[1]: Started\n" * 100
        fake_ssh = FakeSSHClient(
            command_results={"journalctl": (0, journal_text, "")},
        )
        from utils.disk.archiver import archive_journal_dump

        result = asyncio.run(archive_journal_dump(fake_ssh, tmp_path))

        assert result is not None
        assert result.path.suffix == ".gz"
        assert result.path.parent.name == "journal"
        assert result.original_bytes > 0
        assert result.compressed_bytes > 0
        with gzip.open(result.path, "rb") as f:
            content = f.read()
        assert b"systemd" in content

    def test_returns_none_when_journal_empty(self, tmp_path):
        fake_ssh = FakeSSHClient(
            command_results={"journalctl": (0, "   ", "")},
        )
        from utils.disk.archiver import archive_journal_dump

        result = asyncio.run(archive_journal_dump(fake_ssh, tmp_path))

        assert result is None

    def test_ssh_failure_returns_none(self, tmp_path):
        fake_ssh = FakeSSHClient(
            command_results={"journalctl": (1, "", "journalctl: error")},
        )
        from utils.disk.archiver import archive_journal_dump

        # Empty/error output → None (non-fatal)
        result = asyncio.run(archive_journal_dump(fake_ssh, tmp_path))

        assert result is None


class TestEnforceArchiveRetention:
    def _make_archive_dir(self, base: Path) -> Path:
        """Return a clean subdirectory to use as the archive root (avoids pytest metadata)."""
        d = base / "archives"
        d.mkdir()
        return d

    def _make_file(self, path: Path, size_bytes: int, mtime_offset: float = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size_bytes)
        if mtime_offset:
            t = time.time() + mtime_offset
            import os

            os.utime(path, (t, t))

    def test_no_op_when_under_limit(self, tmp_path):
        d = self._make_archive_dir(tmp_path)
        self._make_file(d / "a.log.gz", 100)
        self._make_file(d / "b.log.gz", 100)

        from utils.disk.archiver import enforce_archive_retention

        freed = enforce_archive_retention(d, max_bytes=10_000)

        assert freed == 0
        assert (d / "a.log.gz").exists()
        assert (d / "b.log.gz").exists()

    def test_deletes_oldest_first(self, tmp_path):
        d = self._make_archive_dir(tmp_path)
        # Create files with explicitly different mtimes
        self._make_file(d / "old.log.gz", 200, mtime_offset=-100)
        self._make_file(d / "mid.log.gz", 200, mtime_offset=-50)
        self._make_file(d / "new.log.gz", 200, mtime_offset=1)  # future = newest

        from utils.disk.archiver import enforce_archive_retention

        # Budget = 400 bytes, total = 600 → must delete at least the oldest
        enforce_archive_retention(d, max_bytes=400)

        assert not (d / "old.log.gz").exists(), "oldest file should be deleted"
        assert (d / "new.log.gz").exists(), "newest file should be kept"

    def test_deletes_multiple_files_to_reach_budget(self, tmp_path):
        d = self._make_archive_dir(tmp_path)
        self._make_file(d / "a.log.gz", 300, mtime_offset=-200)
        self._make_file(d / "b.log.gz", 300, mtime_offset=-100)
        self._make_file(d / "c.log.gz", 300, mtime_offset=1)

        from utils.disk.archiver import enforce_archive_retention

        # Budget = 300 bytes, total = 900 → two oldest must go to get under budget
        enforce_archive_retention(d, max_bytes=300)

        assert not (d / "a.log.gz").exists(), "oldest file should be deleted"
        assert not (d / "b.log.gz").exists(), "second-oldest should be deleted"
        assert (d / "c.log.gz").exists(), "newest file should be kept"

    def test_returns_zero_for_nonexistent_dir(self, tmp_path):
        from utils.disk.archiver import enforce_archive_retention

        freed = enforce_archive_retention(tmp_path / "no_such_dir", max_bytes=1000)

        assert freed == 0

    def test_walks_subdirectories(self, tmp_path):
        d = self._make_archive_dir(tmp_path)
        subdir = d / "ha_logs"
        self._make_file(subdir / "old.log.gz", 500, mtime_offset=-100)
        self._make_file(subdir / "new.log.gz", 500, mtime_offset=1)

        from utils.disk.archiver import enforce_archive_retention

        # Budget = 500 bytes, total = 1000 → must delete the oldest
        enforce_archive_retention(d, max_bytes=500)

        assert not (subdir / "old.log.gz").exists()
        assert (subdir / "new.log.gz").exists()


class TestMeasurePueoFootprint:
    def test_sums_directories_correctly(self, tmp_path, isolated_config, monkeypatch):
        import importlib
        import sys
        import yaml

        # Point all config paths to temp dirs we control
        backup_dir = tmp_path / "backups"
        archive_dir = tmp_path / "archives"
        chroma_dir = tmp_path / "chromadb"
        cache_dir = tmp_path / "cache"
        hitl_dir = tmp_path / "hitl"
        db_file = tmp_path / "state.db"
        log_file = tmp_path / "pueo.log"

        backup_dir.mkdir()
        archive_dir.mkdir()
        chroma_dir.mkdir()
        cache_dir.mkdir()
        hitl_dir.mkdir()
        db_file.write_bytes(b"x" * 1000)
        log_file.write_bytes(b"x" * 2000)
        (backup_dir / "backup.tar").write_bytes(b"x" * 5000)
        (archive_dir / "log.gz").write_bytes(b"x" * 3000)

        isolated_config.write_text(
            yaml.dump(
                {
                    "agent": {
                        "backup_local_dir": str(backup_dir),
                        "pueo_archive_dir": str(archive_dir),
                        "chromadb_path": str(chroma_dir),
                        "rag_hacs_cache_dir": str(cache_dir),
                        "rag_ha_docs_cache_dir": str(cache_dir),
                        "update_release_notes_cache_dir": str(cache_dir),
                        "db_path": str(db_file),
                        "log_file": str(log_file),
                        "notify_watch_dir": str(hitl_dir),
                        "pueo_local_max_gb": 100.0,
                        "pueo_archive_max_gb": 2.0,
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])

        # Reload pueo_storage so it picks up the new config
        if "utils.disk.pueo_storage" in sys.modules:
            importlib.reload(sys.modules["utils.disk.pueo_storage"])
        from utils.disk.pueo_storage import measure_pueo_footprint

        fp = measure_pueo_footprint()

        assert fp.backups_bytes == 5000
        assert fp.archives_bytes == 3000
        assert fp.db_bytes == 1000
        assert fp.log_bytes == 2000
        assert (
            fp.total_bytes
            == fp.backups_bytes
            + fp.archives_bytes
            + fp.chromadb_bytes
            + fp.cache_bytes
            + fp.db_bytes
            + fp.log_bytes
            + fp.hitl_bytes
        )

    def test_missing_dirs_count_as_zero(self, tmp_path, isolated_config, monkeypatch):
        import importlib
        import sys
        import yaml

        # All paths point to non-existent dirs
        isolated_config.write_text(
            yaml.dump(
                {
                    "agent": {
                        "backup_local_dir": str(tmp_path / "no_backups"),
                        "pueo_archive_dir": str(tmp_path / "no_archives"),
                        "chromadb_path": str(tmp_path / "no_chroma"),
                        "rag_hacs_cache_dir": str(tmp_path / "no_cache"),
                        "rag_ha_docs_cache_dir": str(tmp_path / "no_cache2"),
                        "update_release_notes_cache_dir": str(tmp_path / "no_cache3"),
                        "db_path": str(tmp_path / "no_db.db"),
                        "log_file": str(tmp_path / "no_log.log"),
                        "notify_watch_dir": str(tmp_path / "no_hitl"),
                        "pueo_local_max_gb": 100.0,
                        "pueo_archive_max_gb": 2.0,
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        if "utils.disk.pueo_storage" in sys.modules:
            importlib.reload(sys.modules["utils.disk.pueo_storage"])
        from utils.disk.pueo_storage import measure_pueo_footprint

        fp = measure_pueo_footprint()

        assert fp.total_bytes == 0
        assert fp.backups_bytes == 0
        assert fp.archives_bytes == 0
