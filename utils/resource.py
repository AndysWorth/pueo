"""Disk and memory sensing for the HA host via 'ha host info' and /proc/meminfo."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import yaml as _yaml

from interfaces import SSHClientProtocol
from utils.core.logging import get_logger

log = get_logger("resource")


class DiskCriticalError(Exception):
    """Raised when HA disk free space is below the critical threshold, blocking backup creation."""


@dataclass
class ResourceStatus:
    disk_free_gb: float
    disk_total_gb: float
    disk_used_gb: float
    mem_available_mb: float
    mem_total_mb: float
    disk_warn: bool
    disk_critical: bool
    mem_warn: bool


def _parse_host_info(output: str) -> tuple[float, float, float]:
    """Extract disk_free, disk_total, disk_used from 'ha host info' YAML output."""
    data = _yaml.safe_load(output)
    return float(data["disk_free"]), float(data["disk_total"]), float(data["disk_used"])


def _parse_meminfo(output: str) -> tuple[float, float]:
    """Parse MemAvailable and MemTotal from /proc/meminfo, returning values in MB."""
    mem_available_kb = 0
    mem_total_kb = 0
    for line in output.splitlines():
        if line.startswith("MemAvailable:"):
            mem_available_kb = int(line.split()[1])
        elif line.startswith("MemTotal:"):
            mem_total_kb = int(line.split()[1])
    return mem_available_kb / 1024.0, mem_total_kb / 1024.0


async def poll_host_resources(
    ssh_client: SSHClientProtocol,
    disk_warn_gb: float,
    disk_critical_gb: float,
    mem_warn_mb: float,
) -> ResourceStatus:
    """Fetch disk and memory state from the HA host via SSH."""
    _, host_info_out, _ = await ssh_client.run("ha host info", check=False)
    disk_free, disk_total, disk_used = _parse_host_info(host_info_out)

    _, meminfo_out, _ = await ssh_client.run("cat /proc/meminfo", check=False)
    mem_available_mb, mem_total_mb = _parse_meminfo(meminfo_out)

    return ResourceStatus(
        disk_free_gb=disk_free,
        disk_total_gb=disk_total,
        disk_used_gb=disk_used,
        mem_available_mb=mem_available_mb,
        mem_total_mb=mem_total_mb,
        disk_warn=disk_free < disk_warn_gb,
        disk_critical=disk_free < disk_critical_gb,
        mem_warn=mem_available_mb < mem_warn_mb,
    )


# Cached last-known resource state — updated by ResourcePoller after each successful poll.
# execute_remote_backup() reads this to block when disk is critically low.
_last_resource_status: Optional[ResourceStatus] = None


def update_resource_status(status: ResourceStatus) -> None:
    """Store the latest poll result so execute_remote_backup() can check it without an extra SSH call."""
    global _last_resource_status
    _last_resource_status = status


def get_resource_status() -> Optional[ResourceStatus]:
    """Return the last cached resource poll result, or None if not yet polled."""
    return _last_resource_status


def check_disk_not_critical(disk_critical_gb: float) -> None:
    """Raise DiskCriticalError if the cached resource status shows disk below the critical threshold."""
    if _last_resource_status is not None and _last_resource_status.disk_critical:
        raise DiskCriticalError(
            f"HA disk free ({_last_resource_status.disk_free_gb:.1f} GB) is below critical "
            f"threshold ({disk_critical_gb} GB) — backup creation blocked."
        )


# Minimum time between automated recovery attempts when disk stays CRITICAL.
_RECOVERY_COOLDOWN_SECONDS = 1800


class ResourcePoller:
    """Polls HA disk/memory on a fixed interval; sends approval alerts on first threshold breach."""

    def __init__(
        self,
        ssh_client: SSHClientProtocol,
        notifier,  # NotifierProtocol — not typed to avoid circular import
        interval_seconds: float,
        disk_warn_gb: float,
        disk_critical_gb: float,
        mem_warn_mb: float,
        rest_client=None,  # Optional[HARestClientProtocol] — not typed to avoid circular import
        llm_client=None,  # Optional[LLMClientProtocol] — not typed to avoid circular import
        knowledge_store=None,  # Optional[KnowledgeStoreClientProtocol]
        db_path: str = "",
    ) -> None:
        self._ssh = ssh_client
        self._notifier = notifier
        self._interval = interval_seconds
        self._disk_warn_gb = disk_warn_gb
        self._disk_critical_gb = disk_critical_gb
        self._mem_warn_mb = mem_warn_mb
        self._rest_client = rest_client
        self._llm_client = llm_client
        self._knowledge_store = knowledge_store
        self._db_path = db_path
        self._alerted: set[str] = set()
        self._last_recovery_at: float = 0.0

    async def run(self) -> None:
        """Poll indefinitely — start via asyncio.create_task()."""
        while True:
            try:
                status = await poll_host_resources(
                    self._ssh,
                    self._disk_warn_gb,
                    self._disk_critical_gb,
                    self._mem_warn_mb,
                )
                update_resource_status(status)
                try:
                    from utils.supervisor import publish_event

                    publish_event(
                        {
                            "event_type": "resource",
                            "disk_free_gb": status.disk_free_gb,
                            "disk_used_pct": (
                                round(
                                    100 * status.disk_used_gb / status.disk_total_gb, 1
                                )
                                if status.disk_total_gb
                                else 0
                            ),
                            "mem_free_gb": round(status.mem_available_mb / 1024, 2),
                            "disk_critical": status.disk_critical,
                            "disk_warn": status.disk_warn,
                        }
                    )
                except Exception:  # nosec B110 — SSE publish is best-effort
                    pass
                log.info(
                    "resource_poll",
                    disk_free_gb=status.disk_free_gb,
                    mem_available_mb=round(status.mem_available_mb),
                    disk_warn=status.disk_warn,
                    disk_critical=status.disk_critical,
                    mem_warn=status.mem_warn,
                )
                await self._check_and_alert(status)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("resource_poll_failed", error=str(e))
            await asyncio.sleep(self._interval)

    async def _check_and_alert(self, status: ResourceStatus) -> None:
        """Send alerts for new threshold breaches; suppress duplicates until the condition clears."""
        if status.disk_critical:
            log.critical(
                "disk_critical",
                disk_free_gb=status.disk_free_gb,
                threshold_gb=self._disk_critical_gb,
            )

            # Run auto-safe recovery when first breaching CRITICAL, then retry if
            # disk stays critical beyond the cooldown window.
            cooldown_elapsed = (
                time.monotonic() - self._last_recovery_at
            ) > _RECOVERY_COOLDOWN_SECONDS
            is_first_breach = "disk_critical" not in self._alerted

            auto_summary = None
            disk_free_after_gb: Optional[float] = None
            if is_first_breach or cooldown_elapsed:
                try:
                    import config as _cfg
                    from utils.disk_recovery import run_safe_disk_recovery

                    if _cfg.DISK_RECOVERY_AUTO_ENABLED:
                        _inv_ctx = (
                            f"Disk free: {status.disk_free_gb:.2f} GB"
                            f" (total: {status.disk_total_gb:.2f} GB)."
                            f" Critical threshold: {self._disk_critical_gb} GB."
                        )
                        auto_summary = await run_safe_disk_recovery(
                            ssh_client=self._ssh,
                            rest_client=self._rest_client,
                            recorder_keep_days=_cfg.DISK_RECOVERY_RECORDER_KEEP_DAYS,
                            journal_max_mb=_cfg.DISK_RECOVERY_JOURNAL_MAX_MB,
                            llm_client=self._llm_client,
                            knowledge_store=self._knowledge_store,
                            initial_context=_inv_ctx,
                        )
                        # Offload pending backups and enforce retention with force_critical
                        try:
                            from agents import ha_agent_advanced as _adv

                            await _adv.offload_pending_backups(ssh_client=self._ssh)
                            await _adv.enforce_ha_retention(
                                ssh_client=self._ssh,
                                force_critical=True,
                            )
                        except Exception:  # nosec B110
                            pass

                        # Re-poll to find out whether recovery actually freed enough space.
                        try:
                            post = await poll_host_resources(
                                self._ssh,
                                self._disk_warn_gb,
                                self._disk_critical_gb,
                                self._mem_warn_mb,
                            )
                            disk_free_after_gb = post.disk_free_gb
                            update_resource_status(post)
                        except Exception:  # nosec B110
                            pass

                    self._last_recovery_at = time.monotonic()
                except Exception as _e:  # nosec B110
                    log.warning("disk_recovery_auto_failed", error=str(_e))

            # Only send an approval card on the first breach or after a successful recovery
            # retry (cooldown elapsed). Subsequent polls within the cooldown are silent.
            if is_first_breach or cooldown_elapsed:
                import sqlite3 as _sqlite3

                import config as _cfg  # type: ignore[assignment]
                from utils.card_types import CARD_TYPE_DISK_RECOVERY
                from utils.hitl_tracker import (
                    mark_card_sent,
                    should_send_card,
                )

                _db = self._db_path or _cfg.DB_PATH
                with _sqlite3.connect(_db) as _sup_conn:
                    # Cooldown-elapsed retries bypass the pending-state guard so the
                    # poller can re-send while disk stays critical; explicit user
                    # suppression (rejection cooldown, known issues) still blocks.
                    if not should_send_card(
                        _sup_conn,
                        "resource:disk_critical",
                        skip_pending_check=cooldown_elapsed and not is_first_breach,
                    ):
                        self._alerted.add("disk_critical")
                        if is_first_breach or cooldown_elapsed:
                            await self._maybe_send_migrate_card()
                        return

                auto_actions = []
                if auto_summary:
                    for act in auto_summary.actions:
                        auto_actions.append(
                            {
                                "name": act.name,
                                "success": act.success,
                                "message": act.message,
                                "bytes_freed": act.bytes_freed,
                            }
                        )

                ranked_hitl_options = [
                    {
                        "name": "Repack recorder database",
                        "description": (
                            "Compact the HA recorder SQLite file (home-assistant_v2.db). "
                            "Frees physical space previously occupied by deleted history rows. "
                            "Requires extra free space (~2.5× DB size) and takes several minutes."
                        ),
                        "estimated_savings": "up to several GB",
                        "risk": "LOW",
                        "action_key": "repack_recorder",
                    },
                    {
                        "name": "Aggressive recorder purge (7 days)",
                        "description": (
                            "Reduce history retention to 7 days instead of "
                            f"{_cfg.DISK_RECOVERY_RECORDER_KEEP_DAYS} days."
                        ),
                        "estimated_savings": "hundreds of MB to several GB",
                        "risk": "LOW",
                        "action_key": "aggressive_purge_7d",
                    },
                    {
                        "name": "Clear supervisor temp files",
                        "description": (
                            "Remove leftover temp files from failed HA backup attempts "
                            "in /mnt/data/supervisor/tmp/. Can be very large (up to 60 GB) "
                            "if backups repeatedly failed mid-write."
                        ),
                        "estimated_savings": "0 to 60 GB",
                        "risk": "LOW",
                        "action_key": "clean_tmp",
                    },
                    {
                        "name": "Offload and delete old backups from HA",
                        "description": (
                            "Offload any remaining HA-only backups to Pueo, then delete "
                            "confirmed-local backups from HA (keeping the newest one)."
                        ),
                        "estimated_savings": "backup size × count",
                        "risk": "MEDIUM",
                        "action_key": "offload_backups",
                    },
                ]

                # Scan for orphaned add-on data directories and add as approval option.
                orphaned_addons: list = []
                try:
                    from utils.disk_recovery import scan_orphaned_addon_dirs

                    orphaned_addons = await scan_orphaned_addon_dirs(self._ssh)
                    if orphaned_addons:
                        total_bytes = sum(o.size_bytes for o in orphaned_addons)
                        slugs = ", ".join(o.slug for o in orphaned_addons)
                        ranked_hitl_options.append(
                            {
                                "name": "Remove orphaned app data directories",
                                "description": (
                                    f"Delete leftover data directories for uninstalled apps: "
                                    f"{slugs}. These exist in /mnt/data/supervisor/addons/ "
                                    f"and/or /addon_configs/ but are no longer installed."
                                ),
                                "estimated_savings": f"~{total_bytes // (1024 * 1024)} MB",
                                "risk": "LOW",
                                "action_key": "cleanup_orphaned_addon_dirs",
                                "orphaned_slugs": [  # type: ignore[dict-item]
                                    {
                                        "slug": o.slug,
                                        "paths": o.paths,
                                        "size_display": o.size_display,
                                    }
                                    for o in orphaned_addons
                                ],
                            }
                        )
                except Exception as _oe:  # nosec B110 — best-effort
                    log.warning("orphaned_addon_scan_failed", error=str(_oe))

                # Best-effort deep disk analysis — determines whether the approval options
                # are likely to resolve the issue and names the primary culprit if not.
                disk_analysis: dict = {}
                try:
                    from utils.disk_recovery import build_disk_conclusion
                    from utils.disk_usage import (
                        fetch_addon_persistent_sizes,
                        fetch_top_consumers_flat,
                        get_disk_breakdown,
                    )

                    breakdown = get_disk_breakdown()
                    if breakdown:
                        addon_name_map: dict = {}
                        if len(breakdown.sections) > 2:
                            for _item in breakdown.sections[2].items:
                                _slug = _item.path.rstrip("/").rsplit("/", 1)[-1]
                                addon_name_map[_slug] = _item.name

                        top_consumers = fetch_top_consumers_flat(breakdown)
                        addon_persistent: list = []
                        try:
                            addon_persistent = await fetch_addon_persistent_sizes(
                                self._ssh, addon_name_map
                            )
                        except Exception:  # nosec B110 — best-effort
                            pass

                        _dfa = (
                            disk_free_after_gb
                            if disk_free_after_gb is not None
                            else status.disk_free_gb
                        )
                        ha_config_bytes = (
                            breakdown.sections[0].total_bytes
                            if breakdown.sections
                            else 0
                        )
                        backup_bytes = (
                            breakdown.sections[1].total_bytes
                            if len(breakdown.sections) > 1
                            else 0
                        )
                        disk_analysis = build_disk_conclusion(
                            top_consumers=top_consumers,
                            addon_persistent=addon_persistent,
                            auto_savings_mb=(
                                auto_summary.total_freed_mb if auto_summary else 0.0
                            ),
                            disk_free_after_gb=_dfa,
                            disk_critical_gb=self._disk_critical_gb,
                            hitl_options=ranked_hitl_options,
                            backup_section_bytes=backup_bytes,
                            ha_config_section_bytes=ha_config_bytes,
                        )
                except Exception as _e:  # nosec B110 — analysis is best-effort
                    log.warning("disk_analysis_failed", error=str(_e))

                # Use the investigation report embedded in auto_summary (if the LLM-guided
                # path ran inside run_safe_disk_recovery). Falls back to heuristic text.
                if (
                    auto_summary is not None
                    and auto_summary.investigation_report is not None
                ):
                    disk_analysis = _serialize_analysis(
                        auto_summary.investigation_report, disk_analysis, False
                    )
                else:
                    disk_analysis = {**disk_analysis, "source": "heuristic"}

                body_lines = [
                    f"Disk free: {status.disk_free_gb:.1f} GB — below critical threshold "
                    f"{self._disk_critical_gb} GB. Backup creation is blocked.",
                ]
                if auto_summary and auto_summary.succeeded:
                    freed_mb = auto_summary.total_freed_mb
                    body_lines.append(
                        f"\nAuto-recovery ran {len(auto_summary.succeeded)} safe step(s)"
                        + (f", freeing ~{freed_mb:.0f} MB." if freed_mb > 0 else ".")
                    )
                    for act in auto_summary.succeeded:
                        body_lines.append(f"  ✓ {act.message}")
                if auto_summary and auto_summary.failed:
                    for act in auto_summary.failed:
                        body_lines.append(f"  ✗ {act.name}: {act.message}")
                if disk_free_after_gb is not None:
                    still_critical = disk_free_after_gb < self._disk_critical_gb
                    body_lines.append(
                        f"\nDisk after auto-recovery: {disk_free_after_gb:.1f} GB"
                        + (
                            " — still CRITICAL, manual action required."
                            if still_critical
                            else " — recovered."
                        )
                    )

                from utils.hitl_tracker import stable_nid as _stable_nid

                _disk_crit_key = "resource:disk_critical"
                # Mark sent before writing file — avoids the "not sent" DB state if
                # Pueo restarts between the file write and the mark_card_sent call.
                with _sqlite3.connect(_db) as _sup_conn2:
                    mark_card_sent(
                        _sup_conn2,
                        _disk_crit_key,
                        CARD_TYPE_DISK_RECOVERY,
                        f"Disk CRITICAL: {status.disk_free_gb:.1f} GB free",
                    )
                await self._notifier.send(
                    subject="Pueo CRITICAL: HA disk almost full",
                    body="\n".join(body_lines),
                    payload={
                        "notification_id": _stable_nid(_disk_crit_key),
                        "card_type": CARD_TYPE_DISK_RECOVERY,
                        "suppression_key": _disk_crit_key,
                        "type": "resource_alert",
                        "severity": "CRITICAL",
                        "disk_free_gb": status.disk_free_gb,
                        "disk_free_after_gb": disk_free_after_gb,
                        "disk_total_gb": status.disk_total_gb,
                        "mem_available_mb": round(status.mem_available_mb),
                        "auto_actions_taken": auto_actions,
                        "ranked_hitl_options": ranked_hitl_options,
                        "disk_analysis": disk_analysis,
                    },
                )
                try:
                    from utils.core.timeline import write_timeline_event

                    write_timeline_event(
                        "CRITICAL",
                        "resource",
                        f"Disk CRITICAL: {status.disk_free_gb:.1f} GB free"
                        f" — below {self._disk_critical_gb} GB threshold",
                        {
                            "disk_free_gb": status.disk_free_gb,
                            "disk_free_after_gb": disk_free_after_gb,
                            "disk_total_gb": status.disk_total_gb,
                        },
                    )
                except Exception:  # nosec B110
                    pass
                self._alerted.add("disk_critical")

                # If NetAlertX is running as an HA add-on and the user has a
                # Docker host configured, suggest migrating it off HA.
                if is_first_breach or cooldown_elapsed:
                    await self._maybe_send_migrate_card()
        else:
            if "disk_critical" in self._alerted:
                import sqlite3 as _sqlite3

                import config as _cfg  # type: ignore[assignment]
                from utils.hitl_tracker import mark_card_resolved

                with _sqlite3.connect(self._db_path or _cfg.DB_PATH) as _rc:
                    mark_card_resolved(_rc, "resource:disk_critical")
                try:
                    from utils.core.timeline import write_timeline_event

                    write_timeline_event(
                        "INFO",
                        "resource",
                        f"Disk critical alert cleared — free space now above threshold"
                        f" ({status.disk_free_gb:.1f} GB free)",
                        {
                            "disk_free_gb": status.disk_free_gb,
                            "threshold_gb": self._disk_critical_gb,
                            "resolution": "cleared",
                        },
                    )
                except Exception:  # nosec B110
                    pass
            self._alerted.discard("disk_critical")
            if status.disk_warn:
                if "disk_warn" not in self._alerted:
                    import sqlite3 as _sqlite3

                    import config as _cfg  # type: ignore[assignment]
                    from utils.hitl_tracker import (
                        mark_card_sent,
                        should_send_card,
                    )

                    _db_w = self._db_path or _cfg.DB_PATH
                    with _sqlite3.connect(_db_w) as _sup_w:
                        _send_warn = should_send_card(_sup_w, "resource:disk_warn")
                    if not _send_warn:
                        self._alerted.add("disk_warn")
                    else:
                        log.warning(
                            "disk_warn",
                            disk_free_gb=status.disk_free_gb,
                            threshold_gb=self._disk_warn_gb,
                        )

                        # Proactive cleanup at WARN level — run safe recovery steps before
                        # disk slides further to CRITICAL.
                        warn_summary = None
                        try:
                            from utils.disk_recovery import run_safe_disk_recovery

                            if _cfg.DISK_RECOVERY_AUTO_ENABLED:
                                warn_summary = await run_safe_disk_recovery(
                                    ssh_client=self._ssh,
                                    rest_client=self._rest_client,
                                    recorder_keep_days=_cfg.DISK_RECOVERY_RECORDER_KEEP_DAYS,
                                    journal_max_mb=_cfg.DISK_RECOVERY_JOURNAL_MAX_MB,
                                )
                                try:
                                    from agents import ha_agent_advanced as _adv

                                    await _adv.offload_pending_backups(
                                        ssh_client=self._ssh
                                    )
                                    await _adv.enforce_ha_retention(
                                        ssh_client=self._ssh,
                                        force_critical=False,
                                    )
                                except Exception:  # nosec B110
                                    pass
                                self._last_recovery_at = time.monotonic()
                        except Exception as _e:  # nosec B110
                            log.warning("disk_recovery_warn_failed", error=str(_e))

                        warn_body = (
                            f"Disk free: {status.disk_free_gb:.1f} GB — below warning threshold "
                            f"{self._disk_warn_gb} GB."
                        )
                        if warn_summary and warn_summary.succeeded:
                            freed_mb = warn_summary.total_freed_mb
                            warn_body += (
                                f"\nProactive recovery freed ~{freed_mb:.0f} MB."
                                if freed_mb > 0
                                else "\nProactive recovery ran; space freed will reflect in the next poll."
                            )

                        from utils.hitl_tracker import stable_nid as _stable_nid_w

                        _disk_warn_key = "resource:disk_warn"
                        with _sqlite3.connect(_db_w) as _sup_w2:
                            mark_card_sent(
                                _sup_w2,
                                _disk_warn_key,
                                "resource_alert",
                                f"Disk WARN: {status.disk_free_gb:.1f} GB free",
                            )
                        await self._notifier.send(
                            subject="Pueo WARNING: HA disk space low",
                            body=warn_body,
                            payload={
                                "notification_id": _stable_nid_w(_disk_warn_key),
                                "suppression_key": _disk_warn_key,
                                "type": "resource_alert",
                                "severity": "WARN",
                                "disk_free_gb": status.disk_free_gb,
                                "disk_total_gb": status.disk_total_gb,
                                "mem_available_mb": round(status.mem_available_mb),
                            },
                        )
                        try:
                            from utils.core.timeline import write_timeline_event

                            write_timeline_event(
                                "WARN",
                                "resource",
                                f"Disk WARN: {status.disk_free_gb:.1f} GB free"
                                f" — below {self._disk_warn_gb} GB threshold",
                                {"disk_free_gb": status.disk_free_gb},
                            )
                        except Exception:  # nosec B110
                            pass
                        self._alerted.add("disk_warn")
            else:
                if "disk_warn" in self._alerted:
                    import sqlite3 as _sqlite3

                    import config as _cfg  # type: ignore[assignment]
                    from utils.hitl_tracker import mark_card_resolved

                    with _sqlite3.connect(self._db_path or _cfg.DB_PATH) as _rw:
                        mark_card_resolved(_rw, "resource:disk_warn")
                    try:
                        from utils.core.timeline import write_timeline_event

                        write_timeline_event(
                            "INFO",
                            "resource",
                            f"Disk warning cleared — free space now above threshold"
                            f" ({status.disk_free_gb:.1f} GB free)",
                            {
                                "disk_free_gb": status.disk_free_gb,
                                "threshold_gb": self._disk_warn_gb,
                                "resolution": "cleared",
                            },
                        )
                    except Exception:  # nosec B110
                        pass
                self._alerted.discard("disk_warn")

        if status.mem_warn:
            if "mem_warn" not in self._alerted:
                log.warning(
                    "mem_warn",
                    mem_available_mb=round(status.mem_available_mb),
                    threshold_mb=self._mem_warn_mb,
                )
                await self._notifier.send(
                    subject="Pueo WARNING: HA memory low",
                    body=(
                        f"Memory available: {status.mem_available_mb:.0f} MB — below warning "
                        f"threshold {self._mem_warn_mb} MB."
                    ),
                    payload={
                        "suppression_key": "resource:mem_warn",
                        "type": "resource_alert",
                        "severity": "WARN",
                        "mem_available_mb": round(status.mem_available_mb),
                        "mem_total_mb": round(status.mem_total_mb),
                    },
                )
                self._alerted.add("mem_warn")
        else:
            self._alerted.discard("mem_warn")

    async def _maybe_send_migrate_card(self) -> None:
        """Send CARD_TYPE_NETALERTX_MIGRATE if NAX is on HA and disk is CRITICAL.

        Suppressed when:
        - deploy_target != "ha" (already migrated or Docker target)
        - NAX is not FULLY_OPERATIONAL on HA
        - migrate card already sent (tracked in _alerted)
        """
        if "netalertx_migrate" in self._alerted:
            return
        try:
            import config as _cfg
            from netalertx.installer import get_install_state
            from utils.card_types import CARD_TYPE_NETALERTX_MIGRATE

            deploy_target = getattr(_cfg, "NETALERTX_DEPLOY_TARGET", "ha")
            if deploy_target != "ha":
                return
            nax_state = get_install_state(_cfg.DB_PATH)
            if nax_state != "FULLY_OPERATIONAL":
                return

            await self._notifier.send(
                subject="Pueo: Consider migrating NetAlertX off HA to free disk space",
                body=(
                    "HA disk is CRITICAL and NetAlertX Full Access is running as an HA add-on. "
                    "Moving NetAlertX to a separate Docker host would reclaim significant disk "
                    "space on HA. Approve to begin the migration sequence."
                ),
                payload={
                    "card_type": CARD_TYPE_NETALERTX_MIGRATE,
                    "severity": "WARNING",
                    "type": "netalertx_migrate",
                    "deploy_target": deploy_target,
                    "nax_state": nax_state,
                    "docker_host_configured": bool(
                        getattr(_cfg, "NETALERTX_DOCKER_HOST", "")
                    ),
                },
            )
            self._alerted.add("netalertx_migrate")
            log.info(
                "netalertx_migrate_card_sent",
                nax_state=nax_state,
                deploy_target=deploy_target,
            )
        except Exception as exc:  # nosec B110 — best-effort advisory card
            log.warning("netalertx_migrate_card_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Helpers for disk investigation + serialization
# ---------------------------------------------------------------------------


def _build_disk_investigation_context(
    status: ResourceStatus,
    disk_free_after_gb: Optional[float],
    auto_summary: Any,  # RecoverySummary | None
    disk_critical_gb: float,
    heuristic: dict,
) -> str:
    """Format current disk state as a context string for the investigation agent."""
    lines = [
        f"Disk free: {status.disk_free_gb:.2f} GB (total: {status.disk_total_gb:.2f} GB)",
        f"Critical threshold: {disk_critical_gb} GB",
    ]
    if disk_free_after_gb is not None:
        still = disk_free_after_gb < disk_critical_gb
        lines.append(
            f"After auto-recovery: {disk_free_after_gb:.2f} GB"
            f" ({'still CRITICAL' if still else 'recovered'})"
        )
    if auto_summary:
        succeeded = [a.message for a in auto_summary.actions if a.success]
        if succeeded:
            lines.append("Auto-recovery steps taken: " + "; ".join(succeeded))
    if heuristic.get("top_consumers"):
        top = ", ".join(
            f"{c['name']} ({c['size_human']})" for c in heuristic["top_consumers"][:4]
        )
        lines.append(f"Top disk consumers: {top}")
    if heuristic.get("addon_persistent"):
        addons = ", ".join(
            f"{a['name']} ({a['size_human']})"
            for a in heuristic["addon_persistent"][:4]
        )
        lines.append(f"Add-on data sizes: {addons}")
    if heuristic.get("needed_mb", 0) > 0:
        lines.append(
            f"Space still needed: {heuristic['needed_mb']:.0f} MB above critical threshold"
        )
    return "\n".join(lines)


def _serialize_analysis(
    report: Any,  # Optional[InvestigationReport]
    heuristic: dict,
    is_fallback: bool,
) -> dict:
    """Merge investigation result (if available) with heuristic context data."""
    if is_fallback or report is None:
        return {**heuristic, "source": "heuristic"}
    return {
        "source": "investigation",
        "text": report.summary,
        "root_causes": report.root_causes,
        "manual_only": report.manual_only,
        "hitl_actions": [
            {
                "name": a.name,
                "description": a.description,
                "estimated_impact": a.estimated_impact,
                "risk_level": a.risk_level,
            }
            for a in report.hitl_actions
        ],
        "knowledge_sources": report.knowledge_sources,
        "confidence": report.confidence,
        "top_consumers": heuristic.get("top_consumers", []),
        "addon_persistent": heuristic.get("addon_persistent", []),
        "hitl_sufficient": len(report.manual_only) == 0,
        "needed_mb": heuristic.get("needed_mb", 0),
    }
