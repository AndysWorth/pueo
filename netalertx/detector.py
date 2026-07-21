"""Probe the deployment environment to find a running NetAlertX instance.

Tries the HA Supervisor add-on API first; falls back to Docker on the same SSH
host.  Returns a ``DeploymentInfo`` describing where NetAlertX is running and
how to reach it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from utils.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

log = get_logger("netalertx.detector")

# Minimum NetAlertX version Pueo requires.  All endpoints used by Pueo
# (/devices, /events, /metrics, /graphql, /settings/<key>, /health,
# /nettools/trigger-scan, /device/<mac>/update-column, /device/<mac>/field/lock)
# were present and stable as of v26.5.4.
NETALERTX_MIN_VERSION: tuple[int, ...] = (26, 5, 4)


@dataclass
class DeploymentInfo:
    """Describes a discovered NetAlertX deployment."""

    mode: str  # "addon" | "docker"
    container_name: str
    api_base_url: str
    log_path: str  # /data/app.log inside Docker volume (v25.11.29+)
    version: str  # from GET /settings/VERSION; "" if unreachable


async def detect_deployment(
    ssh_client: "SSHClientProtocol",
    host: str,
    api_port: int,
    container_name: str,
    http_client: httpx.AsyncClient | None = None,
) -> DeploymentInfo:
    """Detect whether NetAlertX is running as an HA add-on or a Docker container.

    Probes via SSH (``ha supervisor info``, then ``docker info``) and reads the
    version from the NetAlertX REST API.

    Args:
        ssh_client: injected SSH client (real or fake for tests).
        host: NetAlertX host (IP or hostname).
        api_port: NetAlertX API port (default 20212).
        container_name: Docker container name (from config, default "netalertx").
        http_client: optional pre-built httpx.AsyncClient for tests.

    Returns:
        DeploymentInfo with mode, container_name, api_base_url, log_path, version.

    Raises:
        RuntimeError: if neither HA Supervisor nor Docker is available.
    """
    exit_code, _, _ = await ssh_client.run("ha supervisor info")
    if exit_code == 0:
        mode = "addon"
    else:
        exit_code2, _, _ = await ssh_client.run("docker info")
        if exit_code2 == 0:
            mode = "docker"
        else:
            raise RuntimeError(
                "NetAlertX deployment detection failed: "
                "neither HA Supervisor nor Docker is available on the SSH host"
            )

    api_base_url = f"http://{host}:{api_port}"
    version = await _fetch_version(api_base_url, http_client)
    check_min_version(version)

    return DeploymentInfo(
        mode=mode,
        container_name=container_name,
        api_base_url=api_base_url,
        log_path="/data/app.log",
        version=version,
    )


async def _fetch_version(
    api_base_url: str,
    http_client: httpx.AsyncClient | None,
) -> str:
    """GET /settings/VERSION from the NetAlertX REST API."""
    url = f"{api_base_url}/settings/VERSION"
    try:
        if http_client is not None:
            resp = await http_client.get(url)
        else:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
        resp.raise_for_status()
        return str(resp.json().get("value", ""))
    except Exception:
        return ""


def parse_version(version_str: str) -> tuple[int, ...] | None:
    """Parse a version string like 'v26.7.1' or '26.7.1' into an int tuple.

    Returns None if the string cannot be parsed.
    """
    s = version_str.strip().lstrip("v")
    if not s:
        return None
    parts = s.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def check_min_version(version: str) -> bool:
    """Return True if *version* meets NETALERTX_MIN_VERSION; log a warning otherwise.

    An empty or unparseable version string is treated as unknown and triggers a
    warning but does not raise — Pueo may still be able to operate normally.
    """
    min_str = ".".join(str(v) for v in NETALERTX_MIN_VERSION)
    if not version:
        log.warning(
            "netalertx_version_unknown",
            min_version=min_str,
            detail=(
                f"NetAlertX version could not be determined; "
                f"minimum required is v{min_str}. "
                "Verify the instance is reachable and up to date."
            ),
        )
        return False
    parsed = parse_version(version)
    if parsed is None:
        log.warning(
            "netalertx_version_unparseable",
            raw_version=version,
            min_version=min_str,
            detail=(
                f"NetAlertX version '{version}' could not be parsed; "
                f"expected format like 'v26.7.1'. Minimum required is v{min_str}."
            ),
        )
        return False
    if parsed < NETALERTX_MIN_VERSION:
        log.warning(
            "netalertx_version_too_old",
            detected_version=version,
            min_version=min_str,
            detail=(
                f"NetAlertX {version} is below the minimum supported version "
                f"v{min_str}. Some API endpoints may not be available."
            ),
        )
        return False
    return True
