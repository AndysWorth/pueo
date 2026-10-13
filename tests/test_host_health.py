"""Tests for utils/disk/host_health.py."""

import asyncio
import json
import time

import pytest


def _meminfo(swap_total_kb: int = 1048576, swap_free_kb: int = 630000) -> str:
    return (
        f"MemTotal: 1931384 kB\nMemFree: 22100 kB\nMemAvailable: 563200 kB\n"
        f"SwapTotal: {swap_total_kb} kB\nSwapFree: {swap_free_kb} kB\n"
    )


def _host_info_json(
    disk_life: float = 40.0, boot_ts_us: int = 1_700_000_000_000_000
) -> str:
    return json.dumps(
        {
            "result": "ok",
            "data": {
                "disk_free": 5.5,
                "disk_total": 13.6,
                "disk_used": 8.1,
                "disk_life_time": disk_life,
                "boot_timestamp": boot_ts_us,
                "hostname": "homeassistant",
                "dt_utc": "2026-10-13T12:00:00+00:00",
            },
        }
    )


def _resolution_json(issues: list | None = None) -> str:
    return json.dumps(
        {
            "result": "ok",
            "data": {
                "unsupported": [],
                "unhealthy": [],
                "issues": issues or [],
                "suggestions": [],
                "checks": [],
            },
        }
    )


def _batch_ssh(
    host_raw: str = "",
    res_raw: str = "",
    mem_raw: str = "",
):
    from utils.ha.ssh_client import FakeSSHClient

    parts = [
        "echo '@@host'; ha host info --raw-json 2>/dev/null",
        "echo '@@res'; ha resolution info --raw-json 2>/dev/null",
        "echo '@@mem'; cat /proc/meminfo",
    ]
    batch_cmd = "; ".join(parts)
    output = ""
    if host_raw:
        output += f"@@host\n{host_raw}\n"
    if res_raw:
        output += f"@@res\n{res_raw}\n"
    if mem_raw:
        output += f"@@mem\n{mem_raw}\n"
    return FakeSSHClient(command_results={batch_cmd: (0, output, "")})


class TestResolutionInfo:
    def test_valid_construction(self):
        from utils.disk.host_health import ResolutionInfo

        r = ResolutionInfo(
            unsupported=["foo"],
            unhealthy=[],
            issues=[{"type": "rpi_firmware_update_blocked"}],
            suggestions=[],
        )
        assert r.unsupported == ["foo"]
        assert len(r.issues) == 1

    def test_defaults_empty_lists(self):
        from utils.disk.host_health import ResolutionInfo

        r = ResolutionInfo()
        assert r.unsupported == []
        assert r.issues == []


class TestHostHealth:
    def test_valid_construction(self):
        from utils.disk.host_health import HostHealth, ResolutionInfo

        hh = HostHealth(
            swap_total_mb=1024.0,
            swap_used_mb=400.0,
            disk_life_used_pct=40.0,
            boot_ts=1_700_000_000.0,
            uptime_seconds=86400.0,
            resolution=ResolutionInfo(),
            fetched_at=time.time(),
        )
        assert hh.swap_total_mb == 1024.0
        assert hh.disk_life_used_pct == 40.0

    def test_disk_life_can_be_none(self):
        from utils.disk.host_health import HostHealth

        hh = HostHealth(
            swap_total_mb=0.0,
            swap_used_mb=0.0,
            disk_life_used_pct=None,
            boot_ts=None,
            uptime_seconds=None,
            resolution=None,
            fetched_at=time.time(),
        )
        assert hh.disk_life_used_pct is None
        assert hh.resolution is None


class TestParseSwap:
    def test_normal(self):
        from utils.disk.host_health import _parse_swap

        used, total = _parse_swap(_meminfo(swap_total_kb=1048576, swap_free_kb=630000))
        assert total == pytest.approx(1024.0, abs=0.1)
        assert used == pytest.approx((1048576 - 630000) / 1024.0, abs=0.1)

    def test_no_swap(self):
        from utils.disk.host_health import _parse_swap

        used, total = _parse_swap(_meminfo(swap_total_kb=0, swap_free_kb=0))
        assert total == 0.0
        assert used == 0.0

    def test_missing_swap_lines(self):
        from utils.disk.host_health import _parse_swap

        used, total = _parse_swap("MemTotal: 1000000 kB\n")
        assert total == 0.0
        assert used == 0.0


