"""NetAlertX autonomy-gated healing — item 18.

Dispatches repair actions for each diagnostic category, gated by AutonomyGate:
  Level 1 (Report Only)  — notify; no writes or restarts
  Level 2 (Suggest)      — require approval for every action
  Level 3 (Guided)       — auto-execute MEDIUM risk; approval for HIGH
  Level 4 (Autonomous)   — auto-execute HIGH risk; restarts without HITL

Version change detection persists last-seen NetAlertX version in netalertx_state
(migrated in _migrate_v4).  A version bump gates all automated actions at levels 1–3.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Optional

import yaml

from config import (
    CONFIG_REMOTE_PATH,
    DB_PATH,
    NETALERTX_LOG_CONTAINER_NAME,
)
from netalertx.config_validator import (
    _SNAKE_TO_CAMEL,
    validate_app_conf,
)
from utils.autonomy import RiskLevel
from utils.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.config_validator import ConfigIssue
    from netalertx.diagnosis import NetAlertXDiagnostic
    from interfaces import SSHClientProtocol
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.healer")

_CONF_PATH = "/data/app.conf"
_AUTOMATIONS_PATH = "/config/automations.yaml"


def _merge_conf(current: str, overrides: dict[str, str]) -> str:
    """Return conf text with override key=value lines applied or appended."""
    lines = current.splitlines()
    applied: set[str] = set()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key, _, _ = stripped.partition("=")
        key = key.strip()
        if key in overrides:
            result.append(f"{key}={overrides[key]}")
            applied.add(key)
        else:
            result.append(line)
    for key, value in overrides.items():
        if key not in applied:
            result.append(f"{key}={value}")
    return "\n".join(result)


def _extract_conf_overrides(recommended_fix: str) -> dict[str, str]:
    """Parse KEY=value lines from a recommended_fix string."""
    overrides: dict[str, str] = {}
    for line in recommended_fix.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            key = key.strip()
            if re.match(r"^[A-Z_]+$", key):
                overrides[key] = value.strip()
    return overrides


class NetAlertXHealer:
    """Autonomy-gated healer for NetAlertX diagnostics."""

    def __init__(
        self,
        gate: "AutonomyGate",
        ssh_client: "SSHClientProtocol",
        ha_ssh_client: "SSHClientProtocol",
        api_client: "NetAlertXAPIClient",
        notifier: "NotifierProtocol",
        container_name: str = NETALERTX_LOG_CONTAINER_NAME,
        db_path: str = DB_PATH,
    ) -> None:
        self._gate = gate
        self._ssh = ssh_client
        self._ha_ssh = ha_ssh_client
        self._api = api_client
        self._notifier = notifier
        self._container = container_name
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Version tracking
    # ------------------------------------------------------------------

    def _get_stored_version(self) -> Optional[str]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM netalertx_state WHERE key = 'netalertx_version'"
                ).fetchone()
                return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    def _store_version(self, version: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO netalertx_state (key, value) VALUES ('netalertx_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (version,),
            )
            conn.commit()

    async def check_version_bump(self, current_version: str) -> bool:
        """Return True if healing may proceed (no bump, or bump was approved).

        Persists the current version.  At levels 1–3, a version bump gates all
        subsequent actions via HIGH-risk approval.  At level 4, the bump is
        logged and healing continues.
        """
        stored = self._get_stored_version()
        self._store_version(current_version)

        if stored is None or stored == current_version:
            return True

        log.info(
            "netalertx_version_bump_detected",
            from_version=stored,
            to_version=current_version,
        )
        approved = await self._gate.require_approval(
            subject=f"Pueo: NetAlertX version changed {stored} → {current_version}",
            body=(
                f"NetAlertX upgraded from {stored} to {current_version}. "
                "Review breaking changes before automated actions proceed."
            ),
            payload={"from_version": stored, "to_version": current_version},
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if not approved:
            log.info(
                "netalertx_version_bump_blocked",
                from_version=stored,
                to_version=current_version,
            )
        return approved

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def heal(self, diagnostic: "NetAlertXDiagnostic") -> None:
        """Dispatch healing based on diagnostic category."""
        log.info(
            "netalertx_heal_start",
            category=diagnostic.category,
            severity=diagnostic.severity,
        )
        category = diagnostic.category
        if category == "networking":
            await self._heal_networking(diagnostic)
        elif category == "mqtt":
            await self._heal_mqtt(diagnostic)
        elif category == "ha_integration":
            await self._heal_ha_integration(diagnostic)
        else:
            # database, version, or unknown: notify and report only
            await self._gate.require_approval(
                subject=f"Pueo: NetAlertX {diagnostic.severity} — {diagnostic.issue}",
                body=diagnostic.recommended_fix,
                payload={"category": category, "severity": diagnostic.severity},
                notifier=self._notifier,
                risk=RiskLevel.MEDIUM,
            )

    # ------------------------------------------------------------------
    # Category handlers
    # ------------------------------------------------------------------

    async def _heal_networking(self, diagnostic: "NetAlertXDiagnostic") -> None:
        """Restart container + rescan (HIGH risk; auto at level 4)."""
        if self._gate.should_auto_execute(RiskLevel.HIGH):
            await self._restart_container()
            await self._api.trigger_scan()
            log.info("netalertx_restart_rescan_done")
            return

        approved = await self._gate.require_approval(
            subject="Pueo: NetAlertX networking issue — restart + rescan",
            body=diagnostic.recommended_fix,
            payload={"category": "networking", "severity": diagnostic.severity},
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if approved:
            await self._restart_container()
            await self._api.trigger_scan()
            log.info("netalertx_restart_rescan_done")

    async def _heal_mqtt(self, diagnostic: "NetAlertXDiagnostic") -> None:
        """app.conf rewrite (MEDIUM, auto at level 3+); HA mqtt: conflict (HIGH)."""
        if self._gate.should_auto_execute(RiskLevel.MEDIUM):
            await self._rewrite_app_conf(diagnostic)
        else:
            approved = await self._gate.require_approval(
                subject="Pueo: NetAlertX MQTT — rewrite app.conf",
                body=diagnostic.recommended_fix,
                payload={"category": "mqtt", "action": "rewrite_app_conf"},
                notifier=self._notifier,
                risk=RiskLevel.MEDIUM,
            )
            if not approved:
                return
            await self._rewrite_app_conf(diagnostic)

        # HA configuration.yaml mqtt: key removal (HIGH risk)
        approved_ha = await self._gate.require_approval(
            subject="Pueo: NetAlertX — remove conflicting 'mqtt:' key from HA config",
            body=(
                "HA configuration.yaml has a top-level 'mqtt:' key that blocks "
                "MQTT auto-discovery. Remove it and reconfigure via UI."
            ),
            payload={"category": "mqtt", "action": "remove_ha_mqtt_key"},
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if approved_ha:
            await self._remove_ha_mqtt_key()

    async def _heal_ha_integration(self, diagnostic: "NetAlertXDiagnostic") -> None:
        """Fix HA automation webhook field names to camelCase (HIGH risk)."""
        approved = await self._gate.require_approval(
            subject="Pueo: NetAlertX — fix HA automation webhook fields to camelCase",
            body=diagnostic.recommended_fix,
            payload={"category": "ha_integration"},
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if approved:
            await self._fix_ha_automation_fields()

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    async def _rewrite_app_conf(self, diagnostic: "NetAlertXDiagnostic") -> None:
        log.info("netalertx_app_conf_rewrite_start")
        try:
            current = await self._ssh.read_file(_CONF_PATH)
        except (FileNotFoundError, OSError):
            current = ""

        overrides = _extract_conf_overrides(diagnostic.recommended_fix)
        fixed = _merge_conf(current, overrides) if overrides else current

        issues = validate_app_conf(fixed)
        blocking = [i for i in issues if i.severity in ("HIGH", "CRITICAL")]
        if blocking and not overrides:
            log.error(
                "netalertx_app_conf_still_invalid",
                count=len(blocking),
            )
            return

        await self._ssh.write_file(_CONF_PATH, fixed)
        log.info("netalertx_app_conf_rewrite_done")

    async def _remove_ha_mqtt_key(self) -> None:
        """Remove top-level 'mqtt:' key from HA configuration.yaml via sandbox engine."""
        from ha_agent_sandbox_engine import (
            commit_atomic_swap,
            deploy_and_test_in_sandbox,
            execute_remote_backup,
            record_backup_slug,
        )

        log.info("netalertx_remove_ha_mqtt_key_start")
        try:
            ha_config = await self._ha_ssh.read_file(CONFIG_REMOTE_PATH)
        except (FileNotFoundError, OSError) as exc:
            log.error("netalertx_ha_config_read_failed", error=str(exc))
            return

        try:
            parsed = yaml.safe_load(ha_config) or {}
        except yaml.YAMLError as exc:
            log.error("netalertx_ha_config_parse_failed", error=str(exc))
            return

        if "mqtt" not in parsed:
            log.info("netalertx_ha_mqtt_key_absent")
            return

        parsed.pop("mqtt")
        fixed_yaml = yaml.dump(parsed, default_flow_style=False, allow_unicode=True)

        slug = await execute_remote_backup(ssh_client=self._ha_ssh)
        record_backup_slug(slug)

        if await deploy_and_test_in_sandbox(fixed_yaml, ssh_client=self._ha_ssh):
            await commit_atomic_swap(fixed_yaml, ssh_client=self._ha_ssh)
            log.info("netalertx_ha_mqtt_key_removed")
        else:
            log.error("netalertx_ha_sandbox_failed")

    async def _fix_ha_automation_fields(self) -> None:
        """Replace snake_case webhook fields with camelCase in HA automations."""
        from ha_agent_sandbox_engine import execute_remote_backup, record_backup_slug

        log.info("netalertx_fix_ha_automation_start")
        try:
            content = await self._ha_ssh.read_file(_AUTOMATIONS_PATH)
        except (FileNotFoundError, OSError) as exc:
            log.error("netalertx_ha_automations_read_failed", error=str(exc))
            return

        fixed = content
        for snake, camel in _SNAKE_TO_CAMEL.items():
            fixed = re.sub(rf"\b{re.escape(snake)}\b", camel, fixed)

        if fixed == content:
            log.info("netalertx_ha_automation_no_changes")
            return

        slug = await execute_remote_backup(ssh_client=self._ha_ssh)
        record_backup_slug(slug)

        await self._ha_ssh.write_file(_AUTOMATIONS_PATH, fixed)
        await self._ha_ssh.run("ha core restart")
        log.info("netalertx_ha_automation_fields_fixed")

    async def _restart_container(self) -> None:
        log.info("netalertx_container_restart_start", container=self._container)
        await self._ssh.run(f"docker restart {self._container}")
        log.info("netalertx_container_restart_done")

    # ------------------------------------------------------------------
    # Item 19: maintenance issue healing
    # ------------------------------------------------------------------

    async def heal_maintenance_issues(self, config_issues: list["ConfigIssue"]) -> None:
        """Process ConfigIssues from item 19 maintenance checks.

        - Webhook field snake_case → camelCase fix (HIGH risk gate)
        - MQTT entity divergence → notify only at all levels
        - DB row count excess → DBCLNP cleanup at level 4 only
        """
        _webhook_fields = set(_SNAKE_TO_CAMEL.keys())
        _db_metrics = {"Plugins_History", "Events"}

        webhook_issues = [i for i in config_issues if i.field in _webhook_fields]
        divergence_issues = [
            i for i in config_issues if i.field == "mqtt_entity_divergence"
        ]
        db_issues = [i for i in config_issues if i.field in _db_metrics]

        if webhook_issues:
            await self._heal_webhook_fields(webhook_issues)

        for issue in divergence_issues:
            log.info("netalertx_mqtt_divergence_notified", detail=issue.message)
            await self._notifier.send(
                subject="Pueo: NetAlertX MQTT entity divergence",
                body=issue.message,
                payload={"field": issue.field, "severity": issue.severity},
            )

        if db_issues and self._gate.should_auto_execute(RiskLevel.HIGH):
            await self._api.trigger_scan("DBCLNP")
            log.info("netalertx_dbclnp_triggered", issue_count=len(db_issues))

    async def _heal_webhook_fields(self, issues: list["ConfigIssue"]) -> None:
        """Fix webhook automation snake_case fields to camelCase (HIGH risk)."""
        if self._gate.should_auto_execute(RiskLevel.HIGH):
            await self._fix_ha_automation_fields()
            return

        fields_str = ", ".join(i.field for i in issues)
        approved = await self._gate.require_approval(
            subject="Pueo: NetAlertX — fix HA automation webhook fields to camelCase",
            body=(
                f"Snake_case webhook fields detected: {fields_str}. "
                "Rewrite to camelCase via HA sandbox engine."
            ),
            payload={"fields": [i.field for i in issues]},
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if approved:
            await self._fix_ha_automation_fields()
