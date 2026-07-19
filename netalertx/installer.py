"""NetAlertX idempotent installer — 8-step state machine (items 11–12).

State machine (persisted to netalertx_install_state after each completed step):

  NOT_INSTALLED → MQTT_INSTALLED → MQTT_RUNNING → ADDON_REPO_ADDED
    → ADDON_INSTALLED → ADDON_RUNNING → NETALERTX_CONFIGURED
    → HA_MQTT_INTEGRATION_VERIFIED → HA_AUTOMATION_CREATED → FULLY_OPERATIONAL

Steps 3 and 4 share the MQTT_RUNNING → ADDON_REPO_ADDED transition:
  - Step 3 detects the scan interface and persists it to details_json (state stays
    MQTT_RUNNING) so it can be skipped on re-entry.
  - Step 4 adds the repository and resolves the slug, then advances to ADDON_REPO_ADDED.

Invariants for all steps:
  - Entry and exit logged with a shared correlation ID.
  - On step failure: log structured error → gate.require_approval(CRITICAL) to
    notify; abort.  Prior steps are not rolled back — each is idempotent on
    re-entry.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import TYPE_CHECKING

import httpx

from config import (
    DB_PATH,
    NETALERTX_ADDON_REPOSITORY_URL,
    NETALERTX_ADDON_SLUG,
    NETALERTX_SCAN_INTERFACE,
)
from utils.autonomy import RiskLevel
from utils.logging import get_logger, set_correlation_id

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.installer")

# Ordered install state progression
INSTALL_STATES: list[str] = [
    "NOT_INSTALLED",
    "MQTT_INSTALLED",
    "MQTT_RUNNING",
    "ADDON_REPO_ADDED",
    "ADDON_INSTALLED",
    "ADDON_RUNNING",
    "NETALERTX_CONFIGURED",
    "HA_MQTT_INTEGRATION_VERIFIED",
    "HA_AUTOMATION_CREATED",
    "FULLY_OPERATIONAL",
]

_STATE_RANK: dict[str, int] = {s: i for i, s in enumerate(INSTALL_STATES)}


# ── persistence helpers ───────────────────────────────────────────────────────


def _read_install_state(db_path: str) -> tuple[str, dict]:
    """Return (state, details_json_dict) from the DB, or NOT_INSTALLED if absent."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT state, details_json FROM netalertx_install_state WHERE id = 1"
        ).fetchone()
    if row is None:
        return "NOT_INSTALLED", {}
    details = json.loads(row[1] or "{}") if row[1] else {}
    return row[0], details


def _write_install_state(db_path: str, state: str, details: dict, cid: str) -> None:
    import datetime

    ts = datetime.datetime.now(datetime.UTC).isoformat()
    payload = json.dumps(details)
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM netalertx_install_state WHERE id = 1"
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE netalertx_install_state "
                "SET state=?, correlation_id=?, timestamp=?, details_json=? WHERE id=1",
                (state, cid, ts, payload),
            )
        else:
            conn.execute(
                "INSERT INTO netalertx_install_state "
                "(id, state, correlation_id, timestamp, details_json) VALUES (1,?,?,?,?)",
                (state, cid, ts, payload),
            )
        conn.commit()
    log.info("install_state_updated", state=state, correlation_id=cid)


# ── step helpers ──────────────────────────────────────────────────────────────


async def _poll_addon_state(
    ssh_client: "SSHClientProtocol",
    addon_id: str,
    expected: str,
    attempts: int = 6,
    delay: float = 5.0,
) -> bool:
    """Poll `ha addons info <addon_id>` until state == expected or timeout."""
    import asyncio

    pattern = re.compile(r"state:\s*(\S+)", re.IGNORECASE)
    for _ in range(attempts):
        _, stdout, _ = await ssh_client.run(f"ha addons info {addon_id}")
        m = pattern.search(stdout)
        if m and m.group(1).lower() == expected.lower():
            return True
        await asyncio.sleep(delay)
    return False


