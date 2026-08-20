"""Archive HA host data to local Pueo storage before deletion.

Provides safe-copy helpers that preserve forensic artifacts (HA log, journal)
before disk-recovery steps zero or vacuum them on the HA host.

Public API:
    archive_ha_log          — SFTP-pull /config/home-assistant.log, gzip-compress locally
    archive_journal_dump    — SSH journalctl text dump, gzip-compress locally
    enforce_archive_retention — delete oldest archive files until under the size cap
"""

from __future__ import annotations

import gzip
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from interfaces import SSHClientProtocol
from utils.core.logging import get_logger

log = get_logger("archiver")

HA_LOG_REMOTE_PATH = "/config/home-assistant.log"


@dataclass
class ArchiveResult:
    path: Path
    original_bytes: int
    compressed_bytes: int
    timestamp: datetime


async def archive_ha_log(
    ssh_client: SSHClientProtocol,
    archive_dir: Path,
) -> Optional[ArchiveResult]:
    """SFTP-pull /config/home-assistant.log and gzip-compress it into archive_dir/ha_logs/.

    Returns None if the log is empty or the download fails. Never raises — callers should
    treat a None result as a non-fatal skip and proceed with truncation.
    """
    try:
        _, size_out, _ = await ssh_client.run(
            f"stat -c %s {HA_LOG_REMOTE_PATH} 2>/dev/null || echo 0",
            check=False,
        )
        original_bytes = int(size_out.strip() or "0")
        if original_bytes == 0:
            log.info("archiver_ha_log_empty", skipped=True)
            return None

        dest_dir = archive_dir / "ha_logs"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"ha-log-{ts}.log.gz"

        # Download to a temp file, then stream-compress to dest
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            await ssh_client.download_file(HA_LOG_REMOTE_PATH, tmp_path)
            with open(tmp_path, "rb") as f_in, gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        compressed_bytes = dest.stat().st_size
        log.info(
            "archiver_ha_log_saved",
            path=str(dest),
            original_bytes=original_bytes,
            compressed_bytes=compressed_bytes,
        )
        return ArchiveResult(
            path=dest,
            original_bytes=original_bytes,
            compressed_bytes=compressed_bytes,
            timestamp=datetime.now(),
        )

    except Exception as e:
        log.warning("archiver_ha_log_failed", error=str(e))
        return None


async def archive_journal_dump(
    ssh_client: SSHClientProtocol,
    archive_dir: Path,
    lines: int = 5000,
) -> Optional[ArchiveResult]:
    """SSH journalctl text dump and gzip-compress it into archive_dir/journal/.

    Returns None if the journal output is empty or the SSH call fails.
    """
    try:
        _, journal_text, _ = await ssh_client.run(
            f"journalctl -n {lines} --no-pager --output=short-precise 2>/dev/null || true",
            check=False,
        )
        if not journal_text.strip():
            log.info("archiver_journal_empty", skipped=True)
            return None

        dest_dir = archive_dir / "journal"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"journal-{ts}.txt.gz"

        encoded = journal_text.encode("utf-8", errors="replace")
        original_bytes = len(encoded)
        with gzip.open(dest, "wb") as f_out:
            f_out.write(encoded)

        compressed_bytes = dest.stat().st_size
        log.info(
            "archiver_journal_saved",
            path=str(dest),
            original_bytes=original_bytes,
            compressed_bytes=compressed_bytes,
            lines=lines,
        )
        return ArchiveResult(
            path=dest,
            original_bytes=original_bytes,
            compressed_bytes=compressed_bytes,
            timestamp=datetime.now(),
        )

    except Exception as e:
        log.warning("archiver_journal_failed", error=str(e))
        return None


def enforce_archive_retention(archive_dir: Path, max_bytes: int) -> int:
    """Delete the oldest archive files until total size is at or below max_bytes.

    Walks all files under archive_dir recursively, sorts by mtime (oldest first),
    and deletes until the total is within budget.

    Returns the number of bytes freed.
    """
    if not archive_dir.exists():
        return 0

    all_files = sorted(
        (f for f in archive_dir.rglob("*") if f.is_file()),
        key=lambda f: _safe_mtime(f),
    )

    total_bytes = sum(_safe_size(f) for f in all_files)
    freed = 0

    for f in all_files:
        if total_bytes <= max_bytes:
            break
        size = _safe_size(f)
        try:
            f.unlink()
            total_bytes -= size
            freed += size
            log.info("archiver_retention_deleted", path=str(f), size_bytes=size)
        except OSError as e:
            log.warning("archiver_retention_delete_failed", path=str(f), error=str(e))

    return freed


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
