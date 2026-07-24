"""Disk and memory sensing for the HA host via 'ha host info' and /proc/meminfo."""

import asyncio
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


def check_disk_not_critical(disk_critical_gb: float) -> None:
    """Raise DiskCriticalError if the cached resource status shows disk below the critical threshold."""
    if _last_resource_status is not None and _last_resource_status.disk_critical:
        raise DiskCriticalError(
            f"HA disk free ({_last_resource_status.disk_free_gb:.1f} GB) is below critical "
            f"threshold ({disk_critical_gb} GB) — backup creation blocked."
        )


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
    ) -> None:
        self._ssh = ssh_client
        self._notifier = notifier
        self._interval = interval_seconds
        self._disk_warn_gb = disk_warn_gb
        self._disk_critical_gb = disk_critical_gb
        self._mem_warn_mb = mem_warn_mb
        self._alerted: set[str] = set()

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
            if "disk_critical" not in self._alerted:
                log.critical(
                    "disk_critical",
                    disk_free_gb=status.disk_free_gb,
                    threshold_gb=self._disk_critical_gb,
                )
                await self._notifier.send(
                    subject="Pueo CRITICAL: HA disk almost full",
                    body=(
                        f"Disk free: {status.disk_free_gb:.1f} GB — below critical threshold "
                        f"{self._disk_critical_gb} GB. Backup creation is blocked."
                    ),
                    payload={
                        "type": "resource_alert",
                        "severity": "CRITICAL",
                        "disk_free_gb": status.disk_free_gb,
                        "disk_total_gb": status.disk_total_gb,
                        "mem_available_mb": round(status.mem_available_mb),
                    },
                )
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
                    await self._notifier.send(
                        subject="Pueo WARNING: HA disk space low",
                        body=(
                            f"Disk free: {status.disk_free_gb:.1f} GB — below warning threshold "
                            f"{self._disk_warn_gb} GB."
                        ),
                        payload={
                            "type": "resource_alert",
                            "severity": "WARN",
                            "disk_free_gb": status.disk_free_gb,
                            "disk_total_gb": status.disk_total_gb,
                            "mem_available_mb": round(status.mem_available_mb),
                        },
                    )
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
