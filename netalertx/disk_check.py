"""Shared disk-space guard for NetAlertX installers.

Used by both the HA add-on installer (installer.py) and the Docker installer
(docker_installer.py) to abort early when the target host has insufficient free
space to complete installation safely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol


class DiskSpaceTooLowError(Exception):
    """Raised when the target host has insufficient disk space."""

    def __init__(self, available_gb: float, min_gb: float, path: str = "") -> None:
        self.available_gb = available_gb
        self.min_gb = min_gb
        self.path = path
        super().__init__(
            f"Insufficient disk space at {path!r}: "
            f"{available_gb:.1f} GB available, {min_gb:.1f} GB required"
        )


async def check_target_disk_space(
    ssh_client: "SSHClientProtocol",
    path: str,
    min_gb: float,
) -> float:
    """Check available disk space on the SSH host at *path*.

    Returns available GB. Raises DiskSpaceTooLowError if below min_gb.
    Uses ``df -k`` (universally supported on GNU/Linux, BusyBox/Alpine, and macOS);
    falls back to root filesystem when *path* does not exist yet.
    """
    ec, stdout, _ = await ssh_client.run(f"df -k {path} 2>/dev/null || df -k /")
    if ec != 0 or not stdout.strip():
        raise RuntimeError(f"df command failed on {path!r}")
    for line in stdout.splitlines():
        if line.startswith("Filesystem"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            avail_str = parts[3]
            try:
                available_gb = float(avail_str) / (1024 * 1024)  # KB → GB
                if available_gb < min_gb:
                    raise DiskSpaceTooLowError(available_gb, min_gb, path)
                return available_gb
            except ValueError:
                continue
    raise RuntimeError(f"Could not parse df output for {path!r}")
