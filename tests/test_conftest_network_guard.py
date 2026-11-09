"""Tests for the conftest network guard (#1162)."""

import socket

import pytest

import conftest as _conftest


class TestIsLoopbackHost:
    @pytest.mark.parametrize(
        "host",
        [None, "", "localhost", "LOCALHOST", "127.0.0.1", "127.5.6.7", "::1", "[::1]"],
    )
    def test_loopback_hosts_allowed(self, host):
        assert _conftest._is_loopback_host(host)

    def test_bind_all_addresses_allowed(self):
        assert _conftest._is_loopback_host("0.0.0.0")  # nosec B104
        assert _conftest._is_loopback_host("::")

    def test_bytes_host_decoded(self):
        assert _conftest._is_loopback_host(b"localhost")
        assert not _conftest._is_loopback_host(b"api.github.com")

    @pytest.mark.parametrize(
        "host",
        [
            "homeassistant.local",
            "ha.local",
            "192.168.1.10",
            "ntfy.sh",
            "fe80::102b:76f7:a2b7:26cc%en0",
            "2601:18c::1",
        ],
    )
    def test_other_hosts_blocked(self, host):
        assert not _conftest._is_loopback_host(host)


class TestNetworkGuardActive:
    def test_active_inside_a_unit_test(self):
        assert _conftest._network_guard_active()

    def test_inactive_for_integration_tests(self, monkeypatch):
        monkeypatch.setenv(
            "PYTEST_CURRENT_TEST", "tests/integration/test_live_ha.py::test_x (call)"
        )
        assert not _conftest._network_guard_active()

    def test_inactive_outside_a_test(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert not _conftest._network_guard_active()


class TestNetworkGuard:
    def test_getaddrinfo_non_loopback_fails_the_test(self):
        with pytest.raises(pytest.fail.Exception, match="homeassistant.local"):
            socket.getaddrinfo("homeassistant.local", 8123)
        _conftest._network_violations.clear()

    def test_failure_is_not_swallowed_by_except_exception(self):
        """The guard must survive production code's broad `except Exception`."""

        def _swallowing_caller():
            try:
                socket.getaddrinfo("homeassistant.local", 8123)
            except Exception:  # nosec B110
                return "swallowed"

        with pytest.raises(pytest.fail.Exception):
            _swallowing_caller()
        _conftest._network_violations.clear()

    def test_connect_non_loopback_fails_the_test(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(pytest.fail.Exception, match="192.0.2.1:8123"):
                sock.connect(("192.0.2.1", 8123))
            with pytest.raises(pytest.fail.Exception, match="192.0.2.1:8123"):
                sock.connect_ex(("192.0.2.1", 8123))
        finally:
            sock.close()
        _conftest._network_violations.clear()

    def test_violation_is_recorded_for_the_teardown_check(self):
        with pytest.raises(pytest.fail.Exception):
            socket.getaddrinfo("ha.local", 8123)
        assert _conftest._network_violations == ["ha.local"]
        _conftest._network_violations.clear()

    def test_loopback_getaddrinfo_and_connect_still_work(self):
        assert socket.getaddrinfo("127.0.0.1", 0)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            client.connect(server.getsockname())
            # Already connected: exercises the connect_ex pass-through.
            assert client.connect_ex(server.getsockname()) != 0
        finally:
            client.close()
            server.close()
        assert _conftest._network_violations == []

    def test_integration_tests_are_exempt(self, monkeypatch):
        monkeypatch.setenv(
            "PYTEST_CURRENT_TEST", "tests/integration/test_live_ha.py::test_x (call)"
        )
        calls = []
        monkeypatch.setattr(
            _conftest, "_real_getaddrinfo", lambda *a, **kw: calls.append(a) or []
        )
        assert socket.getaddrinfo("homeassistant.local", 8123) == []
        assert calls and _conftest._network_violations == []

    def test_unreachable_ha_ssh_fixture_raises_without_a_socket(
        self, unreachable_ha_ssh
    ):
        import asyncio

        from utils.ha.ssh_client import AsyncSSHClient

        client = AsyncSSHClient("homeassistant.local", "root", "/nonexistent")
        with pytest.raises(OSError, match="unreachable"):
            asyncio.run(client.run("ha core info"))
        assert _conftest._network_violations == []
