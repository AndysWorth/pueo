"""Live HA smoke tests — skipped automatically unless HA_HOST is set.

Run with:
    HA_HOST=homeassistant.local pytest tests/integration/ -m live_ha -v

These tests open a real SSH connection and issue read-only HA commands.
They never modify state. All assertions are minimal — presence/type checks only.
"""

import asyncio
import os

import pytest


@pytest.mark.live_ha
class TestLiveHASmoke:
    """Minimal smoke tests that require a real Home Assistant instance."""

    def _ssh_kwargs(self):
        return {
            "host": os.environ.get("HA_HOST", ""),
            "user": os.environ.get("HA_USER", "root"),
            "key_path": os.environ.get("SSH_KEY_PATH", ""),
        }

    def test_ssh_connection_succeeds(self):
        """Can open an SSH connection to HA_HOST without error."""
        from utils.ssh_client import AsyncSSHClient

        kw = self._ssh_kwargs()
        client = AsyncSSHClient(**kw)

        async def _check():
            rc, out, _ = await client.run("echo hello")
            return rc, out

        rc, out = asyncio.run(_check())
        assert rc == 0
        assert "hello" in out

    def test_fetch_remote_config_returns_yaml(self):
        """fetch_remote_config() returns a non-empty string from the live HA instance."""
        from utils.ssh_client import AsyncSSHClient

        kw = self._ssh_kwargs()
        client = AsyncSSHClient(**kw)
        config_path = os.environ.get("HA_CONFIG_PATH", "/config/configuration.yaml")

        content = asyncio.run(client.read_file(config_path))
        assert isinstance(content, str)
        assert len(content) > 0

    def test_ha_core_check_exits_zero(self):
        """ha core check returns exit code 0 on a healthy HA instance."""
        from utils.ssh_client import AsyncSSHClient

        kw = self._ssh_kwargs()
        client = AsyncSSHClient(**kw)

        rc, _out, _err = asyncio.run(client.run("ha core check"))
        assert rc == 0

    def test_log_stream_yields_at_least_one_line(self):
        """ha core logs yields at least one line within a short timeout."""

        async def _collect():
            from utils.ssh_client import AsyncSSHClient

            kw = {
                "host": os.environ.get("HA_HOST", ""),
                "user": os.environ.get("HA_USER", "root"),
                "key_path": os.environ.get("SSH_KEY_PATH", ""),
            }
            client = AsyncSSHClient(**kw)
            lines = []
            async for line in client.stream_lines("ha core logs --follow"):
                lines.append(line)
                break  # one line is enough
            return lines

        lines = asyncio.run(asyncio.wait_for(_collect(), timeout=10.0))
        assert len(lines) >= 1
