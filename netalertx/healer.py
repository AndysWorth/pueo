"""NetAlertX autonomy-gated healing — items 18 and 46.

heal() dispatches repair actions by category (linear pipeline, backward-compatible).
heal_with_loop() replaces the linear pipeline with AgentLoop for iterative
tool-calling repair (item 46). When an LLM client is provided in production,
one_shot_diagnose.py calls heal_with_loop() instead of heal().

Version change detection persists last-seen NetAlertX version in netalertx_state
(migrated in _migrate_v4).  A version bump gates all automated actions at levels 1–3.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Optional

import config as _config
from config import (
    DB_PATH,
    NETALERTX_LOG_CONTAINER_NAME,
)
from netalertx.config_validator import (
    validate_app_conf,
)
from utils.agent.autonomy import RiskLevel
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.config_validator import ConfigIssue
    from netalertx.diagnosis import NetAlertXDiagnostic
    from interfaces import (
        HARestClientProtocol,
        HAWebSocketClientProtocol,
        KnowledgeStoreClientProtocol,
        LLMClientProtocol,
        SSHClientProtocol,
    )
    from utils.agent.autonomy import AutonomyGate
    from utils.ha.ha_environment import HAEnvironmentProfile
    from utils.hitl.notify import NotifierProtocol

log = get_logger("netalertx.healer")

_CONF_PATH_DOCKER = "/data/app.conf"


def _addon_conf_path(slug: str) -> str:
    return f"/app_configs/{slug}/config/app.conf"


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
    """Autonomy-gated healer for NetAlertX diagnostics.

    heal() uses the original linear category-dispatch pipeline.
    heal_with_loop() replaces it with an AgentLoop (item 46).
    """

    def __init__(
        self,
        gate: "AutonomyGate",
        ssh_client: "SSHClientProtocol",
        ha_ssh_client: "SSHClientProtocol",
        api_client: "NetAlertXAPIClient",
        notifier: "NotifierProtocol",
        container_name: str = NETALERTX_LOG_CONTAINER_NAME,
        db_path: str = DB_PATH,
        knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
        rest_client: Optional["HARestClientProtocol"] = None,
        ws_client: Optional["HAWebSocketClientProtocol"] = None,
        ha_profile: Optional["HAEnvironmentProfile"] = None,
    ) -> None:
        self._gate = gate
        self._ssh = ssh_client
        self._ha_ssh = ha_ssh_client
        self._api = api_client
        self._notifier = notifier
        self._container = container_name
        self._db_path = db_path
        self._knowledge_store = knowledge_store
        self._rest_client = rest_client
        self._ws_client = ws_client
        self._ha_profile = ha_profile

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
    # AgentLoop-based healing — item 46
    # ------------------------------------------------------------------

    async def heal_with_loop(
        self,
        diagnostic: "NetAlertXDiagnostic",
        llm_client: "LLMClientProtocol",
    ) -> None:
        """Replace the linear category-dispatch with an iterative AgentLoop.

        The model investigates via query_netalertx / read_logs, then calls
        the appropriate fix tool (restart_netalertx, rewrite_netalertx_conf,
        or apply_fix for HA config changes) before finishing.
        """
        from utils.agent.agent_loop import AgentLoop
        from utils.agent.tool_executor import ToolExecutor
        from utils.agent.tool_registry import build_netalertx_tool_registry

        executor = ToolExecutor(
            ha_ssh_client=self._ha_ssh,
            gate=self._gate,
            notifier=self._notifier,
            nax_ssh_client=self._ssh,
            netalertx_api_client=self._api,
            netalertx_container_name=self._container,
            knowledge_store=self._knowledge_store,
            db_path=self._db_path,
            ha_ws_client=self._ws_client,
            ha_rest_client=self._rest_client,
        )
        if self._ha_profile is not None:
            executor.set_ha_profile(self._ha_profile)
        registry = build_netalertx_tool_registry()
        try:
            from utils.agent.supervisor import (
                clear_llm_active as _clear_llm,
                set_llm_active as _set_llm,
            )

            def _on_nax_llm_start(model: str, trigger: str) -> None:
                _set_llm("netalertx_repair", model)

            def _on_nax_llm_done(model: str, latency_ms: float) -> None:
                _clear_llm("netalertx_repair")

        except Exception:  # nosec B110
            _on_nax_llm_start = None  # type: ignore[assignment]
            _on_nax_llm_done = None  # type: ignore[assignment]

        from utils.agent.supervisor import (
            decrement_active_agent,
            increment_active_agent,
            make_activity_timeline_callback,
            publish_activity_done,
        )

        loop = AgentLoop(
            llm_client=llm_client,
            tool_executor=executor,
            tool_registry=registry,
            model=_config.OLLAMA_MODEL,
            trigger="netalertx",
            activity_type="netalertx_repair",
            db_path=self._db_path,
            knowledge_store=self._knowledge_store,
            on_llm_call_start=_on_nax_llm_start,
            on_llm_call_done=_on_nax_llm_done,
            capture_llm=True,
            timeline_callback=make_activity_timeline_callback(
                "netalertx_repair", trigger="NetAlertX device event"
            ),
        )

        from netalertx.installer import resolve_addon_slug

        _slug = resolve_addon_slug()
        _slug_line = (
            f"  Add-on slug: {_slug}"
            if _slug
            else "  Add-on slug: unknown (Docker mode)"
        )
        _conf_line = (
            f"  App conf path: /app_configs/{_slug}/config/app.conf"
            if _slug
            else "  App conf path: /data/app.conf (Docker)"
        )

        initial_context = (
            "You are diagnosing and repairing a NetAlertX network monitoring issue.\n\n"
            f"Diagnosis:\n"
            f"  Issue: {diagnostic.issue}\n"
            f"  Severity: {diagnostic.severity}\n"
            f"  Category: {diagnostic.category}\n"
            f"  Recommended fix: {diagnostic.recommended_fix}\n"
            f"  Affected version: {diagnostic.affected_netalertx_version}\n\n"
            f"NetAlertX instance:\n"
            f"{_slug_line}\n"
            f"{_conf_line}\n\n"
            "Investigate using query_netalertx and read_logs, then apply the appropriate fix. "
            "Call finish_repair when done."
        )

        log.info(
            "netalertx_heal_loop_start",
            category=diagnostic.category,
            severity=diagnostic.severity,
        )
        from utils.knowledge.runbook_signature import sig_netalertx

        _sig = sig_netalertx(diagnostic.category)
        increment_active_agent()
        try:
            result = await loop.run(initial_context, signature=_sig)
            publish_activity_done("netalertx_repair", result.outcome)
        finally:
            decrement_active_agent()
        log.info(
            "netalertx_heal_loop_complete",
            outcome=result.outcome,
            steps=len(result.steps),
        )

        # Send a card for every outcome except clean success (outcome == "success"
        # with no further action needed).  Failures and needs_action are always
        # reported; success is reported as informational so the user can see what changed.
        await self._send_heal_outcome_card(diagnostic, result)

        # Autonomous gap detection (item 84): mirror the HA loop behaviour.
        if result.capability_gap and result.gap_description:
            from agents.ha_agent_sandbox_engine import _run_code_proposal_loop

            log.info(
                "gap_detection_triggered",
                gap_description=result.gap_description[:100],
            )
            await _run_code_proposal_loop(
                gap_description=result.gap_description,
                ssh_client=self._ha_ssh,
                gate=self._gate,
                notifier=self._notifier,
                llm_client=llm_client,
                db_path=self._db_path,
            )

    # ------------------------------------------------------------------
    # Heal outcome card
    # ------------------------------------------------------------------

    async def _send_heal_outcome_card(
        self,
        diagnostic: "NetAlertXDiagnostic",
        result: "object",
    ) -> None:
        """Send a CARD_TYPE_NETALERTX_HEAL card summarising the heal outcome."""
        import sqlite3

        from utils.hitl.card_types import CARD_TYPE_NETALERTX_HEAL
        from utils.hitl.hitl_tracker import mark_card_sent, should_send_card, stable_nid

        outcome = getattr(result, "outcome", "unknown")
        steps = len(getattr(result, "steps", []))
        episode_id = getattr(result, "episode_id", "")

        card_key = (
            f"netalertx_heal:{diagnostic.category}:{diagnostic.severity}:{outcome}"
        )
        nid = stable_nid(card_key)

        if outcome == "success":
            subject = f"Pueo: NetAlertX heal succeeded — {diagnostic.issue[:60]}"
            body = (
                f"Category: {diagnostic.category}  Severity: {diagnostic.severity}\n"
                f"Steps taken: {steps}\n"
                f"Outcome: success"
            )
        else:
            subject = f"Pueo: NetAlertX heal {outcome} — {diagnostic.issue[:60]}"
            body = (
                f"Category: {diagnostic.category}  Severity: {diagnostic.severity}\n"
                f"Steps taken: {steps}\n"
                f"Outcome: {outcome}\n\n"
                "Manual review may be needed."
            )

        try:
            with sqlite3.connect(self._db_path) as conn:
                if should_send_card(conn, card_key):
                    await self._notifier.send(
                        subject=subject,
                        body=body,
                        payload={
                            "notification_id": nid,
                            "card_type": CARD_TYPE_NETALERTX_HEAL,
                            "category": diagnostic.category,
                            "severity": diagnostic.severity,
                            "outcome": outcome,
                            "issue": diagnostic.issue,
                            "episode_id": episode_id,
                        },
                    )
                    mark_card_sent(conn, card_key, CARD_TYPE_NETALERTX_HEAL, subject)
                    log.info(
                        "netalertx_heal_outcome_card_sent",
                        outcome=outcome,
                        card_key=card_key,
                    )
        except Exception as exc:  # nosec B110
            log.error("netalertx_heal_outcome_card_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Main entry point (linear pipeline — backward-compatible)
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
        else:
            # config, database, version, ha_integration, availability: notify and report only
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

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    async def _rewrite_app_conf(self, diagnostic: "NetAlertXDiagnostic") -> None:
        from netalertx.app_conf import AppConfSyntaxError, format_value, write_app_conf
        from netalertx.installer import resolve_addon_slug

        log.info("netalertx_app_conf_rewrite_start")
        slug = resolve_addon_slug()
        if not slug:
            log.error("netalertx_app_conf_rewrite_no_slug")
            return

        overrides = _extract_conf_overrides(diagnostic.recommended_fix)
        if not overrides:
            log.warning("netalertx_app_conf_rewrite_no_overrides")
            return

        formatted = {k: format_value(v) for k, v in overrides.items()}
        try:
            diff = await write_app_conf(
                self._ha_ssh,
                slug,
                formatted,
                reason=f"healer:{diagnostic.category}",
            )
            log.info("netalertx_app_conf_rewrite_done", diff=diff)
        except AppConfSyntaxError as exc:
            log.error("netalertx_app_conf_rewrite_syntax_error", error=str(exc))

    async def _restart_container(self) -> None:
        from netalertx.installer import resolve_addon_slug

        slug = resolve_addon_slug()
        if slug:
            cmd = f"ha apps restart {slug}"
            ssh = self._ha_ssh
        else:
            cmd = f"docker restart {self._container}"
            ssh = self._ssh
        log.info("netalertx_container_restart_start", command=cmd)
        await ssh.run(cmd)
        log.info("netalertx_container_restart_done")

    # ------------------------------------------------------------------
    # Dashboard-triggered heal (item 58)
    # ------------------------------------------------------------------

    async def run_heal(self, action: str) -> None:
        """Execute a named heal action approved via the dashboard."""
        raise ValueError(f"Unknown NetAlertX heal action: {action!r}")

    # ------------------------------------------------------------------
    # Item 19: maintenance issue healing
    # ------------------------------------------------------------------

    async def heal_maintenance_issues(self, config_issues: list["ConfigIssue"]) -> None:
        """Process ConfigIssues from item 19 maintenance checks.

        - DB row count excess → DBCLNP cleanup at level 4 only
        """
        _db_metrics = {"Plugins_History", "Events"}
        db_issues = [i for i in config_issues if i.field in _db_metrics]

        if db_issues and self._gate.should_auto_execute(RiskLevel.HIGH):
            await self._api.trigger_scan("DBCLNP")
            log.info("netalertx_dbclnp_triggered", issue_count=len(db_issues))
