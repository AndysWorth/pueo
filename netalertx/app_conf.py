"""Safe app.conf reader/writer for the NetAlertX HA add-on.

NetAlertX loads app.conf with Python's compile(), so every value must be a
valid Python literal.  This module is the single authority on reading, merging,
syntax-checking, and writing that file.  All callers (reconcile, healer,
installer, ToolExecutor) go through write_app_conf so that every write is
covered by a backup, a compile-check, and a last-known-good copy.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

log = get_logger("netalertx.app_conf")

_KEY_RE = re.compile(r"^(\s*)([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")


class AppConfSyntaxError(ValueError):
    """Raised when a proposed app.conf fails the compile() check."""


# ── public helpers ────────────────────────────────────────────────────────────


def parse_check(text: str) -> list[str]:
    """Compile text as Python and return a list of error strings (empty = OK).

    Mirrors NetAlertX's plugin_helper.read_config_file() check.
    """
    try:
        compile(text, "<app.conf>", "exec")
        return []
    except SyntaxError as exc:
        return [f"SyntaxError at line {exc.lineno}: {exc.msg}"]
    except Exception as exc:
        return [str(exc)]


def format_value(value: object) -> str:
    """Coerce a Python value to a valid app.conf literal string.

    - None / empty string  → ''
    - int / float / bool   → str(value)
    - list / dict          → repr(value)
    - str that already looks like a Python literal → pass through
    - anything else        → repr(value)
    """
    if value is None or value == "":
        return "''"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return repr(value)
    s = str(value)
    # Pass through if it already looks like a quoted string or a list/dict literal.
    if s and s[0] in ("'", '"', "[", "{") or s in ("True", "False"):
        return s
    # Bare word — quote it.
    return repr(s)


def merge(current: str, updates: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Return (merged_content, diff) with override KEY = value lines applied.

    Canonical write format is ``KEY = value`` (spaces around ``=``), matching
    the installer's convention.  Any value in *updates* that is already a
    formatted literal (from format_value()) is written as-is.

    diff maps key → 'old_val -> new_val'.
    """
    lines = current.splitlines(keepends=True) if current else []
    applied: set[str] = set()
    diff: dict[str, str] = {}
    new_lines: list[str] = []

    for line in lines:
        m = _KEY_RE.match(line)
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


def conf_path_for_slug(slug: str, app_config_root: str = "") -> str:
    """Return the remote path to app.conf for an HA add-on slug."""
    root = app_config_root or "/app_configs"
    return f"{root}/{slug}/config/app.conf"


# ── safe writer ───────────────────────────────────────────────────────────────


async def write_app_conf(
    ssh: "SSHClientProtocol",
    slug: str,
    updates: dict[str, str],
    *,
    reason: str,
    app_config_root: str = "",
    explicit_path: str = "",
) -> dict[str, str]:
    """Merge *updates* into app.conf and write it safely.

    Write order:
      1. Read current conf.
      2. execute_remote_backup (HA backup — raises on failure).
      3. record_backup_slug.
      4. merge(current, updates).
      5. parse_check — raises AppConfSyntaxError on any error (no write).
      6. Write current to app.conf.pueo-lkg.
      7. Write merged to app.conf.

    Pass *explicit_path* to override the slug-based path derivation (e.g. when
    the caller has already resolved the exact data directory from ha apps info).

    Returns the diff dict (key → 'old -> new') for logging.
    Raises AppConfSyntaxError when the merged text fails compile().
    """
    from agents.ha_agent_sandbox_engine import execute_remote_backup, record_backup_slug

    if explicit_path:
        path = explicit_path
    else:
        from utils.ha.app_configs import resolve_app_config_root

        root = app_config_root or await resolve_app_config_root(ssh)
        path = conf_path_for_slug(slug, root)

    lkg_path = path + ".pueo-lkg"

    try:
        current = await ssh.read_file(path)
    except (FileNotFoundError, OSError):
        current = ""

    # backup before any write
    backup_slug = await execute_remote_backup(ssh_client=ssh)
    record_backup_slug(backup_slug)

    merged, diff = merge(current, updates)

    errors = parse_check(merged)
    if errors:
        log.error(
            "app_conf_syntax_error",
            errors=errors,
            reason=reason,
            path=path,
        )
        raise AppConfSyntaxError("; ".join(errors))

    # save last-known-good before overwriting
    if current:
        await ssh.write_file(lkg_path, current)

    await ssh.write_file(path, merged)
    log.info("app_conf_written", diff=diff, path=path, reason=reason)
    return diff
