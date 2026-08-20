"""NetAlertX uninstaller — removes the HA add-on and cleans up Pueo state.

Reverses the installer's steps in order:
  1. Backup, then remove the NetAlertX webhook automation from automations.yaml
  2. Stop the HA add-on
  3. Uninstall the HA add-on
  4. Reset netalertx_install_state to NOT_INSTALLED

Each step is guarded: SSH failures are logged and treated as warnings (the
add-on may already be stopped/absent on the HA side) so that a partial
uninstall can always be completed on retry.

Entry point: `python main.py --mode netalertx-uninstall`
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import TYPE_CHECKING

from config import (
    DB_PATH,
    NETALERTX_ADDON_SLUG,
    NETALERTX_SSH_HOST,
    NETALERTX_SSH_KEY_PATH,
    NETALERTX_SSH_USER,
)
from utils.agent.autonomy import RiskLevel
from utils.hitl.card_types import CARD_TYPE_NETALERTX_UNINSTALL
from utils.core.logging import get_logger, set_correlation_id

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from utils.agent.autonomy import AutonomyGate
    from utils.hitl.notify import NotifierProtocol

log = get_logger("netalertx.uninstaller")

# Automations file paths tried in order (mirrors installer step 8)
_AUTOMATION_PATHS = [
    "/config/automations.yaml",
    "/config/automations/netalertx_webhook.yaml",
]


# ── automation removal ────────────────────────────────────────────────────────


def _remove_webhook_automation(content: str) -> str:
    """Remove the NetAlertX webhook automation block from automations YAML content.

    Detects the block by the unique id 'netalertx_event_handler' and removes
    from that list item to the next top-level list item (or end of file).
    Handles both single-file (automations.yaml) and per-file directory styles.
    Returns the cleaned content; returns an empty string when the NetAlertX
    automation was the only item.
    """
    # Match from the list-item marker before the netalertx id through to the
    # next top-level list item marker or end of string.
    pattern = re.compile(
        r"(?:^|\n)- (?:id: 'netalertx_event_handler'|.*\n(?:[^\n]*\n)*?.*"
        r"id: 'netalertx_event_handler').*?(?=\n- |\Z)",
        re.DOTALL,
    )
    # Simpler two-pass approach: split on top-level list items and filter.
    items = re.split(r"(?=^- )", content, flags=re.MULTILINE)
    filtered = [
        item
        for item in items
        if "netalertx_event_handler" not in item
        and "NetAlertX Event Handler" not in item
        and "netalertx_event" not in item
    ]
    result = "".join(filtered)
    # Collapse triple-blank lines introduced by removal.
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.rstrip("\n") + "\n" if result.strip() else ""


def _automation_present(content: str) -> bool:
    """Return True when a NetAlertX webhook automation is in the YAML content."""
    low = content.lower()
    return "netalertx_event_handler" in low or (
        "netalertx" in low and "platform: webhook" in low
    )


# ── state persistence (shared with installer) ─────────────────────────────────


def _write_install_state(db_path: str, state: str, cid: str) -> None:
    import datetime

    ts = datetime.datetime.now(datetime.UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM netalertx_install_state WHERE id = 1"
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE netalertx_install_state "
                "SET state=?, correlation_id=?, timestamp=?, details_json=? WHERE id=1",
                (state, cid, ts, "{}"),
            )
        else:
            conn.execute(
                "INSERT INTO netalertx_install_state "
                "(id, state, correlation_id, timestamp, details_json) VALUES (1,?,?,?,?)",
                (state, cid, ts, "{}"),
            )
        conn.commit()
    log.info("install_state_updated", state=state, correlation_id=cid)


def _read_install_state(db_path: str) -> str:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT state FROM netalertx_install_state WHERE id = 1"
            ).fetchone()
        return row[0] if row else "NOT_INSTALLED"
    except Exception:
        return "NOT_INSTALLED"


def _resolve_slug(db_path: str) -> str:
    """Return the slug from config or from the install-state DB details."""
    if NETALERTX_ADDON_SLUG:
        return NETALERTX_ADDON_SLUG
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT details_json FROM netalertx_install_state WHERE id = 1"
            ).fetchone()
        if row and row[0]:
            details = json.loads(row[0])
            return details.get("addon_slug", "")
    except Exception:  # nosec B110
        pass
    return ""


# ── disk reporting ────────────────────────────────────────────────────────────


async def _report_disk_space(ssh_client: "SSHClientProtocol") -> str:
    """Return a human-readable disk-space line for the HA data partition."""
    ec, out, _ = await ssh_client.run("df -h /data 2>/dev/null || df -h /")
    if ec != 0 or not out.strip():
        return "(disk info unavailable)"
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("Filesystem")]
    return lines[0] if lines else "(disk info unavailable)"


# ── main uninstall flow ───────────────────────────────────────────────────────


async def run_uninstaller(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
) -> str:
    """Uninstall NetAlertX from HA and reset Pueo's install state.

    Returns the final install state string ('NOT_INSTALLED' on success).
    """
    cid = str(uuid.uuid4())[:8]
    set_correlation_id(cid)
    log.info("uninstaller_start", correlation_id=cid)

    current_state = _read_install_state(db_path)
    slug = _resolve_slug(db_path)
    if not slug:
        slug = "db21ed7f_netalertx_fa"  # well-known FA slug as fallback
        log.warning(
            "uninstaller_slug_fallback",
            slug=slug,
            reason="slug not in config or DB; using well-known FA slug",
            correlation_id=cid,
        )

    disk_before = await _report_disk_space(ssh_client)
    log.info("uninstaller_disk_before", disk=disk_before, correlation_id=cid)

    # ── Approval required (CRITICAL — destructive, irreversible on HA side) ───
    approved = await gate.require_approval(
        subject="NetAlertX uninstall: remove add-on from Home Assistant",
        body=(
            f"This will stop and uninstall the NetAlertX add-on ({slug!r}) from HA "
            f"and remove the Pueo webhook automation from automations.yaml.\n\n"
            f"Current install state: {current_state}\n"
            f"HA disk before: {disk_before}\n\n"
            "The add-on's config data directory on HA will also be removed by HA Supervisor. "
            "This action is not easily reversible — re-installation requires running "
            "`python main.py --mode netalertx-setup` or `--mode netalertx-docker-setup`."
        ),
        payload={
            "card_type": CARD_TYPE_NETALERTX_UNINSTALL,
            "notification_id": f"{cid}_uninstall_approval",
            "slug": slug,
            "state_before": current_state,
        },
        notifier=notifier,
        risk=RiskLevel.CRITICAL,
    )
    if not approved:
        log.info("uninstaller_rejected", correlation_id=cid)
        return current_state

    # ── Step 1: remove webhook automation ────────────────────────────────────
    log.info("uninstaller_step1_start", step="remove_automation", correlation_id=cid)
    automation_removed = False
    for auto_path in _AUTOMATION_PATHS:
        try:
            content = await ssh_client.read_file(auto_path)
        except FileNotFoundError:
            continue
        if not _automation_present(content):
            log.info(
                "uninstaller_step1_not_present",
                path=auto_path,
                correlation_id=cid,
            )
            automation_removed = True
            break
        # Backup before write (safety invariant)
        from agents.ha_agent_sandbox_engine import execute_remote_backup

        try:
            backup_slug = await execute_remote_backup(ssh_client=ssh_client)
            log.info(
                "uninstaller_step1_backup",
                backup_slug=backup_slug,
                correlation_id=cid,
            )
        except Exception as exc:
            log.warning(
                "uninstaller_step1_backup_failed",
                error=str(exc),
                action="proceeding without backup — automation removal is low-risk",
                correlation_id=cid,
            )

        new_content = _remove_webhook_automation(content)
        await ssh_client.write_file(auto_path, new_content)
        log.info(
            "uninstaller_step1_automation_removed",
            path=auto_path,
            correlation_id=cid,
        )
        automation_removed = True
        break

    if not automation_removed:
        log.info(
            "uninstaller_step1_no_automation_file",
            tried=_AUTOMATION_PATHS,
            correlation_id=cid,
        )

    # ── Step 2: stop the add-on ───────────────────────────────────────────────
    log.info(
        "uninstaller_step2_start", step="stop_addon", slug=slug, correlation_id=cid
    )
    ec, _, stderr = await ssh_client.run(f"ha apps stop {slug}")
    if ec != 0:
        log.warning(
            "uninstaller_step2_stop_failed",
            slug=slug,
            error=stderr,
            action="continuing — add-on may already be stopped",
            correlation_id=cid,
        )
    else:
        log.info("uninstaller_step2_stopped", slug=slug, correlation_id=cid)

    # ── Step 3: uninstall the add-on ─────────────────────────────────────────
    log.info(
        "uninstaller_step3_start",
        step="uninstall_addon",
        slug=slug,
        correlation_id=cid,
    )
    ec, _, stderr = await ssh_client.run(f"ha apps uninstall {slug}")
    if ec != 0:
        log.warning(
            "uninstaller_step3_uninstall_failed",
            slug=slug,
            error=stderr,
            action="continuing — add-on may already be absent",
            correlation_id=cid,
        )
    else:
        log.info("uninstaller_step3_uninstalled", slug=slug, correlation_id=cid)

    # ── Step 4: reset install state ───────────────────────────────────────────
    _write_install_state(db_path, "NOT_INSTALLED", cid)
    log.info("uninstaller_step4_state_reset", correlation_id=cid)

    disk_after = await _report_disk_space(ssh_client)
    log.info(
        "uninstaller_complete",
        slug=slug,
        disk_before=disk_before,
        disk_after=disk_after,
        correlation_id=cid,
    )
    await notifier.send(
        subject="NetAlertX uninstalled from Home Assistant",
        body=(
            f"Add-on {slug!r} has been removed from HA.\n\n"
            f"Disk before: {disk_before}\n"
            f"Disk after:  {disk_after}\n\n"
            "To install on a separate machine: python main.py --mode netalertx-docker-setup\n"
            "To reinstall on HA: python main.py --mode netalertx-setup"
        ),
        payload={
            "card_type": CARD_TYPE_NETALERTX_UNINSTALL,
            "notification_id": f"{cid}_uninstall_done",
        },
    )
    return "NOT_INSTALLED"


async def main(
    ssh_client: "SSHClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    db_path: str = DB_PATH,
) -> None:
    """Entry point for `--mode netalertx-uninstall`."""
    from config import AUTONOMY_LEVEL, NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR
    from utils.agent.autonomy import AutonomyGate
    from utils.hitl.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient

    _ssh = ssh_client or AsyncSSHClient(
        NETALERTX_SSH_HOST, NETALERTX_SSH_USER, NETALERTX_SSH_KEY_PATH
    )
    _gate = gate or AutonomyGate(level=AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    final_state = await run_uninstaller(_ssh, _gate, _notifier, db_path=db_path)
    log.info("netalertx_uninstall_done", state=final_state)
