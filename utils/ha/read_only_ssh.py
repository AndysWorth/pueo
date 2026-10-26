"""Read-only SSH guard for autonomous investigation sessions (ADR 043).

Investigations (repair issue, notification, lovelace) run under an
auto-approving gate, so their tools must never reach a write-capable SSH
client. ``ReadOnlySSHClient`` wraps a real client and passes through only
read-only commands and file reads; everything else raises
``ReadOnlySSHError``, which the tool methods surface as a failed ToolResult.
"""

from __future__ import annotations

import posixpath
import re
from typing import TYPE_CHECKING, AsyncIterator

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

log = get_logger("read_only_ssh")

# Fixed log-reading commands issued internally by read_logs, search_log and
# list_log_sources. Each pattern is anchored and admits no shell metacharacters.
_READ_ONLY_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^ha (?:core|supervisor|host) logs --lines \d{1,4}$"),
    re.compile(r"^ha host logs --boot -1 --lines \d{1,4}$"),
    re.compile(r"^ha apps logs [a-z0-9][a-z0-9_\-]* -n \d{1,4}$"),
    re.compile(r"^ha apps list --raw-json$"),
)

_ALLOWED_READ_PREFIXES: tuple[str, ...] = ("/config/", "/backup/")
_DENIED_READ_BASENAMES: frozenset[str] = frozenset({"secrets.yaml"})
_DENIED_READ_PREFIXES: tuple[str, ...] = ("/config/.storage/auth",)


class ReadOnlySSHError(PermissionError):
    """Raised when an investigation session attempts a non-read-only SSH action."""


def is_read_only_command(command: str) -> bool:
    """Return True if ``command`` is safe to run without approval."""
    from utils.agent.tool_executor import _ha_command_risk, _is_ha_command_allowed

    if _is_ha_command_allowed(command) and _ha_command_risk(command) is None:
        return True
    return any(p.match(command) for p in _READ_ONLY_COMMAND_PATTERNS)


def is_readable_path(path: str) -> bool:
    """Return True if ``path`` may be read by an investigation session."""
    if ".." in path.split("/"):
        return False
    norm = posixpath.normpath(path)
    if not any(norm.startswith(p) for p in _ALLOWED_READ_PREFIXES):
        return False
    if posixpath.basename(norm) in _DENIED_READ_BASENAMES:
        return False
    return not any(norm.startswith(p) for p in _DENIED_READ_PREFIXES)


class ReadOnlySSHClient:
    """SSHClientProtocol wrapper that permits only read-only operations."""

    def __init__(self, inner: "SSHClientProtocol") -> None:
        self._inner = inner

    async def read_file(self, path: str) -> str:
        if not is_readable_path(path):
            log.warning("read_only_ssh_blocked", op="read_file", path=path)
            raise ReadOnlySSHError(
                f"read of {path!r} is not available in this investigation session"
            )
        return await self._inner.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        log.warning("read_only_ssh_blocked", op="write_file", path=path)
        raise ReadOnlySSHError("file writes are not available in this session")

    async def download_file(self, remote_path: str, local_path: str) -> None:
        log.warning("read_only_ssh_blocked", op="download_file", path=remote_path)
        raise ReadOnlySSHError("file downloads are not available in this session")

    async def run(self, command: str, check: bool = False) -> tuple[int, str, str]:
        if not is_read_only_command(command):
            log.warning("read_only_ssh_blocked", op="run", command=command[:100])
            raise ReadOnlySSHError(
                f"command {command!r} is not available in this investigation "
                "session (read-only; state-changing commands need a HITL card)"
            )
        return await self._inner.run(command, check=check)

    def stream_lines(self, command: str) -> AsyncIterator[str]:
        log.warning("read_only_ssh_blocked", op="stream_lines", command=command[:100])
        raise ReadOnlySSHError("streaming commands are not available in this session")
