"""Tests for the read-only investigation SSH guard (ADR 043, #873)."""

from __future__ import annotations

import asyncio
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.ha.read_only_ssh import (
    ReadOnlySSHClient,
    ReadOnlySSHError,
    is_read_only_command,
    is_readable_path,
)
from utils.ha.ssh_client import FakeSSHClient


class TestIsReadOnlyCommand:
    @pytest.mark.parametrize(
        "cmd",
        [
            "ha core info",
            "ha core info --raw-json",
            "ha backups list",
            "ha resolution info --raw-json",
            "ha apps info mosquitto",
            "ha apps stats z-wave-js --raw-json",
            "ha core logs --lines 200",
            "ha supervisor logs --lines 100",
            "ha host logs --lines 100",
            "ha host logs --lines 5000",
            "ha apps logs core_mosquitto -n 500",
            "ha apps list --raw-json",
        ],
    )
    def test_read_only_commands_allowed(self, cmd):
        assert is_read_only_command(cmd)

    @pytest.mark.parametrize(
        "cmd",
        [
            # Allowlisted but gated (state-changing)
            "ha core restart",
            "ha core stop",
            "ha supervisor repair",
            "ha su repair",
            "ha resolution healthcheck",
            "ha apps restart mosquitto",
            "ha resolution suggestion apply 0123456789abcdef0123456789abcdef",
            "ha resolution issue dismiss 0123456789abcdef0123456789abcdef",
            # Not allowlisted at all
            "ha backups new --name x",
            "ha os update",
            "docker restart netalertx",
            "rm -rf /config",
            "grep -n 'x' /config/configuration.yaml",
            # Injection attempts against the log patterns
            "ha core logs --lines 200; ha core restart",
            "ha apps logs core_mosquitto -n 500 && reboot",
            "ha apps logs ../x -n 5",
            "ha core logs --lines 5000 | sh",
            "ha core logs --lines -5",
            "ha core logs --follow",
            "ha os logs --lines 100",
            "ha core logs --lines 99999",
        ],
    )
    def test_write_or_unknown_commands_blocked(self, cmd):
        assert not is_read_only_command(cmd)


class TestIsReadablePath:
    @pytest.mark.parametrize(
        "path",
        ["/config/configuration.yaml", "/config/packages/x.yaml", "/backup/a.tar"],
    )
    def test_allowed(self, path):
        assert is_readable_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/config/secrets.yaml",
            "/config/packages/secrets.yaml",
            "/config/.storage/auth",
            "/config/.storage/auth_provider.homeassistant",
            "/config/../etc/shadow",
            "/etc/passwd",
            "/config",
            "/root/.ssh/id_ed25519",
        ],
    )
    def test_denied(self, path):
        assert not is_readable_path(path)


class TestReadOnlySSHClient:
    def test_run_passes_through_read_only(self):
        inner = FakeSSHClient(command_results={"ha core info": (0, "ok", "")})
        ro = ReadOnlySSHClient(inner)
        assert asyncio.run(ro.run("ha core info")) == (0, "ok", "")
        assert inner.commands_run == ["ha core info"]

    def test_run_blocks_gated_command(self):
        inner = FakeSSHClient()
        ro = ReadOnlySSHClient(inner)
        with pytest.raises(ReadOnlySSHError, match="not available"):
            asyncio.run(ro.run("ha core restart"))
        assert inner.commands_run == []

    def test_read_file_passes_through(self):
        inner = FakeSSHClient(file_contents={"/config/a.yaml": "x: 1"})
        assert asyncio.run(ReadOnlySSHClient(inner).read_file("/config/a.yaml")) == (
            "x: 1"
        )

    def test_read_file_blocks_secrets(self):
        inner = FakeSSHClient(file_contents={"/config/secrets.yaml": "pw: x"})
        with pytest.raises(ReadOnlySSHError):
            asyncio.run(ReadOnlySSHClient(inner).read_file("/config/secrets.yaml"))

    def test_write_file_blocked(self):
        inner = FakeSSHClient()
        with pytest.raises(ReadOnlySSHError):
            asyncio.run(ReadOnlySSHClient(inner).write_file("/config/a.yaml", "x"))
        assert inner.written_files == {}

    def test_download_file_blocked(self, tmp_path):
        inner = FakeSSHClient()
        with pytest.raises(ReadOnlySSHError):
            asyncio.run(
                ReadOnlySSHClient(inner).download_file("/backup/a", str(tmp_path / "a"))
            )
        assert inner.downloaded_files == []

    def test_stream_lines_blocked(self):
        with pytest.raises(ReadOnlySSHError):
            ReadOnlySSHClient(FakeSSHClient()).stream_lines("ha core logs --follow")


