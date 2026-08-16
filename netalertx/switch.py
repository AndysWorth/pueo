"""NetAlertX platform switch — moves an installation from HA to Docker or vice versa.

Flow:
  1. Read current platform from details_json (or infer from state name).
  2. Read desired platform from cfg.NETALERTX_DEPLOY_TARGET.
  3. Abort if no mismatch or if state is not FULLY_OPERATIONAL.
  4. Issue CARD_TYPE_NETALERTX_SWITCH approval card.
  5. On approve: run the current-platform uninstaller, then the target-platform installer.

Entry point: `python main.py --mode netalertx-switch`
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from config import DB_PATH, NETALERTX_DEPLOY_TARGET
from utils.card_types import CARD_TYPE_NETALERTX_SWITCH
from utils.logging import get_logger, set_correlation_id

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.switch")


def _get_installed_platform(db_path: str) -> tuple[str, str]:
    """Return (state, platform) from the install DB.

    Platform defaults to 'docker' for DOCKER_* states and 'ha' otherwise.
    """
    import json
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT state, details_json FROM netalertx_install_state WHERE id = 1"
            ).fetchone()
        if not row:
            return "NOT_INSTALLED", "ha"
        state, details_raw = row
        details = json.loads(details_raw or "{}") if details_raw else {}
        platform = details.get("platform")
        if not platform:
            platform = "docker" if (state or "").startswith("DOCKER_") else "ha"
        return state or "NOT_INSTALLED", str(platform)
    except Exception:  # nosec B110
        return "NOT_INSTALLED", "ha"


async def run_switch(
    docker_ssh: "SSHClientProtocol",
    ha_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
    target_platform: str = NETALERTX_DEPLOY_TARGET,
) -> str:
    """Switch NetAlertX from its current platform to target_platform.

    Returns 'FULLY_OPERATIONAL' on success, or the state before if aborted.
    """
    import uuid

    cid = str(uuid.uuid4())[:8]
    set_correlation_id(cid)

    state, current_platform = _get_installed_platform(db_path)

    if state != "FULLY_OPERATIONAL":
        log.warning(
            "switch_not_operational",
            state=state,
            action="Nothing is fully installed; run setup instead.",
            correlation_id=cid,
        )
        print(
            f"NetAlertX install state is {state!r} — "
            "nothing to switch. Run setup first."
        )
        return state

    if current_platform == target_platform:
        log.info(
            "switch_no_mismatch",
            platform=current_platform,
            correlation_id=cid,
        )
        print(f"NetAlertX is already on {current_platform!r}. Nothing to do.")
        return state

    from utils.autonomy import RiskLevel

    approved = await gate.require_approval(
        subject=(f"Switch NetAlertX from {current_platform!r} to {target_platform!r}"),
        body=(
            f"Current platform: {current_platform}\n"
            f"Target platform:  {target_platform}\n\n"
            "Steps:\n"
            f"  1. Uninstall from {current_platform} (approval required)\n"
            f"  2. Install on {target_platform}\n\n"
            "The current installation must be fully removed before the new one starts. "
            "Approve to begin the switch."
        ),
        payload={
            "card_type": CARD_TYPE_NETALERTX_SWITCH,
            "notification_id": f"{cid}_switch_approval",
            "current_platform": current_platform,
            "target_platform": target_platform,
        },
        notifier=notifier,
        risk=RiskLevel.CRITICAL,
    )
    if not approved:
        log.info("switch_rejected", correlation_id=cid)
        return state

    # ── Run current-platform uninstaller ──────────────────────────────────────
    if current_platform == "ha":
        from netalertx.uninstaller import run_uninstaller

        final = await run_uninstaller(ha_ssh, gate, notifier, db_path=db_path)
    else:
        from netalertx.docker_uninstaller import run_docker_uninstaller

        final = await run_docker_uninstaller(
            docker_ssh, ha_ssh, gate, notifier, db_path=db_path
        )

    if final != "NOT_INSTALLED":
        log.error(
            "switch_uninstall_incomplete",
            state=final,
            correlation_id=cid,
        )
        return final

    # ── Run target-platform installer ─────────────────────────────────────────
    if target_platform == "docker":
        from netalertx.docker_installer import run_docker_installer

        result = await run_docker_installer(
            docker_ssh, ha_ssh, gate, notifier, db_path=db_path
        )
        new_state = result if isinstance(result, str) else "FULLY_OPERATIONAL"
    else:
        from netalertx.installer import run_installer

        new_state = await run_installer(ha_ssh, gate, notifier, db_path=db_path)

    log.info(
        "switch_complete",
        from_platform=current_platform,
        to_platform=target_platform,
        final_state=new_state,
        correlation_id=cid,
    )
    return new_state


async def main(
    docker_ssh: "SSHClientProtocol | None" = None,
    ha_ssh: "SSHClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    db_path: str = DB_PATH,
) -> None:
    """Entry point for `--mode netalertx-switch`."""
    from config import (
        AUTONOMY_LEVEL,
        HA_USER,
        NETALERTX_DOCKER_HOST,
        NETALERTX_DOCKER_SSH_KEY_PATH,
        NETALERTX_DOCKER_SSH_USER,
        NETALERTX_SSH_HOST,
        NETALERTX_SSH_KEY_PATH,
        NETALERTX_SSH_USER,
        NOTIFIER,
        NOTIFY_URL,
        NOTIFY_WATCH_DIR,
        SSH_KEY_PATH,
    )
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

    final = await run_switch(_docker_ssh, _ha_ssh, _gate, _notifier, db_path=db_path)
    log.info("netalertx_switch_done", state=final)
