"""Tool executor — dispatches tool calls to their implementations (item 43).

ToolExecutor is initialized with SSH clients, autonomy gate, and notifier.
Each tool method returns a ToolResult; errors are captured rather than raised.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Optional

from config import CONFIG_REMOTE_PATH, OLLAMA_MODEL
from utils.logging import get_correlation_id, get_logger
from utils.tool_registry import ToolCall, ToolResult

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from netalertx.api_client import NetAlertXAPIClient
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("tool_executor")

_HA_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ha core check",
        "ha core restart",
        "ha core stop",
        "ha host info",
        "ha backups list",
        "ha backups new",
        "ha apps list",
        "ha os info",
    }
)

_READ_FILE_ALLOWED_PREFIXES: tuple[str, ...] = ("/config/", "/backup/")


class ToolExecutor:
    """Executes tool calls on behalf of AgentLoop.

    ha_ssh_client and nax_ssh_client may be the same object when NetAlertX
    runs as a supervisor add-on on the same HA host.
    """

    def __init__(
        self,
        ha_ssh_client: "SSHClientProtocol",
        gate: "AutonomyGate",
        notifier: "NotifierProtocol",
        nax_ssh_client: Optional["SSHClientProtocol"] = None,
        netalertx_api_client: Optional["NetAlertXAPIClient"] = None,
        netalertx_container_name: str = "netalertx",
    ) -> None:
        self._ha_ssh = ha_ssh_client
        self._nax_ssh = nax_ssh_client
        self._gate = gate
        self._notifier = notifier
        self._api = netalertx_api_client
        self._container = netalertx_container_name
        self._apply_fix_used = False

    def reset(self) -> None:
        """Reset per-loop state. Called by AgentLoop before each run()."""
        self._apply_fix_used = False

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.arguments
        name = tool_call.name
        log.info("tool_execute", tool=name)
        try:
            if name == "read_config":
                return await self._read_config(args.get("path", CONFIG_REMOTE_PATH))
            if name == "read_logs":
                return await self._read_logs(int(args.get("lines", 100)))
            if name == "run_ha_command":
                return await self._run_ha_command(args.get("command", ""))
            if name == "read_file":
                return await self._read_file(args.get("path", ""))
            if name == "query_netalertx":
                return await self._query_netalertx(args.get("query_type", "health"))
            if name == "apply_fix":
                return await self._apply_fix(
                    args.get("yaml_content", ""),
                    args.get("description", ""),
                )
            if name == "verify_fix":
                return await self._verify_fix()
            if name == "finish_repair":
                return ToolResult(
                    tool_name="finish_repair",
                    success=True,
                    output=args.get("summary", "Repair complete"),
                )
            if name == "query_knowledge":
                return ToolResult(
                    tool_name="query_knowledge",
                    success=False,
                    output="",
                    error="query_knowledge not yet implemented (Phase 15)",
                )
            if name == "restart_netalertx":
                return await self._restart_netalertx()
            if name == "rewrite_netalertx_conf":
                return await self._rewrite_netalertx_conf(args.get("overrides", {}))
            return ToolResult(
                tool_name=name,
                success=False,
                output="",
                error=f"Unknown tool: {name!r}",
            )
        except Exception as exc:
            log.error("tool_execute_error", tool=name, error=str(exc))
            return ToolResult(tool_name=name, success=False, output="", error=str(exc))

    # ------------------------------------------------------------------
    # HA tools
    # ------------------------------------------------------------------

    async def _read_config(self, path: str) -> ToolResult:
        try:
            content = await self._ha_ssh.read_file(path)
            return ToolResult(tool_name="read_config", success=True, output=content)
        except Exception as exc:
            return ToolResult(
                tool_name="read_config", success=False, output="", error=str(exc)
            )

    async def _read_logs(self, lines: int) -> ToolResult:
        try:
            _, stdout, stderr = await self._ha_ssh.run(
                f"ha core logs --lines {lines}", check=False
            )
            return ToolResult(
                tool_name="read_logs", success=True, output=stdout or stderr
            )
        except Exception as exc:
            return ToolResult(
                tool_name="read_logs", success=False, output="", error=str(exc)
            )

    async def _run_ha_command(self, command: str) -> ToolResult:
        normalized = command.strip()
        if normalized not in _HA_COMMAND_ALLOWLIST:
            log.warning("run_ha_command_rejected", command=normalized)
            return ToolResult(
                tool_name="run_ha_command",
                success=False,
                output="",
                error=f"Command not in allowlist: {normalized!r}",
            )
        try:
            exit_code, stdout, stderr = await self._ha_ssh.run(normalized, check=False)
            return ToolResult(
                tool_name="run_ha_command",
                success=exit_code == 0,
                output=stdout or stderr,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="run_ha_command", success=False, output="", error=str(exc)
            )

    async def _read_file(self, path: str) -> ToolResult:
        if not any(path.startswith(p) for p in _READ_FILE_ALLOWED_PREFIXES):
            log.warning("read_file_rejected", path=path)
            return ToolResult(
                tool_name="read_file",
                success=False,
                output="",
                error=f"Path not in allowed directories: {path!r}",
            )
        try:
            content = await self._ha_ssh.read_file(path)
            return ToolResult(tool_name="read_file", success=True, output=content)
        except Exception as exc:
            return ToolResult(
                tool_name="read_file", success=False, output="", error=str(exc)
            )

    async def _apply_fix(self, yaml_content: str, description: str) -> ToolResult:
        if self._apply_fix_used:
            log.warning("apply_fix_already_used")
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error="apply_fix may only be called once per loop run",
            )

        from utils.yaml_validator import validate_proposed_fix

        try:
            original = await self._ha_ssh.read_file(CONFIG_REMOTE_PATH)
        except Exception as exc:
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error=f"Could not read original config: {exc}",
            )

        validation = validate_proposed_fix(original, yaml_content)
        if not validation.is_safe:
            log.error("apply_fix_validation_failed", reasons=validation.reasons)
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error=f"Content validation failed: {'; '.join(validation.reasons)}",
            )

        from utils.autonomy import RiskLevel

        nid = get_correlation_id() or str(uuid.uuid4())
        approved = await self._gate.require_approval(
            subject=f"Pueo HITL: apply_fix — {description}",
            body=f"Proposed fix:\n{yaml_content[:500]}",
            payload={
                "notification_id": nid,
                "severity": "HIGH",
                "description": description,
                "correlation_id": nid,
            },
            notifier=self._notifier,
            risk=RiskLevel.HIGH,
        )
        if not approved:
            log.warning("apply_fix_gate_rejected")
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error="Rejected via autonomy gate",
            )

        # Backup invariant: backup before every write
        from ha_agent_sandbox_engine import (
            commit_atomic_swap,
            deploy_and_test_in_sandbox,
            execute_remote_backup,
            record_backup_slug,
        )
        from ha_agent_advanced import (
            enforce_ha_retention,
            offload_backup_to_local,
            purge_local_backups,
        )

        try:
            slug = await execute_remote_backup(ssh_client=self._ha_ssh)
            record_backup_slug(slug)
            await offload_backup_to_local(slug, ssh_client=self._ha_ssh)
            await enforce_ha_retention(ssh_client=self._ha_ssh)
            purge_local_backups()
        except Exception as exc:
            log.critical("apply_fix_backup_failed", error=str(exc))
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error=f"Backup failed: {exc}",
            )

        passed = await deploy_and_test_in_sandbox(yaml_content, ssh_client=self._ha_ssh)
        if not passed:
            return ToolResult(
                tool_name="apply_fix",
                success=False,
                output="",
                error="Sandbox test failed; production not modified",
            )

        await commit_atomic_swap(yaml_content, ssh_client=self._ha_ssh)
        self._apply_fix_used = True
        log.info("apply_fix_committed", slug=slug)
        return ToolResult(
            tool_name="apply_fix",
            success=True,
            output=f"Fix applied and validated; backup slug: {slug}",
        )

    async def _verify_fix(self) -> ToolResult:
        from ha_agent_sandbox_engine import execute_remote_preflight_check

        try:
            exit_code, stdout, stderr = await execute_remote_preflight_check(
                ssh_client=self._ha_ssh
            )
            if exit_code == 0:
                return ToolResult(
                    tool_name="verify_fix", success=True, output="ha core check passed"
                )
            return ToolResult(
                tool_name="verify_fix",
                success=False,
                output=stderr or stdout,
                error="ha core check failed",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="verify_fix", success=False, output="", error=str(exc)
            )

    # ------------------------------------------------------------------
    # NetAlertX tools
    # ------------------------------------------------------------------

    async def _query_netalertx(self, query_type: str) -> ToolResult:
        if self._api is None:
            return ToolResult(
                tool_name="query_netalertx",
                success=False,
                output="",
                error="NetAlertX API client not configured",
            )
        try:
            data: dict | list
            if query_type == "health":
                data = await self._api.get_about()
            elif query_type == "devices":
                data = await self._api.get_devices()
            elif query_type == "events":
                data = await self._api.get_events()
            else:
                return ToolResult(
                    tool_name="query_netalertx",
                    success=False,
                    output="",
                    error=f"Unknown query_type: {query_type!r}",
                )
            return ToolResult(
                tool_name="query_netalertx",
                success=True,
                output=json.dumps(data, default=str),
            )
        except Exception as exc:
            return ToolResult(
                tool_name="query_netalertx", success=False, output="", error=str(exc)
            )

    async def _restart_netalertx(self) -> ToolResult:
        ssh = self._nax_ssh or self._ha_ssh
        try:
            _, stdout, stderr = await ssh.run(
                f"docker restart {self._container}", check=False
            )
            if self._api is not None:
                try:
                    await self._api.trigger_scan()
                except Exception:  # nosec B110 — scan trigger is best-effort
                    pass
            return ToolResult(
                tool_name="restart_netalertx",
                success=True,
                output=stdout or stderr or "container restarted",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="restart_netalertx", success=False, output="", error=str(exc)
            )

    async def _rewrite_netalertx_conf(self, overrides: dict) -> ToolResult:
        from netalertx.config_validator import validate_app_conf
        from netalertx.healer import _merge_conf

        _CONF_PATH = "/data/app.conf"
        ssh = self._nax_ssh or self._ha_ssh
        try:
            current = await ssh.read_file(_CONF_PATH)
        except (FileNotFoundError, OSError):
            current = ""

        fixed = _merge_conf(current, {k: str(v) for k, v in overrides.items()})
        issues = validate_app_conf(fixed)
        blocking = [i for i in issues if i.severity in ("HIGH", "CRITICAL")]
        if blocking:
            reasons = [i.message for i in blocking]
            return ToolResult(
                tool_name="rewrite_netalertx_conf",
                success=False,
                output="",
                error=f"Config still invalid after merge: {'; '.join(reasons)}",
            )

        try:
            await ssh.write_file(_CONF_PATH, fixed)
            return ToolResult(
                tool_name="rewrite_netalertx_conf",
                success=True,
                output="app.conf updated",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="rewrite_netalertx_conf",
                success=False,
                output="",
                error=str(exc),
            )
