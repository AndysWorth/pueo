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

import asyncio
import json
import re
import sqlite3
import uuid
from typing import TYPE_CHECKING

import httpx

from config import (
    CONFIG_REMOTE_PATH,
    DB_PATH,
    HA_API_TOKEN,
    HA_HOST,
    NETALERTX_ADDON_REPOSITORY_URL,
    NETALERTX_ADDON_SLUG,
    NETALERTX_API_PORT,
    NETALERTX_HOST,
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


# ── steps 5–8 helpers ────────────────────────────────────────────────────────


async def _poll_addon_not_state(
    ssh_client: "SSHClientProtocol",
    addon_id: str,
    excluded_state: str,
    attempts: int = 60,
    delay: float = 5.0,
) -> bool:
    """Poll until state != excluded_state (used to detect install completion)."""
    pattern = re.compile(r"state:\s*(\S+)", re.IGNORECASE)
    for _ in range(attempts):
        _, stdout, _ = await ssh_client.run(f"ha addons info {addon_id}")
        m = pattern.search(stdout)
        if m and m.group(1).lower() != excluded_state.lower():
            return True
        await asyncio.sleep(delay)
    return False


def _parse_data_path(info_output: str) -> str:
    """Parse the add-on data directory from `ha addons info` output (`data:` field)."""
    m = re.search(r"^\s*data:\s*(.+)$", info_output, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _merge_plugins(existing_val: str, required: list[str]) -> str:
    """Return a Python list literal with required plugins merged into existing_val."""
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


def _merge_app_conf(
    original: str, updates: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """Merge key=value updates into app.conf content.

    Returns (merged_content, diff) where diff maps key → 'old_val -> new_val'.
    """
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


async def _detect_subnet(ssh_client: "SSHClientProtocol", interface: str) -> str:
    """Return the IPv4 subnet CIDR for an interface (e.g. '192.168.1.0/24')."""
    import ipaddress

    _, stdout, _ = await ssh_client.run(f"ip addr show {interface}")
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", stdout)
    if not m:
        return ""
    try:
        return str(ipaddress.ip_interface(m.group(1)).network)
    except ValueError:
        return ""


def _check_automation_exists(content: str) -> bool:
    """Return True if a NetAlertX webhook automation is already in the YAML content."""
    low = content.lower()
    return "netalertx" in low and "platform: webhook" in low


_WEBHOOK_AUTOMATION_YAML = """\
- id: 'netalertx_event_handler'
  alias: 'NetAlertX Event Handler'
  description: 'Handles NetAlertX network events via webhook (created by Pueo)'
  trigger:
    - platform: webhook
      webhook_id: 'netalertx_event'
      local_only: true
  condition: []
  action:
    - service: persistent_notification.create
      data:
        title: "NetAlertX: {{ trigger.json.eveEventType }}"
        message: >
          MAC: {{ trigger.json.eveMac }}  IP: {{ trigger.json.eveIp }}
          Vendor: {{ trigger.json.devVendor }}  Time: {{ trigger.json.eveDateTime }}
          Notes: {{ trigger.json.devComments }}
  mode: queued
  max: 10
"""


# ── step 5: install and start the NetAlertX add-on ───────────────────────────


async def _step5_install_addon(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Install and start the NetAlertX add-on.

    Advances state through ADDON_INSTALLED → ADDON_RUNNING.
    """
    log.info("step5_start", step="install_addon", correlation_id=cid)

    slug = NETALERTX_ADDON_SLUG or details.get("addon_slug", "")
    if not slug:
        await gate.require_approval(
            subject="NetAlertX installer: add-on slug unknown",
            body=(
                "The NetAlertX add-on slug could not be determined from step 4. "
                "Set netalertx.addon_slug in config.yaml and re-run setup."
            ),
            payload={"notification_id": f"{cid}_step5_no_slug", "step": 5},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        log.error("step5_no_slug", correlation_id=cid)
        return False

    current_db_state, _ = _read_install_state(db_path)

    # ── sub-step A: install (skip if already at ADDON_INSTALLED or beyond) ────
    if _STATE_RANK[current_db_state] < _STATE_RANK["ADDON_INSTALLED"]:
        _, info_out, _ = await ssh_client.run(f"ha addons info {slug}")
        state_m = re.search(r"state:\s*(\S+)", info_out, re.IGNORECASE)
        addon_state = state_m.group(1).lower() if state_m else "unknown"

        if addon_state == "unknown":
            log.info("step5_installing", slug=slug, correlation_id=cid)
            await ssh_client.run(f"ha addons install {slug}", check=True)
            installed = await _poll_addon_not_state(
                ssh_client, slug, "unknown", attempts=60, delay=5.0
            )
            if not installed:
                log.error("step5_install_timeout", slug=slug, correlation_id=cid)
                await gate.require_approval(
                    subject="NetAlertX installer: add-on install timed out",
                    body=(
                        f"Add-on {slug!r} did not complete installation within 5 minutes. "
                        "Check Supervisor logs and re-run setup."
                    ),
                    payload={
                        "notification_id": f"{cid}_step5_install_timeout",
                        "step": 5,
                    },
                    notifier=notifier,
                    risk=RiskLevel.CRITICAL,
                )
                return False
        else:
            log.info(
                "step5_already_installed",
                slug=slug,
                addon_state=addon_state,
                correlation_id=cid,
            )

        _write_install_state(db_path, "ADDON_INSTALLED", details, cid)
        current_db_state = "ADDON_INSTALLED"

    # ── sub-step B: start (skip if already ADDON_RUNNING) ────────────────────
    if _STATE_RANK[current_db_state] < _STATE_RANK["ADDON_RUNNING"]:
        _, info_out, _ = await ssh_client.run(f"ha addons info {slug}")
        state_m = re.search(r"state:\s*(\S+)", info_out, re.IGNORECASE)
        addon_state = state_m.group(1).lower() if state_m else "unknown"

        if addon_state != "running":
            log.info("step5_starting", slug=slug, correlation_id=cid)
            await ssh_client.run(f"ha addons start {slug}", check=True)

        running = await _poll_addon_state(
            ssh_client, slug, "running", attempts=60, delay=5.0
        )
        if not running:
            log.error("step5_start_timeout", slug=slug, correlation_id=cid)
            await gate.require_approval(
                subject="NetAlertX installer: add-on start timed out",
                body=(
                    f"Add-on {slug!r} did not reach running state within 5 minutes. "
                    "Check Supervisor logs and re-run setup."
                ),
                payload={
                    "notification_id": f"{cid}_step5_start_timeout",
                    "step": 5,
                },
                notifier=notifier,
                risk=RiskLevel.CRITICAL,
            )
            return False

        _write_install_state(db_path, "ADDON_RUNNING", details, cid)

    log.info("step5_complete", slug=slug, correlation_id=cid)
    return True


# ── step 6: configure app.conf ────────────────────────────────────────────────


async def _step6_configure_app_conf(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    http_client: httpx.AsyncClient,
) -> bool:
    """Merge and write app.conf into the NetAlertX add-on data directory.

    Advances state to NETALERTX_CONFIGURED.
    """
    log.info("step6_start", step="configure_app_conf", correlation_id=cid)

    slug = NETALERTX_ADDON_SLUG or details.get("addon_slug", "")

    # Locate the add-on data directory
    _, info_out, _ = await ssh_client.run(f"ha addons info {slug}")
    data_path = _parse_data_path(info_out)
    if not data_path:
        log.error("step6_no_data_path", slug=slug, correlation_id=cid)
        await gate.require_approval(
            subject="NetAlertX installer: cannot locate app.conf",
            body=(
                f"Could not determine data directory for add-on {slug!r}. "
                "Check Supervisor status and re-run setup."
            ),
            payload={"notification_id": f"{cid}_step6_no_data_path", "step": 6},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False

    conf_path = f"{data_path}/app.conf"

    try:
        original_conf = await ssh_client.read_file(conf_path)
    except Exception:
        original_conf = ""

    # Detect subnet for the scan interface
    interface = details.get("scan_interface", "")
    if interface:
        subnet = await _detect_subnet(ssh_client, interface)
        scan_subnets_val = (
            f"['{subnet}   {interface}']" if subnet else f"['   {interface}']"
        )
    else:
        scan_subnets_val = "[]"

    # Read HA timezone from configuration.yaml
    try:
        ha_conf = await ssh_client.read_file(CONFIG_REMOTE_PATH)
        tz_m = re.search(
            r"homeassistant:\s*\n(?:[^\n]*\n)*?\s*time_zone:\s*['\"]?([^\s'\"]+)",
            ha_conf,
        )
        timezone = tz_m.group(1) if tz_m else "UTC"
    except Exception:
        timezone = "UTC"

    # Merge LOADED_PLUGINS — preserve existing plugins
    plugins_m = re.search(r"LOADED_PLUGINS\s*=\s*(\[.*?\])", original_conf, re.DOTALL)
    plugins_val = plugins_m.group(1) if plugins_m else "[]"
    merged_plugins = _merge_plugins(plugins_val, ["MQTT", "ARPSCAN"])

    updates: dict[str, str] = {
        "MQTT_BROKER": f"'{NETALERTX_HOST}'",
        "MQTT_PORT": "1883",
        "HA_URL": f"'http://{HA_HOST}:8123'",
        "HA_BEARER_TOKEN": f"'{HA_API_TOKEN}'",
        "SCAN_SUBNETS": scan_subnets_val,
        "TIMEZONE": f"'{timezone}'",
        "LOADED_PLUGINS": merged_plugins,
    }

    merged_conf, diff = _merge_app_conf(original_conf, updates)

    # Backup before write (safety invariant)
    from ha_agent_sandbox_engine import execute_remote_backup

    try:
        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        details["app_conf_backup_slug"] = backup_slug
        log.info("step6_backup_created", backup_slug=backup_slug, correlation_id=cid)
    except Exception as e:
        log.error("step6_backup_failed", error=str(e), correlation_id=cid)
        await gate.require_approval(
            subject="NetAlertX installer: backup failed before app.conf write",
            body=f"HA backup failed ({e}). Aborting to protect HA state.",
            payload={"notification_id": f"{cid}_step6_backup_fail", "step": 6},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False

    await ssh_client.write_file(conf_path, merged_conf)
    log.info("step6_app_conf_written", diff=diff, path=conf_path, correlation_id=cid)

    # Restart and verify
    await ssh_client.run(f"ha addons restart {slug}", check=True)
    running = await _poll_addon_state(ssh_client, slug, "running")
    if not running:
        log.error("step6_restart_timeout", slug=slug, correlation_id=cid)
        await gate.require_approval(
            subject="NetAlertX installer: add-on failed to restart",
            body=f"Add-on {slug!r} did not return to running state after app.conf write.",
            payload={"notification_id": f"{cid}_step6_restart_fail", "step": 6},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False

    verify_url = f"http://{NETALERTX_HOST}:{NETALERTX_API_PORT}/health"
    try:
        resp = await http_client.get(verify_url, timeout=30)
        if resp.status_code == 200:
            log.info("step6_api_verified", url=verify_url, correlation_id=cid)
        else:
            log.warning(
                "step6_api_unexpected_status",
                status=resp.status_code,
                correlation_id=cid,
            )
    except Exception as e:
        log.warning(
            "step6_api_check_failed", error=str(e), url=verify_url, correlation_id=cid
        )

    _write_install_state(db_path, "NETALERTX_CONFIGURED", details, cid)
    log.info("step6_complete", correlation_id=cid)
    return True


# ── step 7: verify HA MQTT integration ───────────────────────────────────────


async def _step7_verify_mqtt_integration(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
    http_client: httpx.AsyncClient,
) -> bool:
    """Check that the HA MQTT integration is configured (UI-only step).

    If not configured, requests HITL with manual instructions.
    Advances state to HA_MQTT_INTEGRATION_VERIFIED.
    """
    log.info("step7_start", step="verify_mqtt_integration", correlation_id=cid)

    entries_url = f"http://{HA_HOST}:8123/api/config/config_entries"

    async def _mqtt_configured() -> bool:
        try:
            resp = await http_client.get(
                entries_url,
                headers={"Authorization": f"Bearer {HA_API_TOKEN}"},
                timeout=10,
            )
            if resp.status_code != 200:
                return False
            entries = resp.json()
            return isinstance(entries, list) and any(
                e.get("domain") == "mqtt" for e in entries
            )
        except Exception:
            return False

    if await _mqtt_configured():
        log.info("step7_mqtt_found", correlation_id=cid)
        _write_install_state(db_path, "HA_MQTT_INTEGRATION_VERIFIED", details, cid)
        return True

    # Not found — send HITL with manual setup instructions
    approved = await gate.require_approval(
        subject="NetAlertX installer: configure MQTT integration in HA",
        body=(
            "HA MQTT integration is not yet configured. Set it up manually:\n"
            "  1. Settings → Devices & Services → Add Integration\n"
            "  2. Search for 'MQTT'\n"
            f"  3. Broker: {HA_HOST}, Port: 1883, no credentials\n"
            "  4. Save, then signal approval to continue.\n\n"
            "Note: do NOT add `mqtt:` to configuration.yaml — "
            "that key disables MQTT auto-discovery on current HA."
        ),
        payload={"notification_id": f"{cid}_step7_mqtt", "step": 7},
        notifier=notifier,
        risk=RiskLevel.LOW,
    )
    if not approved:
        log.warning("step7_mqtt_rejected", correlation_id=cid)
        return False

    # Recheck after user approval
    if await _mqtt_configured():
        log.info("step7_mqtt_verified_after_approval", correlation_id=cid)
    else:
        # User approved but API not yet reflecting — proceed (user confirmed manually)
        log.warning(
            "step7_mqtt_not_detected_after_approval",
            note="proceeding on user approval",
            correlation_id=cid,
        )

    _write_install_state(db_path, "HA_MQTT_INTEGRATION_VERIFIED", details, cid)
    return True


# ── step 8: create HA webhook automation ─────────────────────────────────────


async def _step8_create_webhook_automation(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    details: dict,
    cid: str,
    db_path: str,
) -> bool:
    """Write a NetAlertX webhook automation to HA.

    Tries /config/automations.yaml first; falls back to
    /config/automations/netalertx_webhook.yaml.  Uses backup → write →
    ha core check → revert-on-failure pattern.

    Advances state to HA_AUTOMATION_CREATED then FULLY_OPERATIONAL.
    """
    log.info("step8_start", step="create_webhook_automation", correlation_id=cid)

    # Determine where to write the automation
    automations_path = "/config/automations.yaml"
    original_content: str | None = None
    try:
        original_content = await ssh_client.read_file(automations_path)
    except FileNotFoundError:
        # Fall back to per-file directory style
        automations_path = "/config/automations/netalertx_webhook.yaml"
        try:
            original_content = await ssh_client.read_file(automations_path)
        except FileNotFoundError:
            original_content = None

    # Idempotency: skip if automation already present
    if original_content is not None and _check_automation_exists(original_content):
        log.info("step8_automation_exists", path=automations_path, correlation_id=cid)
        _write_install_state(db_path, "HA_AUTOMATION_CREATED", details, cid)
        _write_install_state(db_path, "FULLY_OPERATIONAL", details, cid)
        return True

    # Backup before write
    from ha_agent_sandbox_engine import execute_remote_backup

    try:
        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        details["automation_backup_slug"] = backup_slug
        log.info("step8_backup_created", backup_slug=backup_slug, correlation_id=cid)
    except Exception as e:
        log.error("step8_backup_failed", error=str(e), correlation_id=cid)
        await gate.require_approval(
            subject="NetAlertX installer: backup failed before automation write",
            body=f"HA backup failed ({e}). Aborting to protect HA state.",
            payload={"notification_id": f"{cid}_step8_backup_fail", "step": 8},
            notifier=notifier,
            risk=RiskLevel.CRITICAL,
        )
        return False

    # Build new content
    if original_content:
        new_content = original_content.rstrip("\n") + "\n\n" + _WEBHOOK_AUTOMATION_YAML
    else:
        new_content = _WEBHOOK_AUTOMATION_YAML

    await ssh_client.write_file(automations_path, new_content)

    # Validate with ha core check; restore on failure
    ec, stdout, stderr = await ssh_client.run("ha core check")
    if ec != 0:
        log.error(
            "step8_ha_check_failed",
            output=stderr or stdout,
            correlation_id=cid,
        )
        restore = original_content if original_content is not None else ""
        await ssh_client.write_file(automations_path, restore)
        await gate.require_approval(
            subject="NetAlertX installer: automation YAML failed HA check",
            body=(
                "The generated webhook automation did not pass `ha core check`. "
                "Original automations file has been restored. "
                "Add the automation manually and signal approval to continue."
            ),
            payload={"notification_id": f"{cid}_step8_check_fail", "step": 8},
            notifier=notifier,
            risk=RiskLevel.HIGH,
        )
        return False

    await ssh_client.run("ha core restart")

    webhook_url = f"http://{HA_HOST}:8123/api/webhook/netalertx_event"
    log.info(
        "step8_webhook_url",
        url=webhook_url,
        note="configure this URL in NetAlertX HA_WEBHOOK_URL",
        correlation_id=cid,
    )

    _write_install_state(db_path, "HA_AUTOMATION_CREATED", details, cid)
    _write_install_state(db_path, "FULLY_OPERATIONAL", details, cid)
    log.info("step8_complete", correlation_id=cid)
    return True


# ── public entry point ────────────────────────────────────────────────────────


async def run_steps_5_to_8(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Execute installer steps 5–8, resuming from the current persisted state.

    Returns the final state string after completion or on failure.
    """
    cid = str(uuid.uuid4())
    set_correlation_id(cid)

    _http = http_client or httpx.AsyncClient()

    current_state, details = _read_install_state(db_path)
    log.info(
        "installer_start",
        current_state=current_state,
        steps="5-8",
        correlation_id=cid,
    )

    # Step 5: ADDON_REPO_ADDED / ADDON_INSTALLED → ADDON_RUNNING
    if _STATE_RANK[current_state] < _STATE_RANK["ADDON_RUNNING"]:
        ok = await _step5_install_addon(
            ssh_client, gate, notifier, details, cid, db_path
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, details = _read_install_state(db_path)

    # Step 6: ADDON_RUNNING → NETALERTX_CONFIGURED
    if _STATE_RANK[current_state] < _STATE_RANK["NETALERTX_CONFIGURED"]:
        ok = await _step6_configure_app_conf(
            ssh_client, gate, notifier, details, cid, db_path, _http
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, details = _read_install_state(db_path)

    # Step 7: NETALERTX_CONFIGURED → HA_MQTT_INTEGRATION_VERIFIED
    if _STATE_RANK[current_state] < _STATE_RANK["HA_MQTT_INTEGRATION_VERIFIED"]:
        ok = await _step7_verify_mqtt_integration(
            ssh_client, gate, notifier, details, cid, db_path, _http
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, details = _read_install_state(db_path)

    # Step 8: HA_MQTT_INTEGRATION_VERIFIED → HA_AUTOMATION_CREATED → FULLY_OPERATIONAL
    if _STATE_RANK[current_state] < _STATE_RANK["FULLY_OPERATIONAL"]:
        ok = await _step8_create_webhook_automation(
            ssh_client, gate, notifier, details, cid, db_path
        )
        if not ok:
            return _read_install_state(db_path)[0]
        current_state, _ = _read_install_state(db_path)

    log.info(
        "installer_steps_5_to_8_complete",
        final_state=current_state,
        correlation_id=cid,
    )
    return current_state


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


async def run_installer(
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    db_path: str = DB_PATH,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Execute all 8 installer steps, resuming from the current persisted state.

    Returns the final state string.
    """
    state = await run_steps_1_to_4(ssh_client, gate, notifier, db_path, http_client)
    if _STATE_RANK.get(state, -1) < _STATE_RANK["ADDON_REPO_ADDED"]:
        return state
    return await run_steps_5_to_8(ssh_client, gate, notifier, db_path, http_client)


async def main(
    ssh_client: "SSHClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    db_path: str = DB_PATH,
) -> None:
    """Entry point for `--mode netalertx-setup`."""
    from config import (
        AUTONOMY_LEVEL,
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
    _gate = gate or AutonomyGate(level=AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    async with httpx.AsyncClient() as _http:
        final_state = await run_installer(
            _ssh, _gate, _notifier, db_path=db_path, http_client=_http
        )
    log.info("netalertx_setup_done", state=final_state)

    if final_state == "FULLY_OPERATIONAL":
        from config import NETALERTX_API_TOKEN, NETALERTX_API_PORT, NETALERTX_HOST
        from netalertx.api_client import NetAlertXAPIClient
        from netalertx.ha_name_sync import HaNameSync

        _api = NetAlertXAPIClient(
            base_url=f"http://{NETALERTX_HOST}:{NETALERTX_API_PORT}",
            api_token=NETALERTX_API_TOKEN,
        )
        _syncer = HaNameSync(
            ssh_client=_ssh,
            api_client=_api,
            gate=_gate,
            notifier=_notifier,
        )
        report = await _syncer.sync_names()
        log.info(
            "ha_name_sync_done",
            written=len(report.written),
            locked=len(report.locked),
            conflicted=len(report.conflicted),
            unnamed=len(report.unnamed),
        )
