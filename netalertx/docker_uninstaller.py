"""NetAlertX Docker uninstaller — removes the container and cleans up Pueo state.

Reverses the docker_installer's steps:
  1. CRITICAL approval required
  2. Stop container: `docker stop netalertx` on Docker SSH host
  3. Remove container: `docker rm netalertx`
  4. Offer config dir removal (separate approval step) — default: keep
  5. Remove webhook automation from HA (reuse uninstaller logic)
  6. Reset netalertx_install_state to NOT_INSTALLED

Entry point: `python main.py --mode netalertx-docker-uninstall`
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from config import (
    DB_PATH,
    HA_USER,
    NETALERTX_DOCKER_CONFIG_PATH,
    NETALERTX_DOCKER_HOST,
    NETALERTX_DOCKER_SSH_KEY_PATH,
    NETALERTX_DOCKER_SSH_USER,
    NETALERTX_SSH_HOST,
    NETALERTX_SSH_KEY_PATH,
    NETALERTX_SSH_USER,
    SSH_KEY_PATH,
)
from utils.autonomy import RiskLevel
from utils.card_types import CARD_TYPE_NETALERTX_UNINSTALL
from utils.core.logging import get_logger, set_correlation_id

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.docker_uninstaller")

_CONTAINER_NAME = "netalertx"


def _read_platform_from_db(db_path: str) -> str:
    """Return the platform recorded in details_json, defaulting to 'ha'."""
    import json
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT state, details_json FROM netalertx_install_state WHERE id = 1"
            ).fetchone()
        if not row:
            return "ha"
        state, details_raw = row
        details = json.loads(details_raw or "{}") if details_raw else {}
        if "platform" in details:
            return str(details["platform"])
        return "docker" if (state or "").startswith("DOCKER_") else "ha"
    except Exception:  # nosec B110
        return "ha"


async def _report_disk_space(ssh_client: "SSHClientProtocol") -> str:
    """Return a human-readable disk-space line from the Docker host."""
    ec, out, _ = await ssh_client.run("df -h / 2>/dev/null")
    if ec != 0 or not out.strip():
        return "(disk info unavailable)"
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("Filesystem")]
    return lines[0] if lines else "(disk info unavailable)"


async def run_docker_uninstaller(
    docker_ssh: "SSHClientProtocol",
    ha_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
    config_path: str = NETALERTX_DOCKER_CONFIG_PATH,
) -> str:
    """Uninstall the NetAlertX Docker container and reset Pueo's install state.

    Returns the final install state string ('NOT_INSTALLED' on success).
    """
    import uuid

    from netalertx.uninstaller import (
        _AUTOMATION_PATHS,
        _automation_present,
        _read_install_state,
        _remove_webhook_automation,
        _write_install_state,
    )

    cid = str(uuid.uuid4())[:8]
    set_correlation_id(cid)
    log.info("docker_uninstaller_start", correlation_id=cid)

    current_state = _read_install_state(db_path)
    disk_before = await _report_disk_space(docker_ssh)

    # ── Approval required (CRITICAL — destructive, irreversible) ─────────────
    approved = await gate.require_approval(
        subject="NetAlertX Docker uninstall: remove container from Docker host",
        body=(
            f"This will stop and remove the {_CONTAINER_NAME!r} container from "
            f"{NETALERTX_DOCKER_HOST}.\n\n"
            f"Current install state: {current_state}\n"
            f"Docker host disk before: {disk_before}\n\n"
            "The HA webhook automation added during install will also be removed. "
            "This action is not easily reversible — re-installation requires running "
            "`python main.py --mode netalertx-docker-setup`."
        ),
        payload={
            "card_type": CARD_TYPE_NETALERTX_UNINSTALL,
            "notification_id": f"{cid}_docker_uninstall_approval",
            "container": _CONTAINER_NAME,
            "docker_host": NETALERTX_DOCKER_HOST,
            "state_before": current_state,
        },
        notifier=notifier,
        risk=RiskLevel.CRITICAL,
    )
    if not approved:
        log.info("docker_uninstaller_rejected", correlation_id=cid)
        return current_state

    # ── Step 1: stop container ────────────────────────────────────────────────
    log.info(
        "docker_uninstaller_step1_start",
        step="stop_container",
        container=_CONTAINER_NAME,
        correlation_id=cid,
    )
    ec, _, stderr = await docker_ssh.run(f"docker stop {_CONTAINER_NAME}")
    if ec != 0:
        log.warning(
            "docker_uninstaller_step1_stop_failed",
            container=_CONTAINER_NAME,
            error=stderr,
            action="continuing — container may already be stopped",
            correlation_id=cid,
        )
    else:
        log.info(
            "docker_uninstaller_step1_stopped",
            container=_CONTAINER_NAME,
            correlation_id=cid,
        )

    # ── Step 2: remove container ──────────────────────────────────────────────
    log.info(
        "docker_uninstaller_step2_start",
        step="remove_container",
        container=_CONTAINER_NAME,
        correlation_id=cid,
    )
    ec, _, stderr = await docker_ssh.run(f"docker rm {_CONTAINER_NAME}")
    if ec != 0:
        log.warning(
            "docker_uninstaller_step2_remove_failed",
            container=_CONTAINER_NAME,
            error=stderr,
            action="continuing — container may already be absent",
            correlation_id=cid,
        )
    else:
        log.info(
            "docker_uninstaller_step2_removed",
            container=_CONTAINER_NAME,
            correlation_id=cid,
        )

    # ── Step 3: offer config dir removal (optional approval step) ────────────
    if config_path:
        remove_config = await gate.require_approval(
            subject=f"Remove NetAlertX config directory on Docker host ({config_path})?",
            body=(
                f"The Docker host still has the NetAlertX config directory at:\n"
                f"  {config_path}\n\n"
                "Removing it frees disk space but deletes all NetAlertX configuration "
                "(app.conf, scan results). Keep it if you may reinstall on the same host.\n\n"
                "Approve = delete the directory. Reject = leave it in place."
            ),
            payload={
                "card_type": CARD_TYPE_NETALERTX_UNINSTALL,
                "notification_id": f"{cid}_docker_config_removal",
                "config_path": config_path,
            },
            notifier=notifier,
            risk=RiskLevel.MEDIUM,
        )
        if remove_config:
            ec, _, stderr = await docker_ssh.run(f"rm -rf {config_path}")
            if ec != 0:
                log.warning(
                    "docker_uninstaller_step3_config_removal_failed",
                    path=config_path,
                    error=stderr,
                    correlation_id=cid,
                )
            else:
                log.info(
                    "docker_uninstaller_step3_config_removed",
                    path=config_path,
                    correlation_id=cid,
                )
        else:
            log.info(
                "docker_uninstaller_step3_config_kept",
                path=config_path,
                correlation_id=cid,
            )

    # ── Step 4: remove webhook automation from HA ─────────────────────────────
    log.info(
        "docker_uninstaller_step4_start",
        step="remove_automation",
        correlation_id=cid,
    )
    automation_removed = False
    for auto_path in _AUTOMATION_PATHS:
        try:
            content = await ha_ssh.read_file(auto_path)
        except FileNotFoundError:
            continue
        if not _automation_present(content):
            log.info(
                "docker_uninstaller_step4_not_present",
                path=auto_path,
                correlation_id=cid,
            )
            automation_removed = True
            break
        new_content = _remove_webhook_automation(content)
        await ha_ssh.write_file(auto_path, new_content)
        log.info(
            "docker_uninstaller_step4_automation_removed",
            path=auto_path,
            correlation_id=cid,
        )
        automation_removed = True
        break

    if not automation_removed:
        log.info(
            "docker_uninstaller_step4_no_automation_file",
            tried=_AUTOMATION_PATHS,
            correlation_id=cid,
        )

    # ── Step 5: reset install state ───────────────────────────────────────────
    _write_install_state(db_path, "NOT_INSTALLED", cid)
    log.info("docker_uninstaller_step5_state_reset", correlation_id=cid)

    disk_after = await _report_disk_space(docker_ssh)
    log.info(
        "docker_uninstaller_complete",
        container=_CONTAINER_NAME,
        disk_before=disk_before,
        disk_after=disk_after,
        correlation_id=cid,
    )
    await notifier.send(
        subject="NetAlertX Docker container uninstalled",
        body=(
            f"Container {_CONTAINER_NAME!r} has been removed from {NETALERTX_DOCKER_HOST}.\n\n"
            f"Disk before: {disk_before}\n"
            f"Disk after:  {disk_after}\n\n"
            "To reinstall on Docker: python main.py --mode netalertx-docker-setup\n"
            "To install on HA: python main.py --mode netalertx-setup"
        ),
        payload={
            "card_type": CARD_TYPE_NETALERTX_UNINSTALL,
            "notification_id": f"{cid}_docker_uninstall_done",
        },
    )
    return "NOT_INSTALLED"


async def main(
    docker_ssh: "SSHClientProtocol | None" = None,
    ha_ssh: "SSHClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    db_path: str = DB_PATH,
) -> None:
    """Entry point for `--mode netalertx-docker-uninstall`."""
    from config import AUTONOMY_LEVEL, NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

    _docker_user = NETALERTX_DOCKER_SSH_USER or HA_USER
    _docker_key = NETALERTX_DOCKER_SSH_KEY_PATH or SSH_KEY_PATH

    _docker_ssh = docker_ssh or AsyncSSHClient(
        NETALERTX_DOCKER_HOST, _docker_user, _docker_key
    )
    _ha_host = NETALERTX_SSH_HOST
    _ha_user = NETALERTX_SSH_USER or HA_USER
    _ha_key = NETALERTX_SSH_KEY_PATH or SSH_KEY_PATH
    _ha_ssh = ha_ssh or AsyncSSHClient(_ha_host, _ha_user, _ha_key)

    _gate = gate or AutonomyGate(level=AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    final_state = await run_docker_uninstaller(
        _docker_ssh, _ha_ssh, _gate, _notifier, db_path=db_path
    )
    log.info("netalertx_docker_uninstall_done", state=final_state)
