"""Disk space recovery helpers for the HA host.

Safe functions (auto-executable on disk critical):
  truncate_ha_log     — zero out /config/home-assistant.log without deleting it
  vacuum_journal      — rotate + size-cap the systemd journal
  purge_recorder      — call recorder.purge via HA REST API

Approval-required functions (audit/report only; destruction happens via dashboard dispatch):
  audit_supervisor_tmp — list /mnt/data/supervisor/tmp/* sizes; caller decides to delete

Recovery sequence:
  run_safe_disk_recovery — runs all auto-safe steps and returns a summary for the approval card
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import config
from interfaces import HARestClientProtocol, SSHClientProtocol
from utils.archiver import (
    archive_ha_log,
    archive_journal_dump,
    enforce_archive_retention,
)
from utils.core.logging import get_logger

log = get_logger("disk_recovery")

# Action keys the dispatch table in run_safe_disk_recovery recognizes.
# The LLM investigation prompt lists these so the model uses matching keys.
DISK_AUTO_ACTION_KEYS: frozenset[str] = frozenset(
    {"truncate_ha_log", "vacuum_journal", "purge_recorder"}
)


@dataclass
class RecoveryAction:
    name: str
    bytes_freed: int
    message: str
    success: bool


@dataclass
class RecoverySummary:
    actions: list[RecoveryAction] = field(default_factory=list)
    # Set when run_safe_disk_recovery ran an LLM investigation before recovery.
    investigation_report: Optional[Any] = None  # InvestigationReport | None
    used_investigation: bool = False

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

    If ARCHIVE_HA_LOG_ENABLED, the log is SFTP-pulled and gzip-compressed to PUEO_ARCHIVE_DIR
    before truncation. Archive failure is non-fatal — truncation proceeds regardless.
    """
    name = "truncate_ha_log"
    try:
        if config.ARCHIVE_HA_LOG_ENABLED:
            archive_result = await archive_ha_log(
                ssh_client, Path(config.PUEO_ARCHIVE_DIR)
            )
            if archive_result:
                enforce_archive_retention(
                    Path(config.PUEO_ARCHIVE_DIR),
                    int(config.PUEO_ARCHIVE_MAX_GB * 1_000_000_000),
                )

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

    If ARCHIVE_JOURNAL_ENABLED, a text dump of recent journal entries is gzip-compressed to
    PUEO_ARCHIVE_DIR before the vacuum runs. Archive failure is non-fatal.
    """
    name = "vacuum_journal"
    try:
        if config.ARCHIVE_JOURNAL_ENABLED:
            archive_result = await archive_journal_dump(
                ssh_client, Path(config.PUEO_ARCHIVE_DIR)
            )
            if archive_result:
                enforce_archive_retention(
                    Path(config.PUEO_ARCHIVE_DIR),
                    int(config.PUEO_ARCHIVE_MAX_GB * 1_000_000_000),
                )

        # Check if journalctl is available (not present on HAOS SSH shells)
        _, which_out, _ = await ssh_client.run(
            "which journalctl 2>/dev/null || echo ''", check=False
        )
        if not which_out.strip():
            log.info("disk_recovery_journal_skipped", reason="journalctl_not_found")
            return RecoveryAction(
                name=name,
                bytes_freed=0,
                message="Journal vacuum skipped (journalctl not available on this host)",
                success=False,
            )

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
        # apply_filter is omitted: HA returns 400 when True but no recorder entity
        # filters are configured, which is the common case.
        # HA renamed the field from purge_keep_days → keep_days in 2026.x.
        await rest_client.call_service(
            "recorder",
            "purge",
            {"keep_days": keep_days, "repack": repack},
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
        try:
            import httpx as _httpx

            if isinstance(e, _httpx.HTTPStatusError):
                log.warning(
                    "disk_recovery_recorder_purge_failed",
                    status=e.response.status_code,
                    body=e.response.text[:500],
                )
            else:
                log.warning("disk_recovery_recorder_purge_failed", error=str(e))
        except Exception:  # nosec B110
            log.warning("disk_recovery_recorder_purge_failed", error=str(e))
        return RecoveryAction(name=name, bytes_freed=0, message=str(e), success=False)


@dataclass
class TmpAuditItem:
    path: str
    size_display: str
    size_bytes_approx: int


@dataclass
class OrphanedAddonDir:
    slug: str
    paths: list
    size_display: str
    size_bytes: int


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


async def scan_orphaned_addon_dirs(
    ssh_client: SSHClientProtocol,
) -> list:
    """Return OrphanedAddonDir entries for add-on data dirs left on disk after uninstall.

    Compares installed slugs (ha apps list --raw-json) against directories in
    /mnt/data/supervisor/addons/ and /addon_configs/. Returns [] on any error.
    """
    import json as _json

    try:
        _, addon_json, _ = await ssh_client.run("ha apps list --raw-json", check=False)
        try:
            _data = _json.loads(addon_json)
            installed_slugs = {
                a["slug"]
                for a in _data.get("data", {}).get("addons", [])
                if "slug" in a
            }
        except Exception:
            installed_slugs = set()

        _, addons_ls, _ = await ssh_client.run(
            "ls /mnt/data/supervisor/addons/ 2>/dev/null || true", check=False
        )
        _, configs_ls, _ = await ssh_client.run(
            "ls /addon_configs/ 2>/dev/null || true", check=False
        )

        addons_on_disk = {s for s in addons_ls.split() if s.strip()}
        configs_on_disk = {s for s in configs_ls.split() if s.strip()}
        orphaned_slugs = (addons_on_disk | configs_on_disk) - installed_slugs

        results = []
        for slug in sorted(orphaned_slugs):
            paths = []
            if slug in addons_on_disk:
                paths.append(f"/mnt/data/supervisor/addons/{slug}")
            if slug in configs_on_disk:
                paths.append(f"/addon_configs/{slug}")

            paths_arg = " ".join(paths)
            _, du_out, _ = await ssh_client.run(
                f"du -sh {paths_arg} 2>/dev/null | awk '{{sum += $1}} END {{print sum}}'",
                check=False,
            )
            # du -sh with multiple paths: sum the sizes. Try to get a combined display.
            _, du_display, _ = await ssh_client.run(
                f"du -sh {paths_arg} 2>/dev/null",
                check=False,
            )
            # Parse total size from display lines
            total_bytes = 0
            for line in du_display.strip().splitlines():
                parts = line.split(None, 1)
                if parts:
                    total_bytes += _parse_du_size(parts[0])
            size_disp = (
                du_display.strip().splitlines()[0].split(None, 1)[0]
                if du_display.strip()
                else "?"
            )
            if len(paths) > 1:
                size_disp = f"~{_bytes_to_human(total_bytes)}"

            results.append(
                OrphanedAddonDir(
                    slug=slug,
                    paths=paths,
                    size_display=size_disp,
                    size_bytes=total_bytes,
                )
            )
        return results
    except Exception as e:
        log.warning("scan_orphaned_addon_dirs_failed", error=str(e))
        return []


def _bytes_to_human(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} G"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} M"
    if n >= 1024:
        return f"{n / 1024:.1f} K"
    return f"{n} B"


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


def build_disk_conclusion(
    top_consumers: list,
    addon_persistent: list,
    auto_savings_mb: float,
    disk_free_after_gb: float,
    disk_critical_gb: float,
    hitl_options: list,
    backup_section_bytes: int,
    ha_config_section_bytes: int = 0,
) -> dict:
    """Compute a plain-English conclusion about whether approval options will resolve the disk issue.

    Uses a conservative heuristic to estimate max savings from available approval options:
      - offload_backups: backup section size (all backups freed)
      - recorder purge/repack: 20% of HA Config section (rough recorder-DB estimate)

    Returns a dict with text, hitl_sufficient, needed_mb, hitl_estimate_mb,
    top_consumers (up to 4), and addon_persistent (up to 4).
    """
    needed_mb = max(0.0, (disk_critical_gb - disk_free_after_gb) * 1024)

    option_keys = {opt.get("action_key", "") for opt in hitl_options}
    hitl_estimate_mb = 0.0
    if "offload_backups" in option_keys:
        hitl_estimate_mb += backup_section_bytes / (1024 * 1024)
    if any(k in option_keys for k in ("repack_recorder", "aggressive_purge_7d")):
        hitl_estimate_mb += ha_config_section_bytes / (1024 * 1024) * 0.20

    hitl_sufficient = (auto_savings_mb + hitl_estimate_mb) >= needed_mb

    culprit: Optional[dict] = None
    if addon_persistent:
        culprit = addon_persistent[0]
    elif top_consumers:
        culprit = top_consumers[0]

    if needed_mb <= 0:
        text = (
            f"Auto-recovery freed ~{auto_savings_mb:.0f} MB and disk is now above the "
            "critical threshold. Approving the options below will add extra headroom."
        )
    elif hitl_sufficient:
        text = (
            f"Auto-recovery freed ~{auto_savings_mb:.0f} MB. Disk still needs "
            f"~{needed_mb:.0f} MB more to clear the critical threshold. "
            f"The recovery options below (estimated ~{hitl_estimate_mb:.0f} MB) "
            "should be sufficient — approving them is recommended."
        )
    else:
        culprit_str = ""
        if culprit:
            culprit_str = f" The largest single consumer is {culprit['name']} ({culprit['size_human']})."
        text = (
            f"Auto-recovery freed ~{auto_savings_mb:.0f} MB, but disk still needs "
            f"~{needed_mb:.0f} MB more. The recovery options below can free at most "
            f"~{hitl_estimate_mb:.0f} MB — likely not enough to resolve this.{culprit_str} "
            "To actually fix this, consider removing or reducing a large add-on, "
            "or expanding storage."
        )

    return {
        "text": text,
        "hitl_sufficient": hitl_sufficient,
        "needed_mb": round(needed_mb, 1),
        "hitl_estimate_mb": round(hitl_estimate_mb, 1),
        "top_consumers": top_consumers[:4],
        "addon_persistent": addon_persistent[:4],
    }


async def run_safe_disk_recovery(
    ssh_client: SSHClientProtocol,
    rest_client: Optional[HARestClientProtocol],
    recorder_keep_days: int = 30,
    journal_max_mb: int = 200,
    llm_client: Optional[Any] = None,
    knowledge_store: Optional[Any] = None,
    initial_context: str = "",
) -> RecoverySummary:
    """Run disk recovery, optionally guided by an LLM investigation.

    When llm_client is provided, the function runs investigate_with_fallback before
    any SSH commands. If the investigation returns recognized auto_action keys, only
    those steps are executed (in the LLM-recommended order). If the investigation
    fails or returns no recognized keys, the fixed 3-step heuristic runs instead.

    Steps (all non-destructive of user data):
      1. Truncate HA log file
      2. Vacuum systemd journal
      3. Purge recorder history (keep last N days, no repack)

    rest_client may be None if HA_API_TOKEN is not configured; recorder purge is skipped.
    Returns a RecoverySummary with per-step outcomes and the investigation report (if run).
    """

    async def _dispatch(action_key: str) -> Optional[RecoveryAction]:
        if action_key == "truncate_ha_log":
            return await truncate_ha_log(ssh_client)
        if action_key == "vacuum_journal":
            return await vacuum_journal(ssh_client, max_mb=journal_max_mb)
        if action_key == "purge_recorder" and rest_client is not None:
            return await purge_recorder(
                rest_client, keep_days=recorder_keep_days, repack=False
            )
        return None

    # --- LLM-guided path ---
    if llm_client is not None:
        from utils.investigation_loop import investigate_with_fallback

        keys_doc = ", ".join(sorted(DISK_AUTO_ACTION_KEYS))
        inv_context = (
            f"{initial_context}\n\n"
            f"Available auto_action keys (use exactly): {keys_doc}."
        ).strip()

        report, is_fallback = await investigate_with_fallback(
            topic="HA disk space critically low",
            goal=(
                "Identify the root cause of the disk shortage. "
                "Recommend which auto-safe recovery steps to execute immediately, "
                "ranked by expected impact. Use only the provided action_key values."
            ),
            context=inv_context,
            llm_client=llm_client,
            ssh_client=ssh_client,
            knowledge_store=knowledge_store,
            timeout=60.0,
        )

        if not is_fallback and report is not None:
            summary = RecoverySummary(
                investigation_report=report,
                used_investigation=True,
            )
            executed: set[str] = set()
            for action in report.auto_actions:
                key = action.action_key
                if key in DISK_AUTO_ACTION_KEYS and key not in executed:
                    result = await _dispatch(key)
                    if result is not None:
                        summary.actions.append(result)
                        executed.add(key)

            if executed:
                log.info(
                    "disk_recovery_investigation_guided",
                    steps=list(executed),
                    confidence=report.confidence,
                )
                return summary

            # Investigation succeeded but returned no recognized keys — fall through.
            log.info(
                "disk_recovery_investigation_no_recognized_keys",
                auto_actions=[a.action_key for a in report.auto_actions],
            )
            # Keep the investigation report even when falling back to heuristic steps.
            summary.used_investigation = False
        else:
            summary = RecoverySummary()
    else:
        summary = RecoverySummary()

    # --- Heuristic fallback ---
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
