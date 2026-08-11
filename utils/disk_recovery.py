"""Disk space recovery helpers for the HA host.

Safe functions (auto-executable on disk critical):
  truncate_ha_log     — zero out /config/home-assistant.log without deleting it
  vacuum_journal      — rotate + size-cap the systemd journal
  purge_recorder      — call recorder.purge via HA REST API

HITL-required functions (audit/report only; destruction happens via dashboard dispatch):
  audit_supervisor_tmp — list /mnt/data/supervisor/tmp/* sizes; caller decides to delete

Recovery sequence:
  run_safe_disk_recovery — runs all auto-safe steps and returns a summary for the HITL card
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from interfaces import HARestClientProtocol, SSHClientProtocol
from utils.logging import get_logger

log = get_logger("disk_recovery")


@dataclass
class RecoveryAction:
    name: str
    bytes_freed: int
    message: str
    success: bool


@dataclass
class RecoverySummary:
    actions: list[RecoveryAction] = field(default_factory=list)

    @property
    def total_freed_mb(self) -> float:
        return sum(a.bytes_freed for a in self.actions if a.success) / (1024 * 1024)

    @property
    def succeeded(self) -> list[RecoveryAction]:
        return [a for a in self.actions if a.success]

    @property
    def failed(self) -> list[RecoveryAction]:
        return [a for a in self.actions if not a.success]


async def truncate_ha_log(
    ssh_client: SSHClientProtocol,
) -> RecoveryAction:
    """Zero out /config/home-assistant.log without removing it.

    HA keeps the file descriptor open, so truncation (not deletion) is the correct approach
    — deletion leaves the inode allocated until HA restarts and the space is not freed.
    """
    name = "truncate_ha_log"
    try:
        # Get current size before truncating
        _, size_out, _ = await ssh_client.run(
            "stat -c %s /config/home-assistant.log 2>/dev/null || echo 0",
            check=False,
        )
        bytes_before = int(size_out.strip() or "0")

        await ssh_client.run("truncate -s 0 /config/home-assistant.log", check=True)
        log.info("disk_recovery_log_truncated", bytes_freed=bytes_before)
        return RecoveryAction(
            name=name,
            bytes_freed=bytes_before,
            message=f"Truncated HA log ({bytes_before / (1024*1024):.1f} MB freed)",
            success=True,
        )
    except Exception as e:
        log.warning("disk_recovery_log_truncate_failed", error=str(e))
        return RecoveryAction(name=name, bytes_freed=0, message=str(e), success=False)


async def vacuum_journal(
    ssh_client: SSHClientProtocol,
    max_mb: int = 200,
) -> RecoveryAction:
    """Rotate and size-cap the systemd journal.

    journalctl --rotate marks current journals as archived so --vacuum-size can remove them.
    Without --rotate, vacuum only removes already-archived files and may free nothing.
    """
    name = "vacuum_journal"
    try:
        _, before_out, _ = await ssh_client.run(
            "journalctl --disk-usage 2>/dev/null | grep -oP '[\\d.]+ [KMG]?B' | tail -1",
            check=False,
        )
        await ssh_client.run("journalctl --rotate", check=False)
        await asyncio.sleep(0.5)
        _, vacuum_out, _ = await ssh_client.run(
            f"journalctl --vacuum-size={max_mb}M 2>&1",
            check=False,
        )
        log.info(
            "disk_recovery_journal_vacuumed", max_mb=max_mb, output=vacuum_out.strip()
        )
        return RecoveryAction(
            name=name,
            bytes_freed=0,  # journalctl doesn't report freed bytes precisely
            message=f"Journal vacuumed to ≤{max_mb} MB target",
            success=True,
        )
    except Exception as e:
        log.warning("disk_recovery_journal_vacuum_failed", error=str(e))
        return RecoveryAction(name=name, bytes_freed=0, message=str(e), success=False)


async def purge_recorder(
    rest_client: HARestClientProtocol,
    keep_days: int = 30,
    repack: bool = False,
) -> RecoveryAction:
    """Call the HA recorder.purge service to trim history.

    repack=False (default): frees logical space inside SQLite quickly without requiring
    extra free space. repack=True compacts the file physically but needs ~2.5x the DB
    size available — only use when enough space already exists.
    """
    name = "purge_recorder"
    try:
        await rest_client.call_service(
            "recorder",
            "purge",
            {"purge_keep_days": keep_days, "repack": repack, "apply_filter": True},
        )
        action = "purge+repack" if repack else "purge"
        log.info(
            "disk_recovery_recorder_purged",
            keep_days=keep_days,
            repack=repack,
        )
        return RecoveryAction(
            name=name,
            bytes_freed=0,  # space freed asynchronously by HA; not immediately measurable
            message=f"Recorder {action} triggered (keep_days={keep_days})",
            success=True,
        )
    except Exception as e:
        log.warning("disk_recovery_recorder_purge_failed", error=str(e))
        return RecoveryAction(name=name, bytes_freed=0, message=str(e), success=False)


@dataclass
class TmpAuditItem:
    path: str
    size_display: str
    size_bytes_approx: int


async def audit_supervisor_tmp(
    ssh_client: SSHClientProtocol,
) -> list[TmpAuditItem]:
    """List items in /mnt/data/supervisor/tmp/ with their sizes.

    Returns an empty list if the directory is empty or inaccessible.
    Deletion is NOT performed here — the caller (dashboard handler) is responsible.
    """
    try:
        _, out, _ = await ssh_client.run(
            "du -sh /mnt/data/supervisor/tmp/* 2>/dev/null || true",
            check=False,
        )
        items = []
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            size_str, path = parts[0], parts[1].strip()
            # Convert display size to approximate bytes
            size_bytes = _parse_du_size(size_str)
            items.append(
                TmpAuditItem(
                    path=path, size_display=size_str, size_bytes_approx=size_bytes
                )
            )
        return items
    except Exception as e:
        log.warning("disk_recovery_tmp_audit_failed", error=str(e))
        return []


def _parse_du_size(size_str: str) -> int:
    """Convert du -sh output (e.g. '1.2G', '500M', '4.0K') to approximate bytes."""
    s = size_str.strip().upper()
    multipliers = {"G": 1024**3, "M": 1024**2, "K": 1024, "B": 1}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(s)
    except ValueError:
        return 0


async def run_safe_disk_recovery(
    ssh_client: SSHClientProtocol,
    rest_client: Optional[HARestClientProtocol],
    recorder_keep_days: int = 30,
    journal_max_mb: int = 200,
) -> RecoverySummary:
    """Run all auto-safe disk recovery steps in sequence.

    Steps (all non-destructive of user data):
      1. Truncate HA log file
      2. Vacuum systemd journal
      3. Purge recorder history (keep last N days, no repack)

    rest_client may be None if HA_API_TOKEN is not configured; recorder purge is skipped.
    Returns a RecoverySummary with per-step outcomes for the HITL card.
    """
    summary = RecoverySummary()

    log_action = await truncate_ha_log(ssh_client)
    summary.actions.append(log_action)

    journal_action = await vacuum_journal(ssh_client, max_mb=journal_max_mb)
    summary.actions.append(journal_action)

    if rest_client is not None:
        recorder_action = await purge_recorder(
            rest_client, keep_days=recorder_keep_days, repack=False
        )
        summary.actions.append(recorder_action)
    else:
        log.info("disk_recovery_recorder_skipped", reason="no_rest_client")

    return summary
