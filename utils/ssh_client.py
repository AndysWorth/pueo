"""SSH client implementations: real (asyncssh) and fake (for tests)."""

from __future__ import annotations

import os
from typing import AsyncIterator

import asyncssh

from config import HA_HOST, HA_USER, SSH_KEY_PATH


class AsyncSSHClient:
    """Wraps asyncssh behind the SSHClientProtocol interface."""

    def __init__(
        self,
        host: str = HA_HOST,
        user: str = HA_USER,
        key_path: str = SSH_KEY_PATH,
    ) -> None:
        self._host = host
        self._user = user
        self._key_path = key_path

    def _kw(self) -> dict:
        kw: dict = {"username": self._user, "known_hosts": None}
        agent_sock = os.environ.get("SSH_AUTH_SOCK")
        if agent_sock:
            # Use the SSH agent (handles passphrase-protected keys transparently)
            kw["agent_path"] = agent_sock
        else:
            kw["client_keys"] = [self._key_path]
        return kw

    async def read_file(self, path: str) -> str:
        async with asyncssh.connect(self._host, **self._kw()) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "r") as f:
                    return await f.read()

    async def write_file(self, path: str, content: str) -> None:
        async with asyncssh.connect(self._host, **self._kw()) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "w") as f:
                    await f.write(content)

    async def run(self, command: str, check: bool = False) -> tuple[int, str, str]:
        async with asyncssh.connect(self._host, **self._kw()) as conn:
            result = await conn.run(command, check=check)
            exit_code = result.exit_status if result.exit_status is not None else 1
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            stderr = result.stderr if isinstance(result.stderr, str) else ""
            return exit_code, stdout, stderr

    async def stream_lines(self, command: str) -> AsyncIterator[str]:  # type: ignore[misc]
        async with asyncssh.connect(self._host, **self._kw()) as conn:
            async with conn.create_process(command) as process:
                async for line in process.stdout:
                    yield line


class FakeSSHClient:
    """In-memory SSH client for tests — no real connections made."""

    def __init__(
        self,
        file_contents: dict[str, str] | None = None,
        command_results: dict[str, tuple[int, str, str]] | None = None,
        stream_data: list[str] | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self._file_contents: dict[str, str] = file_contents or {}
        self._command_results: dict[str, tuple[int, str, str]] = command_results or {}
        self._stream_data: list[str] = stream_data or []
        self._stream_error: Exception | None = stream_error
        self.written_files: dict[str, str] = {}
        self.commands_run: list[str] = []

    async def read_file(self, path: str) -> str:
        if path not in self._file_contents:
            raise FileNotFoundError(f"Fake SSH: no file at {path}")
        return self._file_contents[path]

    async def write_file(self, path: str, content: str) -> None:
        self.written_files[path] = content

    async def run(self, command: str, check: bool = False) -> tuple[int, str, str]:
        self.commands_run.append(command)
        for pattern, result in self._command_results.items():
            if pattern in command:
                exit_code, stdout, stderr = result
                if check and exit_code != 0:
                    raise RuntimeError(f"Command failed (exit {exit_code}): {command}")
                return exit_code, stdout, stderr
        return 0, "", ""

    async def stream_lines(self, command: str) -> AsyncIterator[str]:  # type: ignore[misc]
        for line in self._stream_data:
            yield line
        if self._stream_error is not None:
            raise self._stream_error
