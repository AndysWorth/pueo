"""NetAlertX Docker installer — installs NetAlertX FA on a separate Linux host.

State machine (persisted to netalertx_install_state after each completed step):

  NOT_INSTALLED → DOCKER_SSH_VERIFIED → DOCKER_DISK_CHECKED → DOCKER_DIR_CREATED
    → DOCKER_IMAGE_PULLED → DOCKER_CONFIGURED → DOCKER_RUNNING → DOCKER_HEALTHY
    → DOCKER_MQTT_ROUTED → DOCKER_WEBHOOK_CREATED → FULLY_OPERATIONAL

The installer requires two SSH connections:
  - docker_ssh_client: SSH to the target Docker host (all Docker operations)
  - ha_ssh_client: SSH to HA (MQTT routing config + webhook automation)

Entry point: `python main.py --mode netalertx-docker-setup`
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
    HA_API_TOKEN,
    HA_HOST,
    NETALERTX_DOCKER_CONFIG_PATH,
    NETALERTX_DOCKER_HOST,
    NETALERTX_DOCKER_IMAGE,
    NETALERTX_DOCKER_MIN_DISK_GB,
    NETALERTX_DOCKER_SSH_KEY_PATH,
    NETALERTX_DOCKER_SSH_USER,
    NETALERTX_MQTT_PASSWORD,
    NETALERTX_MQTT_USER,
)
from netalertx.disk_check import DiskSpaceTooLowError, check_target_disk_space
from utils.autonomy import RiskLevel
from utils.logging import get_logger, set_correlation_id

# Backward-compatible alias — existing tests import check_disk_space from here.
check_disk_space = check_target_disk_space

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.docker_installer")

DOCKER_INSTALL_STATES: list[str] = [
    "NOT_INSTALLED",
    "DOCKER_SSH_VERIFIED",
    "DOCKER_DISK_CHECKED",
    "DOCKER_DIR_CREATED",
    "DOCKER_IMAGE_PULLED",
    "DOCKER_CONFIGURED",
    "DOCKER_RUNNING",
    "DOCKER_HEALTHY",
    "DOCKER_MQTT_ROUTED",
    "DOCKER_WEBHOOK_CREATED",
    "FULLY_OPERATIONAL",
]

_STATE_RANK: dict[str, int] = {s: i for i, s in enumerate(DOCKER_INSTALL_STATES)}

# Mosquitto custom config written to HA to enable external listener
_MOSQUITTO_CUSTOM_CONF = "listener 1883 0.0.0.0\n"

# Docker container name for NetAlertX
_CONTAINER_NAME = "netalertx"


# ── persistence ───────────────────────────────────────────────────────────────


def _read_install_state(db_path: str) -> tuple[str, dict]:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT state, details_json FROM netalertx_install_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return "NOT_INSTALLED", {}
        details = json.loads(row[1] or "{}") if row[1] else {}
        return row[0], details
    except Exception:
        return "NOT_INSTALLED", {}


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


# ── app.conf helpers (shared logic with installer.py) ─────────────────────────


def _merge_app_conf(
    original: str, updates: dict[str, str]
) -> tuple[str, dict[str, str]]:
    lines = original.splitlines(keepends=True) if original else []
    applied: set[str] = set()
    diff: dict[str, str] = {}
    new_lines: list[str] = []
    key_pat = re.compile(r"^(\s*)([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
    for line in lines:
        m = key_pat.match(line)
        if m:
            indent, key, old_val = m.group(1), m.group(2), m.group(3).rstrip()
            if key in updates and key not in applied:
                new_val = updates[key]
                if old_val != new_val:
                    diff[key] = f"{old_val!r} -> {new_val!r}"
                new_lines.append(f"{indent}{key} = {new_val}\n")
                applied.add(key)
                continue
        new_lines.append(line if line.endswith("\n") else line + "\n")
    for key, val in updates.items():
        if key not in applied:
            diff[key] = f"<not set> -> {val!r}"
            new_lines.append(f"{key} = {val}\n")
    return "".join(new_lines), diff


def _merge_plugins(existing_val: str, required: list[str]) -> str:
    import ast

    try:
        existing: list = (
            ast.literal_eval(existing_val.strip()) if existing_val.strip() else []
        )
        if not isinstance(existing, list):
            existing = []
    except (ValueError, SyntaxError):
        existing = []
    merged = list(existing)
    for plugin in required:
        if plugin not in merged:
            merged.append(plugin)
    return repr(merged)


# ── install steps ─────────────────────────────────────────────────────────────


async def _step1_verify_docker_ssh(
    docker_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    docker_host: str,
) -> bool:
    log.info(
        "step1_start", step="verify_docker_ssh", host=docker_host, correlation_id=cid
    )
    ec, _, stderr = await docker_ssh.run("docker info")
    if ec != 0:
        await gate.require_approval(
            subject="NetAlertX Docker installer: Docker not available on target host",
            body=(
                f"Could not run 'docker info' on {docker_host!r}.\n\n"
                f"Error: {stderr}\n\n"
                "Ensure Docker is installed and the SSH user has permission to use it "
                "(e.g., is in the 'docker' group). Re-run after fixing."
            ),
            payload={"notification_id": f"{cid}_step1_abort", "step": 1},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        log.error("step1_docker_unavailable", host=docker_host, correlation_id=cid)
        return False
    _write_install_state(db_path, "DOCKER_SSH_VERIFIED", details, cid)
    log.info("step1_complete", host=docker_host, correlation_id=cid)
    return True


async def _step2_check_disk(
    docker_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    config_path: str,
    min_gb: float,
) -> bool:
    log.info("step2_start", step="check_disk", path=config_path, correlation_id=cid)
    # Check the parent directory (config_path may not exist yet)
    parent = config_path.rsplit("/", 1)[0] or "/"
    try:
        available_gb = await check_disk_space(docker_ssh, parent, min_gb)
    except DiskSpaceTooLowError as exc:
        await gate.require_approval(
            subject="NetAlertX Docker installer: insufficient disk space",
            body=(
                f"The Docker host has only {exc.available_gb:.1f} GB available at {exc.path!r}. "
                f"At least {exc.min_gb:.1f} GB is required for NetAlertX and its data.\n\n"
                "Free up space on the target machine and re-run setup."
            ),
            payload={"notification_id": f"{cid}_step2_disk_low", "step": 2},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        log.error(
            "step2_disk_too_low",
            available_gb=exc.available_gb,
            min_gb=exc.min_gb,
            correlation_id=cid,
        )
        return False
    except RuntimeError as exc:
        log.warning("step2_disk_check_failed", error=str(exc), correlation_id=cid)
        # Non-fatal: disk check failed (df unavailable), proceed with warning
    else:
        details["available_gb_at_install"] = available_gb
        log.info("step2_disk_ok", available_gb=available_gb, correlation_id=cid)
    _write_install_state(db_path, "DOCKER_DISK_CHECKED", details, cid)
    return True


async def _step3_create_config_dir(
    docker_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    config_path: str,
) -> bool:
    log.info(
        "step3_start", step="create_config_dir", path=config_path, correlation_id=cid
    )
    ec, _, stderr = await docker_ssh.run(f"mkdir -p {config_path}")
    if ec != 0:
        await gate.require_approval(
            subject="NetAlertX Docker installer: cannot create config directory",
            body=(
                f"Failed to create {config_path!r} on Docker host.\n\nError: {stderr}\n\n"
                "Ensure the SSH user has write permissions to the parent directory."
            ),
            payload={"notification_id": f"{cid}_step3_abort", "step": 3},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False
    _write_install_state(db_path, "DOCKER_DIR_CREATED", details, cid)
    log.info("step3_complete", path=config_path, correlation_id=cid)
    return True


async def _step4_pull_image(
    docker_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    image: str,
    config_path: str,
    min_gb: float,
) -> bool:
    log.info("step4_start", step="pull_image", image=image, correlation_id=cid)
    ec, _, stderr = await docker_ssh.run(f"docker pull {image}")
    if ec != 0:
        await gate.require_approval(
            subject="NetAlertX Docker installer: image pull failed",
            body=(
                f"docker pull {image!r} failed.\n\nError: {stderr}\n\n"
                "Check internet connectivity and image name on the Docker host."
            ),
            payload={"notification_id": f"{cid}_step4_abort", "step": 4},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False
    # Re-check disk after pull (image can be ~500 MB)
    parent = config_path.rsplit("/", 1)[0] or "/"
    try:
        await check_disk_space(docker_ssh, parent, min_gb)
    except DiskSpaceTooLowError as exc:
        log.warning(
            "step4_post_pull_disk_low",
            available_gb=exc.available_gb,
            min_gb=exc.min_gb,
            correlation_id=cid,
        )
    _write_install_state(db_path, "DOCKER_IMAGE_PULLED", details, cid)
    log.info("step4_complete", image=image, correlation_id=cid)
    return True


async def _step5_write_app_conf(
    docker_ssh: "SSHClientProtocol",
    details: dict,
    cid: str,
    db_path: str,
    config_path: str,
    docker_host: str,
) -> bool:
    log.info("step5_start", step="write_app_conf", path=config_path, correlation_id=cid)
    conf_path = f"{config_path}/app.conf"
    try:
        original_conf = await docker_ssh.read_file(conf_path)
    except Exception:
        original_conf = ""

    # Auto-detect subnet on Docker host
    subnet = ""
    _, route_out, _ = await docker_ssh.run("ip route show default")
    iface_m = re.search(r"\bdev\s+(\S+)", route_out)
    if iface_m:
        iface = iface_m.group(1)
        details["scan_interface"] = iface
        _, addr_out, _ = await docker_ssh.run(f"ip addr show {iface}")
        cidr_m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", addr_out)
        if cidr_m:
            import ipaddress

            try:
                subnet = str(ipaddress.ip_interface(cidr_m.group(1)).network)
            except ValueError:
                pass
        scan_subnets_val = f"['{subnet}   {iface}']" if subnet else f"['   {iface}']"
    else:
        scan_subnets_val = "[]"

    plugins_m = re.search(r"LOADED_PLUGINS\s*=\s*(\[.*?\])", original_conf, re.DOTALL)
    plugins_val = plugins_m.group(1) if plugins_m else "[]"
    merged_plugins = _merge_plugins(plugins_val, ["MQTT", "ARPSCAN"])

    updates: dict[str, str] = {
        "MQTT_BROKER": f"'{HA_HOST}'",
        "MQTT_PORT": "1883",
        "HA_URL": f"'http://{HA_HOST}:8123'",
        "HA_BEARER_TOKEN": f"'{HA_API_TOKEN}'",
        "HA_WEBHOOK_URL": f"'http://{HA_HOST}:8123/api/webhook/netalertx_event'",
        "SCAN_SUBNETS": scan_subnets_val,
        "TIMEZONE": "'UTC'",
        "LOADED_PLUGINS": merged_plugins,
    }
    if NETALERTX_MQTT_USER:
        updates["MQTT_USER"] = f"'{NETALERTX_MQTT_USER}'"
        updates["MQTT_PASSWORD"] = f"'{NETALERTX_MQTT_PASSWORD}'"

    merged_conf, diff = _merge_app_conf(original_conf, updates)
    await docker_ssh.write_file(conf_path, merged_conf)
    log.info("step5_app_conf_written", diff=diff, path=conf_path, correlation_id=cid)
    _write_install_state(db_path, "DOCKER_CONFIGURED", details, cid)
    return True


async def _step6_start_container(
    docker_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    image: str,
    config_path: str,
) -> bool:
    log.info("step6_start", step="start_container", correlation_id=cid)

    # Stop + remove any pre-existing container (idempotent)
    await docker_ssh.run(f"docker stop {_CONTAINER_NAME} 2>/dev/null || true")
    await docker_ssh.run(f"docker rm {_CONTAINER_NAME} 2>/dev/null || true")

    docker_cmd = (
        f"docker run -d --name {_CONTAINER_NAME} "
        "--restart=unless-stopped "
        "--network=host "
        "--cap-add=NET_RAW "
        f"-v {config_path}:/app/config "
        f"{image}"
    )
    log.info("running_command", cmd=docker_cmd, correlation_id=cid)
    ec, _, stderr = await docker_ssh.run(docker_cmd)
    if ec != 0:
        await gate.require_approval(
            subject="NetAlertX Docker installer: container failed to start",
            body=(
                f"docker run failed.\n\nError: {stderr}\n\n"
                "Check that the image was pulled correctly and the host supports "
                "--cap-add=NET_RAW (requires Linux kernel ≥ 3.19)."
            ),
            payload={"notification_id": f"{cid}_step6_abort", "step": 6},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False
    _write_install_state(db_path, "DOCKER_RUNNING", details, cid)
    log.info("step6_complete", container=_CONTAINER_NAME, correlation_id=cid)
    return True


async def _step7_wait_healthy(
    docker_host: str,
    api_port: int,
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    http_client: httpx.AsyncClient,
) -> bool:
    import asyncio

    log.info("step7_start", step="wait_healthy", host=docker_host, correlation_id=cid)
    url = f"http://{docker_host}:{api_port}/health"
    for attempt in range(12):  # 12 × 5 s = 60 s
        try:
            resp = await http_client.get(url, timeout=5)
            if resp.status_code == 200:
                _write_install_state(db_path, "DOCKER_HEALTHY", details, cid)
                log.info("step7_healthy", url=url, attempt=attempt, correlation_id=cid)
                return True
        except Exception:  # nosec B110
            pass
        log.info("step7_waiting", attempt=attempt, url=url, correlation_id=cid)
        await asyncio.sleep(5)
    await gate.require_approval(
        subject="NetAlertX Docker installer: health check timed out",
        body=(
            f"NetAlertX did not respond at {url} within 60 seconds.\n\n"
            "The container may still be initialising — check `docker logs netalertx` "
            "on the Docker host and re-run setup when the service is up."
        ),
        payload={"notification_id": f"{cid}_step7_timeout", "step": 7},
        notifier=notifier,
        risk=RiskLevel.HIGH,
    )
    return False


async def _step8_route_mqtt(
    ha_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    docker_host: str,
) -> bool:
    """Configure HA Mosquitto to accept external connections from the Docker host.

    Writes /ssl/mosquitto_custom.conf, enables customize mode via Supervisor API,
    then restarts Mosquitto.  All three actions are wrapped in a single HITL card
    (HIGH risk) because restarting Mosquitto briefly interrupts all HA MQTT traffic.
    """
    log.info("step8_start", step="route_mqtt", correlation_id=cid)

    approved = await gate.require_approval(
        subject="NetAlertX Docker installer: configure Mosquitto for external access",
        body=(
            f"To allow NetAlertX on {docker_host!r} to reach the HA MQTT broker, "
            "Pueo will:\n\n"
            "  1. Write /ssl/mosquitto_custom.conf with 'listener 1883 0.0.0.0'\n"
            "  2. Enable Mosquitto customize mode (Supervisor API)\n"
            "  3. Restart the Mosquitto add-on\n\n"
            "Mosquitto will be unavailable for ~10 seconds during restart. "
            "All HA MQTT integrations (device trackers, sensors) will reconnect automatically."
        ),
        payload={
            "notification_id": f"{cid}_step8_mqtt_approval",
            "step": 8,
        },
        notifier=notifier,
        risk=RiskLevel.HIGH,
    )
    if not approved:
        log.info("step8_mqtt_routing_skipped", correlation_id=cid)
        # Non-fatal: user may configure Mosquitto manually; proceed to next step
        _write_install_state(db_path, "DOCKER_MQTT_ROUTED", details, cid)
        return True

    # Write custom Mosquitto listener config
    await ha_ssh.write_file("/ssl/mosquitto_custom.conf", _MOSQUITTO_CUSTOM_CONF)
    log.info("step8_mosquitto_conf_written", correlation_id=cid)

    # Enable customize mode via Supervisor API
    customize_cmd = (
        "curl -sf -X POST http://supervisor/addons/core_mosquitto/options "
        '-H "Authorization: Bearer $SUPERVISOR_TOKEN" '
        '-H "Content-Type: application/json" '
        '-d \'{"customize": {"active": true, "folder": "ssl"}}\''
    )
    ec, _, stderr = await ha_ssh.run(customize_cmd)
    if ec != 0:
        log.warning(
            "step8_mosquitto_options_failed",
            error=stderr,
            action="proceeding — Mosquitto may already be configured for external access",
            correlation_id=cid,
        )

    # Restart Mosquitto
    ec, _, stderr = await ha_ssh.run("ha apps restart core_mosquitto")
    if ec != 0:
        log.warning(
            "step8_mosquitto_restart_failed",
            error=stderr,
            action="proceeding — check Mosquitto status manually",
            correlation_id=cid,
        )

    details["mqtt_routed"] = True
    _write_install_state(db_path, "DOCKER_MQTT_ROUTED", details, cid)
    log.info("step8_complete", correlation_id=cid)
    return True


async def _step9_create_webhook(
    ha_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Create the NetAlertX webhook automation in HA (reuses installer logic)."""
    log.info("step9_start", step="create_webhook", correlation_id=cid)
    from netalertx.installer import _step8_create_webhook_automation

    ok = await _step8_create_webhook_automation(
        ha_ssh, gate, notifier, details, cid, db_path
    )
    if ok:
        details["platform"] = "docker"
        _write_install_state(db_path, "DOCKER_WEBHOOK_CREATED", details, cid)
        _write_install_state(db_path, "FULLY_OPERATIONAL", details, cid)
        log.info("step9_complete", correlation_id=cid)
    return ok


