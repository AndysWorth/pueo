"""Probe the HA app-config mount point.

HA renamed /addon_configs → /app_configs in HA 2026.x (Add-ons → Apps).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol


async def resolve_app_config_root(ssh_client: "SSHClientProtocol") -> str:
    """Return the live app-config mount point, e.g. '/app_configs'.

    Checks /app_configs first (HA 2026.x+), falls back to /addon_configs.
    Returns '' if neither directory exists.
    """
    _, stdout, _ = await ssh_client.run(
        "[ -d /app_configs ] && echo app_configs "
        "|| ([ -d /addon_configs ] && echo addon_configs)",
        check=False,
    )
    token = stdout.strip()
    return f"/{token}" if token else ""