class TestParseHostInfoJson:
    def test_disk_life_and_boot_ts(self):
        from utils.disk.host_health import _parse_host_info_json

        life, boot = _parse_host_info_json(
            _host_info_json(disk_life=40.0, boot_ts_us=1_700_000_000_000_000)
        )
        assert life == pytest.approx(40.0)
        assert boot == pytest.approx(1_700_000_000.0)

    def test_missing_disk_life_returns_none(self):
        from utils.disk.host_health import _parse_host_info_json

        raw = json.dumps(
            {"result": "ok", "data": {"boot_timestamp": 1_700_000_000_000_000}}
        )
        life, boot = _parse_host_info_json(raw)
        assert life is None
        assert boot == pytest.approx(1_700_000_000.0)

    def test_invalid_json_returns_none_none(self):
        from utils.disk.host_health import _parse_host_info_json

        life, boot = _parse_host_info_json("not json")
        assert life is None
        assert boot is None

    def test_empty_string_returns_none_none(self):
        from utils.disk.host_health import _parse_host_info_json

        life, boot = _parse_host_info_json("")
        assert life is None
        assert boot is None


class TestParseResolutionJson:
    def test_healthy(self):
        from utils.disk.host_health import _parse_resolution_json

        res = _parse_resolution_json(_resolution_json())
        assert res is not None
        assert res.issues == []
        assert res.unhealthy == []

    def test_with_issue(self):
        from utils.disk.host_health import _parse_resolution_json

        issues = [
            {
                "type": "rpi_firmware_update_blocked",
                "context": {},
                "reference": None,
                "uuid": "abcd1234abcd1234abcd1234abcd1234",
            }
        ]
        res = _parse_resolution_json(_resolution_json(issues=issues))
        assert res is not None
        assert len(res.issues) == 1
        assert res.issues[0]["type"] == "rpi_firmware_update_blocked"

    def test_invalid_json_returns_none(self):
        from utils.disk.host_health import _parse_resolution_json

        assert _parse_resolution_json("not json") is None


class TestFetchHostHealth:
    def _ssh(
        self,
        host_raw: str = "",
        res_raw: str = "",
        mem_raw: str = "",
    ):
        return _batch_ssh(host_raw=host_raw, res_raw=res_raw, mem_raw=mem_raw)

    def test_full_fetch(self):
        from utils.disk.host_health import fetch_host_health

        ssh = self._ssh(
            host_raw=_host_info_json(disk_life=40.0, boot_ts_us=1_700_000_000_000_000),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(swap_total_kb=1048576, swap_free_kb=630000),
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.disk_life_used_pct == pytest.approx(40.0)
        assert hh.boot_ts == pytest.approx(1_700_000_000.0)
        assert hh.swap_total_mb == pytest.approx(1024.0, abs=0.1)
        assert hh.uptime_seconds is not None and hh.uptime_seconds > 0
        assert hh.resolution is not None
        assert hh.errors == []

    def test_missing_disk_life_when_key_absent(self):
        from utils.disk.host_health import fetch_host_health

        host_raw = json.dumps(
            {"result": "ok", "data": {"boot_timestamp": 1_700_000_000_000_000}}
        )
        ssh = self._ssh(
            host_raw=host_raw, res_raw=_resolution_json(), mem_raw=_meminfo()
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.disk_life_used_pct is None
        assert hh.boot_ts is not None

    def test_empty_sections_populate_errors(self):
        from utils.disk.host_health import fetch_host_health

        ssh = self._ssh()  # no sections → all empty
        hh = asyncio.run(fetch_host_health(ssh))
        assert "host" in hh.errors
        assert "res" in hh.errors
        assert "mem" in hh.errors
        assert hh.disk_life_used_pct is None
        assert hh.resolution is None

    def test_no_swap_board(self):
        from utils.disk.host_health import fetch_host_health

        ssh = self._ssh(
            host_raw=_host_info_json(),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(swap_total_kb=0, swap_free_kb=0),
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.swap_total_mb == 0.0
        assert hh.swap_used_mb == 0.0


class TestHostHealthCache:
    def test_update_and_get(self):
        import importlib
        import utils.disk.host_health as mod

        importlib.reload(mod)
        assert mod.get_host_health() is None
        from utils.disk.host_health import HostHealth

        hh = HostHealth(
            swap_total_mb=1024.0,
            swap_used_mb=200.0,
            disk_life_used_pct=40.0,
            boot_ts=1_700_000_000.0,
            uptime_seconds=3600.0,
            resolution=None,
            fetched_at=time.time(),
        )
        mod.update_host_health(hh)
        assert mod.get_host_health() is hh
