"""Probe the deployment environment to find a running NetAlertX instance.

Tries the HA Supervisor add-on API first; falls back to Docker on the same SSH
host.  Returns a ``DeploymentInfo`` describing where NetAlertX is running and
how to reach it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol


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
