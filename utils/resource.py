"""Disk and memory sensing for the HA host via 'ha host info' and /proc/meminfo."""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import yaml as _yaml

from interfaces import SSHClientProtocol
from utils.logging import get_logger

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
    """Polls HA disk/memory on a fixed interval; sends HITL alerts on first threshold breach."""

    def __init__(
        self,
        ssh_client: SSHClientProtocol,
        notifier,  # NotifierProtocol — not typed to avoid circular import
        interval_seconds: float,
        disk_warn_gb: float,
        disk_critical_gb: float,
        mem_warn_mb: float,
        rest_client=None,  # Optional[HARestClientProtocol] — not typed to avoid circular import
    ) -> None:
        self._ssh = ssh_client
        self._notifier = notifier
        self._interval = interval_seconds
        self._disk_warn_gb = disk_warn_gb
        self._disk_critical_gb = disk_critical_gb
        self._mem_warn_mb = mem_warn_mb
        self._rest_client = rest_client
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
                        auto_summary = await run_safe_disk_recovery(
                            ssh_client=self._ssh,
                            rest_client=self._rest_client,
                            recorder_keep_days=_cfg.DISK_RECOVERY_RECORDER_KEEP_DAYS,
                            journal_max_mb=_cfg.DISK_RECOVERY_JOURNAL_MAX_MB,
                        )
                        # Offload pending backups and enforce retention with force_critical
                        try:
                            import ha_agent_advanced as _adv

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

            # Only send a HITL card on the first breach or after a successful recovery
            # retry (cooldown elapsed). Subsequent polls within the cooldown are silent.
            if is_first_breach or cooldown_elapsed:
                import config as _cfg  # type: ignore[assignment]
                from utils.card_types import CARD_TYPE_DISK_RECOVERY

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

                await self._notifier.send(
                    subject="Pueo CRITICAL: HA disk almost full",
                    body="\n".join(body_lines),
                    payload={
                        "card_type": CARD_TYPE_DISK_RECOVERY,
                        "type": "resource_alert",
                        "severity": "CRITICAL",
                        "disk_free_gb": status.disk_free_gb,
                        "disk_free_after_gb": disk_free_after_gb,
                        "disk_total_gb": status.disk_total_gb,
                        "mem_available_mb": round(status.mem_available_mb),
                        "auto_actions_taken": auto_actions,
                        "ranked_hitl_options": ranked_hitl_options,
                    },
                )
                try:
                    from utils.timeline import write_timeline_event

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
        else:
            self._alerted.discard("disk_critical")
            if status.disk_warn:
                if "disk_warn" not in self._alerted:
                    log.warning(
                        "disk_warn",
                        disk_free_gb=status.disk_free_gb,
                        threshold_gb=self._disk_warn_gb,
                    )

                    # Proactive cleanup at WARN level — run safe recovery steps before
                    # disk slides further to CRITICAL.
                    warn_summary = None
                    try:
                        import config as _cfg
                        from utils.disk_recovery import run_safe_disk_recovery

                        if _cfg.DISK_RECOVERY_AUTO_ENABLED:
                            warn_summary = await run_safe_disk_recovery(
                                ssh_client=self._ssh,
                                rest_client=self._rest_client,
                                recorder_keep_days=_cfg.DISK_RECOVERY_RECORDER_KEEP_DAYS,
                                journal_max_mb=_cfg.DISK_RECOVERY_JOURNAL_MAX_MB,
                            )
                            try:
                                import ha_agent_advanced as _adv

                                await _adv.offload_pending_backups(ssh_client=self._ssh)
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

                    await self._notifier.send(
                        subject="Pueo WARNING: HA disk space low",
                        body=warn_body,
                        payload={
                            "type": "resource_alert",
                            "severity": "WARN",
                            "disk_free_gb": status.disk_free_gb,
                            "disk_total_gb": status.disk_total_gb,
                            "mem_available_mb": round(status.mem_available_mb),
                        },
                    )
                    try:
                        from utils.timeline import write_timeline_event

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
                        "type": "resource_alert",
                        "severity": "WARN",
                        "mem_available_mb": round(status.mem_available_mb),
                        "mem_total_mb": round(status.mem_total_mb),
                    },
                )
                self._alerted.add("mem_warn")
        else:
            self._alerted.discard("mem_warn")