def _parse_slug_from_store(store_output: str, repository_url: str) -> str:
    """Parse the add-on slug from `ha store addons` output.

    Looks for a slug: field near a line referencing the repository URL or
    'netalertx'.  Returns the first match, or "" if none.
    """
    repo_suffix = (
        repository_url.rstrip("/").split("/")[-2]
        + "/"
        + repository_url.rstrip("/").split("/")[-1]
    )
    lines = store_output.splitlines()
    for i, line in enumerate(lines):
        if repo_suffix.lower() in line.lower() or "netalertx" in line.lower():
            slug_match = re.search(r"slug:\s*(\S+)", line, re.IGNORECASE)
            if slug_match:
                return slug_match.group(1)
            for ctx_line in lines[max(0, i - 3) : i + 4]:
                m = re.search(r"slug:\s*(\S+)", ctx_line, re.IGNORECASE)
                if m:
                    return m.group(1)
    return ""


# ── steps 1–4 ────────────────────────────────────────────────────────────────


async def _step1_detect_deployment(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Detect whether to install as HA add-on or Docker container.

    Advances state to MQTT_INSTALLED on success.
    """
    log.info("step1_start", step="detect_deployment", correlation_id=cid)

    ec, _, _ = await ssh_client.run("ha supervisor info")
    if ec == 0:
        mode = "addon"
        log.info("step1_supervisor_found", mode=mode, correlation_id=cid)
    else:
        ec2, _, _ = await ssh_client.run("docker info")
        if ec2 == 0:
            approved = await gate.require_approval(
                subject="NetAlertX installer: Docker fallback",
                body=(
                    "HA Supervisor is unavailable on this host. "
                    "Falling back to Docker deployment mode — confirm to proceed?"
                ),
                payload={"notification_id": f"{cid}_step1_docker", "step": 1},
                notifier=notifier,
                risk=RiskLevel.MEDIUM,
            )
            if not approved:
                log.warning("step1_docker_rejected", correlation_id=cid)
                return False
            mode = "docker"
        else:
            await gate.require_approval(
                subject="NetAlertX installer: no deployment target",
                body=(
                    "Neither HA Supervisor nor Docker is available on the SSH host. "
                    "Install aborted — please set up a deployment target first."
                ),
                payload={"notification_id": f"{cid}_step1_abort", "step": 1},
                notifier=notifier,
                risk=RiskLevel.CRITICAL,
            )
            log.error("step1_no_target", correlation_id=cid)
            return False

    details["mode"] = mode
    _write_install_state(db_path, "MQTT_INSTALLED", details, cid)
    log.info("step1_complete", mode=mode, correlation_id=cid)
    return True


async def _step2_install_mosquitto(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Install and start the Mosquitto MQTT add-on if needed.

    Advances state to MQTT_RUNNING on success.
    """
    log.info("step2_start", step="install_mosquitto", correlation_id=cid)

    _, stdout, _ = await ssh_client.run("ha addons info core_mosquitto")
    state_match = re.search(r"state:\s*(\S+)", stdout, re.IGNORECASE)
    current_addon_state = state_match.group(1).lower() if state_match else "unknown"

    if current_addon_state in ("unknown",) or "not found" in stdout.lower():
        approved = await gate.require_approval(
            subject="NetAlertX installer: install Mosquitto",
            body=(
                "Mosquitto MQTT broker (core_mosquitto) is not installed. "
                "Installing it will affect all HA MQTT integrations — approve?"
            ),
            payload={"notification_id": f"{cid}_step2_install", "step": 2},
            notifier=notifier,
            risk=RiskLevel.HIGH,
        )
        if not approved:
            log.warning("step2_mosquitto_install_rejected", correlation_id=cid)
            return False
        await ssh_client.run("ha addons install core_mosquitto", check=True)
        await ssh_client.run("ha addons start core_mosquitto", check=True)
    elif current_addon_state != "running":
        await ssh_client.run("ha addons start core_mosquitto", check=True)

    running = await _poll_addon_state(ssh_client, "core_mosquitto", "running")
    if not running:
        log.error("step2_mosquitto_not_running", correlation_id=cid)
        await gate.require_approval(
            subject="NetAlertX installer: Mosquitto failed to start",
            body="Mosquitto did not reach running state within 30 s. Aborting.",
            payload={"notification_id": f"{cid}_step2_abort", "step": 2},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False

    _write_install_state(db_path, "MQTT_RUNNING", details, cid)
    log.info("step2_complete", correlation_id=cid)
    return True


async def _step3_detect_interface(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Auto-detect or confirm the network scan interface.

    Stores the interface in details_json but does NOT advance the state
    (MQTT_RUNNING).  Step 4 is responsible for advancing to ADDON_REPO_ADDED.
    Idempotent: skips detection if ``scan_interface`` is already in details.
    """
    log.info("step3_start", step="detect_interface", correlation_id=cid)

    # Idempotency: already detected in a prior run
    if details.get("scan_interface"):
        log.info(
            "step3_interface_already_detected",
            interface=details["scan_interface"],
            correlation_id=cid,
        )
        return True

    # Config override takes precedence over auto-detection
    if NETALERTX_SCAN_INTERFACE:
        details["scan_interface"] = NETALERTX_SCAN_INTERFACE
        _write_install_state(db_path, "MQTT_RUNNING", details, cid)
        log.info(
            "step3_interface_from_config",
            interface=NETALERTX_SCAN_INTERFACE,
            correlation_id=cid,
        )
        return True

    _, stdout, _ = await ssh_client.run("ip route show default")
    candidates = re.findall(r"\bdev\s+(\S+)", stdout)
    candidates = list(dict.fromkeys(candidates))  # deduplicate, preserve order

    if len(candidates) == 1:
        details["scan_interface"] = candidates[0]
        _write_install_state(db_path, "MQTT_RUNNING", details, cid)
        log.info(
            "step3_interface_detected", interface=candidates[0], correlation_id=cid
        )
        return True

    if len(candidates) > 1:
        iface_list = "\n".join(f"  - {c}" for c in candidates)
        approved = await gate.require_approval(
            subject="NetAlertX installer: confirm scan interface",
            body=(
                f"Multiple network interfaces found:\n{iface_list}\n\n"
                f"Approve to use the first candidate ({candidates[0]}), "
                "or reject to abort and set `netalertx.scan_interface` in config.yaml."
            ),
            payload={
                "notification_id": f"{cid}_step3_multi",
                "step": 3,
                "candidates": candidates,
            },
            notifier=notifier,
            risk=RiskLevel.MEDIUM,
        )
        if not approved:
            log.warning("step3_interface_selection_rejected", correlation_id=cid)
            return False
        details["scan_interface"] = candidates[0]
        _write_install_state(db_path, "MQTT_RUNNING", details, cid)
        return True

    # No candidates found
    await gate.require_approval(
        subject="NetAlertX installer: interface detection failed",
        body=(
            "Could not detect the default network interface via `ip route show default`. "
            "Set `netalertx.scan_interface` in config.yaml and re-run setup."
        ),
        payload={"notification_id": f"{cid}_step3_abort", "step": 3},
        notifier=notifier,
        risk=RiskLevel.CRITICAL,
    )
    log.error("step3_no_interface", correlation_id=cid)
    return False


async def _step4_add_repo_and_resolve_slug(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Add the NetAlertX add-on repository and resolve the add-on slug.

    Advances state to ADDON_REPO_ADDED on success.
    """
    log.info("step4_start", step="add_repo_resolve_slug", correlation_id=cid)

    _, stdout, _ = await ssh_client.run("ha store repositories list")
    if NETALERTX_ADDON_REPOSITORY_URL not in stdout:
        await ssh_client.run(
            f"ha store repositories add {NETALERTX_ADDON_REPOSITORY_URL}", check=True
        )
        _, stdout2, _ = await ssh_client.run("ha store repositories list")
        if NETALERTX_ADDON_REPOSITORY_URL not in stdout2:
            await gate.require_approval(
                subject="NetAlertX installer: repository add failed",
                body=(
                    f"Failed to add repository {NETALERTX_ADDON_REPOSITORY_URL}. "
                    "Aborting — check Supervisor connectivity and re-run setup."
                ),
                payload={"notification_id": f"{cid}_step4_abort", "step": 4},
                notifier=notifier,
                risk=RiskLevel.CRITICAL,
            )
            log.error("step4_repo_add_failed", correlation_id=cid)
            return False
        log.info(
            "step4_repo_added", url=NETALERTX_ADDON_REPOSITORY_URL, correlation_id=cid
        )
    else:
        log.info("step4_repo_already_present", correlation_id=cid)

    # Resolve slug — config value takes precedence
    if NETALERTX_ADDON_SLUG:
        details["addon_slug"] = NETALERTX_ADDON_SLUG
        log.info(
            "step4_slug_from_config", slug=NETALERTX_ADDON_SLUG, correlation_id=cid
        )
    elif not details.get("addon_slug"):
        _, addons_out, _ = await ssh_client.run("ha store addons")
        slug = _parse_slug_from_store(addons_out, NETALERTX_ADDON_REPOSITORY_URL)
        if slug:
            details["addon_slug"] = slug
            log.info("step4_slug_resolved", slug=slug, correlation_id=cid)
        else:
            # Non-fatal: slug may be resolvable in step 5 after a Supervisor refresh
            log.warning("step4_slug_not_resolved", correlation_id=cid)

    _write_install_state(db_path, "ADDON_REPO_ADDED", details, cid)
    log.info("step4_complete", correlation_id=cid)
    return True


# ── public entry point ────────────────────────────────────────────────────────


async def run_steps_1_to_4(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Execute installer steps 1–4, resuming from the current persisted state.

    Returns the final state string after completion or on failure.
    """
    cid = str(uuid.uuid4())
    set_correlation_id(cid)

    current_state, details = _read_install_state(db_path)
    log.info(
        "installer_start",
        current_state=current_state,
        steps="1-4",
        correlation_id=cid,
    )

    # Step 1: NOT_INSTALLED → MQTT_INSTALLED
    if _STATE_RANK[current_state] < _STATE_RANK["MQTT_INSTALLED"]:
        ok = await _step1_detect_deployment(
            ssh_client, gate, notifier, details, cid, db_path
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, details = _read_install_state(db_path)

    # Step 2: MQTT_INSTALLED → MQTT_RUNNING
    if _STATE_RANK[current_state] < _STATE_RANK["MQTT_RUNNING"]:
        ok = await _step2_install_mosquitto(
            ssh_client, gate, notifier, details, cid, db_path
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, details = _read_install_state(db_path)

    # Step 3: detect interface (state stays MQTT_RUNNING; stored in details_json)
    # Step 4: MQTT_RUNNING → ADDON_REPO_ADDED (both steps share this transition)
    if _STATE_RANK[current_state] < _STATE_RANK["ADDON_REPO_ADDED"]:
        ok = await _step3_detect_interface(
            ssh_client, gate, notifier, details, cid, db_path
        )
        if not ok:
            return _read_install_state(db_path)[0]
        # Reload after step 3 updated details_json
        current_state, details = _read_install_state(db_path)

        ok = await _step4_add_repo_and_resolve_slug(
            ssh_client, gate, notifier, details, cid, db_path
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, _ = _read_install_state(db_path)

    log.info(
        "installer_steps_1_to_4_complete",
        final_state=current_state,
        correlation_id=cid,
    )
    return current_state


async def main(
    ssh_client: "SSHClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    db_path: str = DB_PATH,
) -> None:
    """Entry point for `--mode netalertx-setup`."""
    from config import (
        AUTONOMY_LEVEL,
        HITL_TIMEOUT_MINUTES,
        NOTIFIER,
        NOTIFY_URL,
        NOTIFY_WATCH_DIR,
    )
    from config import NETALERTX_SSH_HOST, NETALERTX_SSH_KEY_PATH, NETALERTX_SSH_USER
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

    _ssh = ssh_client or AsyncSSHClient(
        NETALERTX_SSH_HOST, NETALERTX_SSH_USER, NETALERTX_SSH_KEY_PATH
    )
    _gate = gate or AutonomyGate(
        level=AUTONOMY_LEVEL, timeout_minutes=HITL_TIMEOUT_MINUTES
    )
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    final_state = await run_steps_1_to_4(_ssh, _gate, _notifier, db_path=db_path)
    log.info("netalertx_setup_done", state=final_state)
