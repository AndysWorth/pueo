# ADR 043 — Read-only SSH for autonomous investigations

**Status:** Accepted
**Date:** 2026-10-14

## Context

Three investigation pipelines build their own `ToolExecutor`: repair issues (`agents/ha_log_monitor.py::_run_repair_issue_investigation`), persistent notifications (`agents/ha_notification_manager.py::_run_notification_investigation`) and Lovelace entities (`agents/ha_lovelace_monitor.py::_run_lovelace_investigation`). Each runs under `FakeAutonomyGate(auto_execute_result=True)`, because the terminal tool creates the HITL card and nested approval cards are unwanted.

An auto-approving gate must never sit in front of a write-capable SSH client. To avoid that, these sessions got `_NullSSH`, whose `run()` returns `(0, "", "")` and whose `read_file()` returns `""`. That fixed the safety problem, but every read tool (`read_logs`, `search_log`, `run_ha_command`, `read_file`, `read_config`, `list_log_sources`) then returned **empty success**. The 2026-10-13/14 activity review found the LLM "investigating" with no evidence and reporting confident, made-up root causes as `outcome=success`. Runbook distillation then learned from those sessions. The same inert client had already caused #846, where an auto-restart went to `_NullSSH` and was reported as "online".

## Decision

Add `ReadOnlySSHClient` (`utils/ha/read_only_ssh.py`). It wraps a real `SSHClientProtocol` and is the only SSH client the investigation tools receive.

| Method | Behaviour |
|---|---|
| `run(cmd)` | Passes through only if `is_read_only_command(cmd)`; otherwise raises `ReadOnlySSHError` |
| `read_file(path)` | Passes through only if `is_readable_path(path)`; otherwise raises |
| `write_file`, `download_file`, `stream_lines` | Always raise |

`is_read_only_command` accepts:
- Commands where `_is_ha_command_allowed(cmd)` is true and `_ha_command_risk(cmd)` is `None`. These are the allowlisted, non-gated commands, so the allowlist stays the single source of truth (ADR 040/041).
- A small set of anchored regexes, with no shell metacharacters, for the fixed log commands the tools emit internally: `ha {core,supervisor,host} logs --lines N`, `ha apps logs <slug> -n N` and `ha apps list --raw-json`.

`is_readable_path` requires a `/config/` or `/backup/` prefix and rejects `..` segments, any `secrets.yaml`, and `/config/.storage/auth*`. These sessions run with no human in the loop and, under `LLM_PROVIDER=cloud`, may send tool output off-host, so credentials are kept out of reach.

`ReadOnlySSHError` subclasses `PermissionError`. Every tool method already catches exceptions into `ToolResult(success=False, error=...)`, so the LLM sees an explicit "not available in this investigation session" instead of silent success. `trigger_backup` and `apply_fix` fail at the backup step, before any write.

The post-update auto-restart in `finish_repair_issue` keeps using the unwrapped client via `post_update_ssh_client` (#847). That path is a fixed, code-driven action, not an LLM-chosen command.

Turning the lights on exposed a latent bug: `read_logs` (supervisor/os/host) and `search_log` (all SSH sources) sent `ha <x> logs --no-follow`, a flag the HA CLI rejects. `_NullSSH` hid this, and even with real SSH the usage error came back as `success=True` "log content". Every source now uses `--lines N`. `ha os` has no `logs` subcommand at all, so `ha_os` is kept in the tool enum as an alias of `ha host logs` (the HAOS journal). `read_logs` and `search_log` treat a non-zero exit as a failure. `addon_slug` is validated with `_SLUG_RE` before it is interpolated into a command; before this it was unvalidated, which was a shell-injection path in chat, where the SSH client is unguarded.

Also: `run_ha_command` now reports a non-zero exit as `success=False, error="exit N: <stderr|stdout|(no output)>"`, and a successful empty output as `(no output)`, so "ran and printed nothing" and "failed" no longer look the same.

## Consequences

- Investigations see real logs, config and Supervisor state, so conclusions rest on evidence.
- State-changing SSH commands cannot run in these sessions, even when auto-approved. The agent has to recommend them in its terminal tool, which produces a card.
- Each investigation opens real SSH connections (one per tool call), as chat already does.
- Adding a new internal read command to a tool used by these registries means adding a pattern to `_READ_ONLY_COMMAND_PATTERNS`. Otherwise the tool fails closed with an explicit error.
- REST write tools (`call_service`, `reload_integration` in the repair-issue registry) now route through `AutonomyGate(AUTONOMY_LEVEL)`, consistent with `_run_host_health_investigation`. They produce HITL cards at lower autonomy levels instead of being silently auto-approved. Residual risk #874 is closed.
- Security review: `known_hosts=None` (ADR 004) still applies to the inner client.