def _executor(ssh, gate=None):
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.hitl.notify import FakeNotifier

    return ToolExecutor(
        ha_ssh_client=ssh,
        gate=gate or FakeAutonomyGate(auto_execute_result=True),
        notifier=FakeNotifier(),
    )


def _call(executor, name, **args):
    from utils.agent.tool_registry import ToolCall

    return asyncio.run(executor.execute(ToolCall(name=name, arguments=args)))


class TestExecutorUnderGuard:
    """Tools behind the guard under an auto-approving gate (the investigation setup)."""

    def test_auto_approved_restart_never_reaches_ssh(self):
        inner = FakeSSHClient()
        r = _call(
            _executor(ReadOnlySSHClient(inner)),
            "run_ha_command",
            command="ha core restart",
        )
        assert not r.success
        assert "not available" in (r.error or "")
        assert inner.commands_run == []

    def test_trigger_backup_fails_cleanly(self):
        inner = FakeSSHClient()
        r = _call(_executor(ReadOnlySSHClient(inner)), "trigger_backup")
        assert not r.success
        assert not any("backups new" in c for c in inner.commands_run)

    def test_read_logs_returns_real_output(self):
        inner = FakeSSHClient(
            command_results={"ha core logs --lines 50": (0, "ERROR boom", "")}
        )
        r = _call(_executor(ReadOnlySSHClient(inner)), "read_logs", lines=50)
        assert r.success
        assert r.output == "ERROR boom"

    def test_search_log_reaches_ssh(self):
        inner = FakeSSHClient(
            command_results={"ha core logs --lines 5000": (0, "a\nERROR zwave\nb", "")}
        )
        r = _call(
            _executor(ReadOnlySSHClient(inner)),
            "search_log",
            log_name="ha_core",
            pattern="zwave",
        )
        assert r.success
        assert "zwave" in r.output