# ── public entry point ────────────────────────────────────────────────────────


async def run_docker_installer(
    docker_ssh: "SSHClientProtocol",
    ha_ssh: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
    docker_host: str = NETALERTX_DOCKER_HOST,
    image: str = NETALERTX_DOCKER_IMAGE,
    config_path: str = NETALERTX_DOCKER_CONFIG_PATH,
    min_disk_gb: float = NETALERTX_DOCKER_MIN_DISK_GB,
    api_port: int = 20212,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Install NetAlertX FA on a separate Docker host.

    Returns the final install state string ('FULLY_OPERATIONAL' on success).
    """
    cid = str(uuid.uuid4())[:8]
    set_correlation_id(cid)
    log.info("docker_installer_start", docker_host=docker_host, correlation_id=cid)

    if not docker_host:
        raise ValueError(
            "NETALERTX_DOCKER_HOST is not set. "
            "Set netalertx.docker_host in config.yaml before running Docker setup."
        )

    state, details = _read_install_state(db_path)
    log.info("docker_installer_resume", state=state, correlation_id=cid)

    rank = _STATE_RANK.get(state, 0)
    _http = http_client or httpx.AsyncClient()
    _owns_http = http_client is None

    try:
        if rank < _STATE_RANK["DOCKER_SSH_VERIFIED"]:
            ok = await _step1_verify_docker_ssh(
                docker_ssh, gate, notifier, details, cid, db_path, docker_host
            )
            if not ok:
                return state
            state = "DOCKER_SSH_VERIFIED"

        if rank < _STATE_RANK["DOCKER_DISK_CHECKED"]:
            ok = await _step2_check_disk(
                docker_ssh,
                gate,
                notifier,
                details,
                cid,
                db_path,
                config_path,
                min_disk_gb,
            )
            if not ok:
                return state
            state = "DOCKER_DISK_CHECKED"

        if rank < _STATE_RANK["DOCKER_DIR_CREATED"]:
            ok = await _step3_create_config_dir(
                docker_ssh, gate, notifier, details, cid, db_path, config_path
            )
            if not ok:
                return state
            state = "DOCKER_DIR_CREATED"

        if rank < _STATE_RANK["DOCKER_IMAGE_PULLED"]:
            ok = await _step4_pull_image(
                docker_ssh,
                gate,
                notifier,
                details,
                cid,
                db_path,
                image,
                config_path,
                min_disk_gb,
            )
            if not ok:
                return state
            state = "DOCKER_IMAGE_PULLED"

        if rank < _STATE_RANK["DOCKER_CONFIGURED"]:
            ok = await _step5_write_app_conf(
                docker_ssh, details, cid, db_path, config_path, docker_host
            )
            if not ok:
                return state
            state = "DOCKER_CONFIGURED"

        if rank < _STATE_RANK["DOCKER_RUNNING"]:
            ok = await _step6_start_container(
                docker_ssh, gate, notifier, details, cid, db_path, image, config_path
            )
            if not ok:
                return state
            state = "DOCKER_RUNNING"

        if rank < _STATE_RANK["DOCKER_HEALTHY"]:
            ok = await _step7_wait_healthy(
                docker_host, api_port, gate, notifier, details, cid, db_path, _http
            )
            if not ok:
                return state
            state = "DOCKER_HEALTHY"

        if rank < _STATE_RANK["DOCKER_MQTT_ROUTED"]:
            ok = await _step8_route_mqtt(
                ha_ssh, gate, notifier, details, cid, db_path, docker_host
            )
            if not ok:
                return state
            state = "DOCKER_MQTT_ROUTED"

        if rank < _STATE_RANK["DOCKER_WEBHOOK_CREATED"]:
            ok = await _step9_create_webhook(
                ha_ssh, gate, notifier, details, cid, db_path
            )
            if not ok:
                return state
            state = "FULLY_OPERATIONAL"

        log.info("docker_installer_complete", state=state, correlation_id=cid)
        await notifier.send(
            subject="NetAlertX installed on Docker host",
            body=(
                f"NetAlertX Full Access is running on {docker_host}:{api_port}.\n\n"
                "Next: set netalertx.host and netalertx.api_token in config.yaml, "
                "then restart Pueo.\n\n"
                f"  netalertx:\n"
                f"    host: {docker_host!r}\n"
                f"    api_port: {api_port}\n"
                f"    api_token: <generate at http://{docker_host}:{api_port}>"
            ),
            payload={
                "card_type": "netalertx_docker_setup",
                "notification_id": f"{cid}_docker_install_done",
            },
        )
    finally:
        if _owns_http:
            await _http.aclose()

    return state


async def main(
    docker_ssh: "SSHClientProtocol | None" = None,
    ha_ssh: "SSHClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    db_path: str = DB_PATH,
) -> None:
    """Entry point for `--mode netalertx-docker-setup`."""
    from config import (
        AUTONOMY_LEVEL,
        HA_HOST,
        HA_USER,
        SSH_KEY_PATH,
        NOTIFIER,
        NOTIFY_URL,
        NOTIFY_WATCH_DIR,
    )
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

    _docker_host = NETALERTX_DOCKER_HOST
    _docker_user = NETALERTX_DOCKER_SSH_USER or HA_USER
    _docker_key = NETALERTX_DOCKER_SSH_KEY_PATH

    if not _docker_host:
        log.error(
            "docker_host_not_configured",
            action="Set netalertx.docker_host in config.yaml before running Docker setup.",
        )
        raise SystemExit(1)

    _docker_ssh = docker_ssh or AsyncSSHClient(_docker_host, _docker_user, _docker_key)
    _ha_ssh = ha_ssh or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _gate = gate or AutonomyGate(level=AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    final_state = await run_docker_installer(
        _docker_ssh,
        _ha_ssh,
        _gate,
        _notifier,
        db_path=db_path,
    )
    log.info("netalertx_docker_setup_done", state=final_state)
