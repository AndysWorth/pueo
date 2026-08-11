"""Measure Pueo's own local storage footprint.

All Pueo data lives on the Mac (not on HA). This module walks the local directories
and file paths that Pueo manages and returns a per-category byte count. The result is
used by the Disk dashboard tab to show how much of the host machine Pueo is consuming.

Nothing in this module makes SSH connections or network calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import config
from utils.logging import get_logger

log = get_logger("pueo_storage")


@dataclass
class PueoFootprint:
    backups_bytes: int  # SFTP-offloaded HA backup .tar files
    archives_bytes: int  # Archived HA logs and journal dumps
    chromadb_bytes: int  # ChromaDB vector store
    cache_bytes: int  # RAG scraper caches (release notes, HACS, HA docs)
    db_bytes: int  # ha_agent_state.db SQLite file
    log_bytes: int  # pueo.log structured log file
    hitl_bytes: int  # HITL JSON cards and sentinel files
    total_bytes: int


def measure_pueo_footprint() -> PueoFootprint:
    """Walk all Pueo-managed local paths and return per-category byte counts.

    Emits a warning log if total exceeds PUEO_LOCAL_MAX_GB.
    """
    backups = _dir_size(Path(config.BACKUP_LOCAL_DIR))
    archives = _dir_size(Path(config.PUEO_ARCHIVE_DIR))
    chromadb = _dir_size(Path(config.CHROMADB_PATH))
    cache = (
        _dir_size(Path(config.RAG_HACS_CACHE_DIR))
        + _dir_size(Path(config.RAG_HA_DOCS_CACHE_DIR))
        + _dir_size(Path(config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR))
    )
    db = _file_size(Path(config.DB_PATH))
    log_file = _file_size(Path(config.LOG_FILE))
    hitl = _dir_size(Path(config.NOTIFY_WATCH_DIR))

    total = backups + archives + chromadb + cache + db + log_file + hitl

    limit_bytes = config.PUEO_LOCAL_MAX_GB * 1_000_000_000
    if total > limit_bytes:
        log.warning(
            "pueo_local_footprint_exceeded",
            total_gb=round(total / 1_000_000_000, 2),
            limit_gb=config.PUEO_LOCAL_MAX_GB,
        )

    return PueoFootprint(
        backups_bytes=backups,
        archives_bytes=archives,
        chromadb_bytes=chromadb,
        cache_bytes=cache,
        db_bytes=db,
        log_bytes=log_file,
        hitl_bytes=hitl,
        total_bytes=total,
    )


def _dir_size(path: Path) -> int:
    """Return total bytes of all files under path. Returns 0 if path does not exist."""
    if not path.exists():
        return 0
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