class TestLogToolsExitCodeAndSlug:
    """Regression: HA CLI rejected --no-follow, and the usage error passed as log text."""

    def test_read_logs_nonzero_exit_is_failure(self):
        ssh = FakeSSHClient(
            command_results={"ha host logs": (1, "", "Error: unknown flag: --x")}
        )
        r = _call(_executor(ssh), "read_logs", lines=10, log_source="ha_os")
        assert not r.success
        assert r.error == "exit 1: Error: unknown flag: --x"

    def test_read_logs_empty_success_labelled(self):
        ssh = FakeSSHClient(command_results={"ha core logs": (0, "", "")})
        r = _call(_executor(ssh), "read_logs", lines=10)
        assert r.success
        assert r.output == "(no output)"

    @pytest.mark.parametrize(
        "log_source", ["ha_core", "ha_supervisor", "ha_os", "ha_host"]
    )
    def test_read_logs_commands_pass_guard(self, log_source):
        inner = FakeSSHClient(command_results={"logs --lines": (0, "x", "")})
        r = _call(
            _executor(ReadOnlySSHClient(inner)),
            "read_logs",
            lines=50,
            log_source=log_source,
        )
        assert r.success, r.error
        assert inner.commands_run[0].endswith("logs --lines 50")

    @pytest.mark.parametrize(
        "log_name", ["ha_core", "ha_supervisor", "ha_os", "ha_host"]
    )
    def test_search_log_commands_pass_guard(self, log_name):
        inner = FakeSSHClient(command_results={"logs --lines 5000": (0, "a\nhit", "")})
        r = _call(
            _executor(ReadOnlySSHClient(inner)),
            "search_log",
            log_name=log_name,
            pattern="hit",
        )
        assert r.success, r.error

    def test_search_log_nonzero_exit_is_failure(self):
        ssh = FakeSSHClient(command_results={"ha core logs": (1, "", "bad flag")})
        r = _call(_executor(ssh), "search_log", log_name="ha_core", pattern="x")
        assert not r.success
        assert "exit 1: bad flag" in (r.error or "")

    def test_search_log_app_nonzero_exit_is_failure(self):
        ssh = FakeSSHClient(command_results={"ha apps logs": (1, "", "no such app")})
        r = _call(
            _executor(ssh),
            "search_log",
            log_name="ha_app",
            addon_slug="core_mosquitto",
            pattern="x",
        )
        assert not r.success
        assert "exit 1: no such app" in (r.error or "")

    @pytest.mark.parametrize("tool", ["read_logs", "search_log"])
    def test_app_slug_injection_rejected(self, tool):
        ssh = FakeSSHClient()
        args = {"addon_slug": "x; rm -rf /config"}
        if tool == "read_logs":
            args.update(lines=10, log_source="ha_app")
        else:
            args.update(log_name="ha_app", pattern="x")
        r = _call(_executor(ssh), tool, **args)
        assert not r.success
        assert "Invalid addon_slug" in (r.error or "")
        assert ssh.commands_run == []


class TestRunHaCommandExitCode:
    def test_nonzero_exit_reports_code_and_stderr(self):
        ssh = FakeSSHClient(command_results={"ha core check": (1, "", "bad yaml")})
        r = _call(_executor(ssh), "run_ha_command", command="ha core check")
        assert not r.success
        assert r.error == "exit 1: bad yaml"

    def test_nonzero_exit_falls_back_to_stdout(self):
        ssh = FakeSSHClient(command_results={"ha core check": (2, "Error: x", "")})
        r = _call(_executor(ssh), "run_ha_command", command="ha core check")
        assert r.error == "exit 2: Error: x"
        assert r.output == "Error: x"

    def test_nonzero_exit_no_output(self):
        ssh = FakeSSHClient(command_results={"ha core check": (3, "", "")})
        r = _call(_executor(ssh), "run_ha_command", command="ha core check")
        assert r.error == "exit 3: (no output)"

    def test_empty_success_is_labelled(self):
        ssh = FakeSSHClient(command_results={"ha core check": (0, "", "")})
        r = _call(_executor(ssh), "run_ha_command", command="ha core check")
        assert r.success
        assert r.output == "(no output)"

    def test_success_with_output(self):
        ssh = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        r = _call(_executor(ssh), "run_ha_command", command="ha core check")
        assert r.success
        assert r.output == "ok"


# ---------------------------------------------------------------------------
# Wiring: each investigation hands ToolExecutor a ReadOnlySSHClient
# ---------------------------------------------------------------------------


def _capture_executor_kwargs():
    """Patch ToolExecutor at its source so constructor kwargs are recorded."""
    from utils.agent import tool_executor as te

    captured: dict = {}
    real = te.ToolExecutor

    def _factory(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    return mock.patch.object(te, "ToolExecutor", side_effect=_factory), captured


def _patched_loop():
    from utils.agent.tool_registry import AgentLoopResult

    loop = MagicMock()
    loop.run = AsyncMock(return_value=AgentLoopResult(outcome="success"))
    return [
        mock.patch("utils.agent.agent_loop.AgentLoop", return_value=loop),
        mock.patch("utils.agent.supervisor.increment_active_agent"),
        mock.patch("utils.agent.supervisor.decrement_active_agent"),
        mock.patch("utils.agent.supervisor.publish_activity_done"),
        mock.patch(
            "utils.agent.supervisor.make_activity_timeline_callback",
            return_value=None,
        ),
    ]


class TestInvestigationWiring:
    def _run(self, coro_fn):
        patch_exec, captured = _capture_executor_kwargs()
        patches = [patch_exec, *_patched_loop()]
        for p in patches:
            p.start()
        try:
            asyncio.run(coro_fn())
        finally:
            for p in reversed(patches):
                p.stop()
        return captured

    def test_repair_issue_investigation(self, tmp_path):
        from agents.ha_log_monitor import _run_repair_issue_investigation
        from utils.hitl.notify import FakeNotifier

        class _Issue:
            issue_key = issue_id = "abc"
            domain = "homeassistant"
            severity = "warning"
            translation_key = "x"
            breaks_in_ha_version = None
            data: dict = {}

        ssh = FakeSSHClient()
        captured = self._run(
            lambda: _run_repair_issue_investigation(
                issue=_Issue(),  # type: ignore[arg-type]
                notifier=FakeNotifier(),
                db_path=str(tmp_path / "x.db"),
                llm_client=MagicMock(),
                ssh_client=ssh,
            )
        )
        assert isinstance(captured["ha_ssh_client"], ReadOnlySSHClient)
        assert captured["ha_ssh_client"]._inner is ssh
        # Post-update restart still uses the unwrapped client.
        assert captured["post_update_ssh_client"] is ssh

    def test_notification_investigation(self, tmp_path):
        from agents.ha_notification_manager import _run_notification_investigation
        from utils.hitl.notify import FakeNotifier

        ssh = FakeSSHClient()
        captured = self._run(
            lambda: _run_notification_investigation(
                notification_id="n1",
                title="t",
                message="m",
                ha_created_at=None,
                db_path=str(tmp_path / "x.db"),
                notifier=FakeNotifier(),
                llm_client=MagicMock(),
                ssh_client=ssh,
            )
        )
        assert isinstance(captured["ha_ssh_client"], ReadOnlySSHClient)
        assert captured["ha_ssh_client"]._inner is ssh

    def test_lovelace_investigation(self, tmp_path):
        from agents.ha_lovelace_monitor import _run_lovelace_investigation
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier

        ssh = FakeSSHClient()
        captured = self._run(
            lambda: _run_lovelace_investigation(
                suspicious=[{"entity_id": "sensor.x", "has_state": False}],
                ws_client=FakeHAWebSocketClient(),
                db_path=str(tmp_path / "x.db"),
                notifier=FakeNotifier(),
                llm_client=MagicMock(),
                ssh_client=ssh,
            )
        )
        assert isinstance(captured["ha_ssh_client"], ReadOnlySSHClient)
        assert captured["ha_ssh_client"]._inner is ssh

    @pytest.mark.parametrize("which", ["notification", "lovelace"])
    def test_default_ssh_client_is_wrapped_real_client(self, tmp_path, which):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.ha.ssh_client import AsyncSSHClient
        from utils.hitl.notify import FakeNotifier

        if which == "notification":
            from agents.ha_notification_manager import (
                _run_notification_investigation,
            )

            def fn():
                return _run_notification_investigation(
                    notification_id="n1",
                    title="t",
                    message="m",
                    ha_created_at=None,
                    db_path=str(tmp_path / "x.db"),
                    notifier=FakeNotifier(),
                    llm_client=MagicMock(),
                )

        else:
            from agents.ha_lovelace_monitor import _run_lovelace_investigation

            def fn():
                return _run_lovelace_investigation(
                    suspicious=[{"entity_id": "sensor.x", "has_state": False}],
                    ws_client=FakeHAWebSocketClient(),
                    db_path=str(tmp_path / "x.db"),
                    notifier=FakeNotifier(),
                    llm_client=MagicMock(),
                )

        captured = self._run(fn)
        assert isinstance(captured["ha_ssh_client"]._inner, AsyncSSHClient)
