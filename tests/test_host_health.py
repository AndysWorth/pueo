"""Tests for utils/disk/host_health.py."""

import asyncio
import json
import time

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_status(
    mem_available_mb: float = 600.0,
    mem_total_mb: float = 1800.0,
    mem_warn: bool = False,
):
    from utils.disk.resource import ResourceStatus

    return ResourceStatus(
        disk_free_gb=5.0,
        disk_total_gb=13.0,
        disk_used_gb=8.0,
        mem_available_mb=mem_available_mb,
        mem_total_mb=mem_total_mb,
        disk_warn=False,
        disk_critical=False,
        mem_warn=mem_warn,
    )


def _make_health(
    swap_total_mb: float = 1024.0,
    swap_used_mb: float = 200.0,
    disk_life_used_pct=None,
    boot_ts: float = 1_700_000_000.0,
    uptime_seconds: float = 3600.0,
    resolution=None,
    psi_some_avg300=None,
    psi_full_avg300=None,
    oom_kill_total=None,
):
    from utils.disk.host_health import HostHealth

    return HostHealth(
        swap_total_mb=swap_total_mb,
        swap_used_mb=swap_used_mb,
        disk_life_used_pct=disk_life_used_pct,
        boot_ts=boot_ts,
        uptime_seconds=uptime_seconds,
        resolution=resolution,
        fetched_at=time.time(),
        psi_some_avg300=psi_some_avg300,
        psi_full_avg300=psi_full_avg300,
        oom_kill_total=oom_kill_total,
    )


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


def _rpi_firmware_json(
    current: str = "2021-12-02",
    latest: str = "2021-12-02",
    update_available: bool = False,
    update_blocked: bool = True,
    blocked_reason: str = "eeprom_update_unavailable",
) -> str:
    return json.dumps(
        {
            "result": "ok",
            "data": {
                "current": current,
                "latest": latest,
                "update_available": update_available,
                "update_blocked": update_blocked,
                "blocked_reason": blocked_reason,
            },
        }
    )


def _batch_ssh(
    host_raw: str = "",
    res_raw: str = "",
    mem_raw: str = "",
    fw_raw: str = "",
    psi_raw: str = "",
    vm_raw: str = "",
    glued: bool = False,
):
    from utils.ha.ssh_client import FakeSSHClient

    parts = [
        "echo; echo '@@host'; ha host info --raw-json 2>/dev/null",
        "echo; echo '@@res'; ha resolution info --raw-json 2>/dev/null",
        "echo; echo '@@mem'; cat /proc/meminfo",
        "echo; echo '@@fw'; ha os boards rpi firmware --raw-json 2>/dev/null",
        "echo; echo '@@psi'; cat /proc/pressure/memory 2>/dev/null",
        "echo; echo '@@vm'; grep -E '^(oom_kill|pswpin|pswpout) ' /proc/vmstat 2>/dev/null",
    ]
    batch_cmd = "; ".join(parts)
    if glued:
        # Simulate ha --raw-json with no trailing newline: marker glued to end of JSON
        output = ""
        if host_raw:
            output += f"@@host\n{host_raw}@@res\n"
            if res_raw:
                output += f"{res_raw}@@mem\n"
                if mem_raw:
                    output += mem_raw
            return FakeSSHClient(command_results={batch_cmd: (0, output, "")})
        # Fall through to normal builder for partial glued tests
    output = ""
    if host_raw:
        output += f"@@host\n{host_raw}\n"
    if res_raw:
        output += f"@@res\n{res_raw}\n"
    if mem_raw:
        output += f"@@mem\n{mem_raw}\n"
    if fw_raw:
        output += f"@@fw\n{fw_raw}\n"
    if psi_raw:
        output += f"@@psi\n{psi_raw}\n"
    if vm_raw:
        output += f"@@vm\n{vm_raw}\n"
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


class TestParsePsi:
    def test_both_lines_parsed(self):
        from utils.disk.host_health import _parse_psi

        raw = (
            "some avg10=1.23 avg60=4.56 avg300=12.50 total=999\n"
            "full avg10=0.00 avg60=0.00 avg300=0.75 total=0\n"
        )
        some, full = _parse_psi(raw)
        assert some == pytest.approx(12.50)
        assert full == pytest.approx(0.75)

    def test_absent_file_returns_none_pair(self):
        from utils.disk.host_health import _parse_psi

        some, full = _parse_psi("")
        assert some is None
        assert full is None

    def test_garbled_line_skipped(self):
        from utils.disk.host_health import _parse_psi

        raw = "some avg10=GARBAGE avg60=1.0 avg300=bad total=0\n"
        some, full = _parse_psi(raw)
        assert some is None

    def test_exact_zero_avg300(self):
        from utils.disk.host_health import _parse_psi

        raw = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        some, full = _parse_psi(raw)
        assert some == pytest.approx(0.0)


class TestParseVmstatOom:
    def test_normal(self):
        from utils.disk.host_health import _parse_vmstat_oom

        raw = "pswpin 123\noom_kill 5\npswpout 456\n"
        assert _parse_vmstat_oom(raw) == 5

    def test_zero_oom_kill(self):
        from utils.disk.host_health import _parse_vmstat_oom

        assert _parse_vmstat_oom("oom_kill 0\n") == 0

    def test_absent_key_returns_none(self):
        from utils.disk.host_health import _parse_vmstat_oom

        assert _parse_vmstat_oom("pswpin 10\npswpout 20\n") is None

    def test_empty_returns_none(self):
        from utils.disk.host_health import _parse_vmstat_oom

        assert _parse_vmstat_oom("") is None


class TestParseHostInfoJson:
    def test_disk_life_and_boot_ts(self):
        from utils.disk.host_health import _parse_host_info_json

        life, boot, ok = _parse_host_info_json(
            _host_info_json(disk_life=40.0, boot_ts_us=1_700_000_000_000_000)
        )
        assert life == pytest.approx(40.0)
        assert boot == pytest.approx(1_700_000_000.0)
        assert ok is True

    def test_missing_disk_life_returns_none(self):
        from utils.disk.host_health import _parse_host_info_json

        raw = json.dumps(
            {"result": "ok", "data": {"boot_timestamp": 1_700_000_000_000_000}}
        )
        life, boot, ok = _parse_host_info_json(raw)
        assert life is None
        assert boot == pytest.approx(1_700_000_000.0)
        assert ok is True

    def test_invalid_json_returns_false_ok(self):
        from utils.disk.host_health import _parse_host_info_json

        life, boot, ok = _parse_host_info_json("not json")
        assert life is None
        assert boot is None
        assert ok is False

    def test_empty_string_returns_false_ok(self):
        from utils.disk.host_health import _parse_host_info_json

        life, boot, ok = _parse_host_info_json("")
        assert life is None
        assert boot is None
        assert ok is False


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
        fw_raw: str = "",
        glued: bool = False,
    ):
        return _batch_ssh(
            host_raw=host_raw,
            res_raw=res_raw,
            mem_raw=mem_raw,
            fw_raw=fw_raw,
            glued=glued,
        )

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

    def test_glued_markers_parse_all_sections(self):
        # Regression: ha --raw-json emits no trailing newline, so @@marker is glued
        # to the end of the JSON line. split_marker_sections() must still find all keys.
        from utils.disk.host_health import fetch_host_health

        ssh = self._ssh(
            host_raw=_host_info_json(disk_life=55.0, boot_ts_us=1_700_000_000_000_000),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(swap_total_kb=1048576, swap_free_kb=600000),
            glued=True,
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.disk_life_used_pct == pytest.approx(55.0)
        assert hh.boot_ts == pytest.approx(1_700_000_000.0)
        assert hh.resolution is not None
        assert hh.swap_total_mb == pytest.approx(1024.0, abs=0.1)
        assert hh.errors == []

    def test_host_parse_failure_adds_host_to_errors(self):
        # Non-empty host section that is not valid JSON should add "host" to errors
        from utils.disk.host_health import fetch_host_health
        from utils.ha.ssh_client import FakeSSHClient

        # Build output with host section containing truncated/invalid JSON
        parts = [
            "echo; echo '@@host'; ha host info --raw-json 2>/dev/null",
            "echo; echo '@@res'; ha resolution info --raw-json 2>/dev/null",
            "echo; echo '@@mem'; cat /proc/meminfo",
            "echo; echo '@@fw'; ha os boards rpi firmware --raw-json 2>/dev/null",
            "echo; echo '@@psi'; cat /proc/pressure/memory 2>/dev/null",
            "echo; echo '@@vm'; grep -E '^(oom_kill|pswpin|pswpout) ' /proc/vmstat 2>/dev/null",
        ]
        batch_cmd = "; ".join(parts)
        output = (
            f"@@host\nnot valid json here\n"
            f"@@res\n{_resolution_json()}\n"
            f"@@mem\n{_meminfo()}\n"
        )
        ssh = FakeSSHClient(command_results={batch_cmd: (0, output, "")})
        hh = asyncio.run(fetch_host_health(ssh))
        assert "host" in hh.errors
        assert hh.disk_life_used_pct is None
        assert hh.resolution is not None
        assert "mem" not in hh.errors
        assert "res" not in hh.errors


class TestFetchHostHealthPsiVm:
    def test_psi_and_vmstat_parsed(self):
        import asyncio

        from utils.disk.host_health import fetch_host_health

        psi_raw = "some avg10=0.50 avg60=1.00 avg300=12.75 total=5000\nfull avg10=0.00 avg60=0.00 avg300=0.25 total=10\n"
        vm_raw = "pswpin 100\noom_kill 3\npswpout 200\n"
        ssh = _batch_ssh(
            host_raw=_host_info_json(),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(),
            psi_raw=psi_raw,
            vm_raw=vm_raw,
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.psi_some_avg300 == pytest.approx(12.75)
        assert hh.psi_full_avg300 == pytest.approx(0.25)
        assert hh.oom_kill_total == 3

    def test_absent_psi_and_vmstat_gives_none_fields(self):
        import asyncio

        from utils.disk.host_health import fetch_host_health

        ssh = _batch_ssh(
            host_raw=_host_info_json(),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(),
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.psi_some_avg300 is None
        assert hh.oom_kill_total is None


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


# ---------------------------------------------------------------------------
# HostHealthFinding
# ---------------------------------------------------------------------------


class TestHostHealthFinding:
    def test_construction(self):
        from utils.disk.host_health import HostHealthFinding

        f = HostHealthFinding(
            key="memory_pressure", kind="memory_pressure", severity="WARN"
        )
        assert f.key == "memory_pressure"
        assert f.kind == "memory_pressure"
        assert f.severity == "WARN"
        assert f.evidence == {}

    def test_construction_with_evidence(self):
        from utils.disk.host_health import HostHealthFinding

        f = HostHealthFinding(
            key="disk_wear",
            kind="disk_wear",
            severity="CRITICAL",
            evidence={"disk_life_used_pct": 95.0},
        )
        assert f.evidence["disk_life_used_pct"] == 95.0


# ---------------------------------------------------------------------------
# evaluate_host_health
# ---------------------------------------------------------------------------


class TestEvaluateHostHealth:
    def _eval(
        self,
        health=None,
        status=None,
        last_boot_ts=1_700_000_000.0,
        pueo_reboot_recent=False,
        known_repair_uuids=None,
        disk_life_warn_pct=80.0,
        mem_warn_mb=256.0,
        mem_psi_warn_pct=10.0,
        oom_kill_prev=None,
    ):
        from utils.disk.host_health import evaluate_host_health

        return evaluate_host_health(
            health=health or _make_health(),
            status=status or _make_status(),
            last_boot_ts=last_boot_ts,
            pueo_reboot_recent=pueo_reboot_recent,
            known_repair_uuids=known_repair_uuids or set(),
            disk_life_warn_pct=disk_life_warn_pct,
            mem_warn_mb=mem_warn_mb,
            mem_psi_warn_pct=mem_psi_warn_pct,
            oom_kill_prev=oom_kill_prev,
        )

    def test_no_findings_all_clear(self):
        findings = self._eval()
        assert findings == []

    def test_memory_low(self):
        findings = self._eval(
            status=_make_status(mem_warn=True, mem_available_mb=100.0)
        )
        keys = [f.key for f in findings]
        assert "memory_low" in keys
        mf = next(f for f in findings if f.key == "memory_low")
        assert mf.severity == "WARN"
        assert mf.evidence["mem_available_mb"] == 100

    def test_memory_pressure_psi_warn(self):
        # PSI some avg300 at threshold → memory_pressure WARN
        h = _make_health(psi_some_avg300=12.5)
        findings = self._eval(health=h, mem_psi_warn_pct=10.0)
        assert any(f.key == "memory_pressure" for f in findings)
        mp = next(f for f in findings if f.key == "memory_pressure")
        assert mp.severity == "WARN"
        assert mp.evidence["psi_some_avg300"] == 12.5
        assert mp.evidence["threshold_psi"] == 10.0
        assert "swap_used_mb" in mp.evidence

    def test_memory_pressure_psi_below_threshold(self):
        h = _make_health(psi_some_avg300=5.0)
        findings = self._eval(health=h, mem_psi_warn_pct=10.0)
        assert not any(f.kind == "memory_pressure" for f in findings)

    def test_memory_pressure_psi_absent(self):
        # No PSI file — no memory_pressure from PSI
        h = _make_health(psi_some_avg300=None)
        findings = self._eval(health=h, mem_psi_warn_pct=10.0)
        assert not any(f.kind == "memory_pressure" for f in findings)

    def test_memory_pressure_oom_kill_delta_high(self):
        # OOM kill counter increased → HIGH
        h = _make_health(oom_kill_total=3)
        findings = self._eval(health=h, oom_kill_prev=1)
        assert any(f.key == "memory_pressure" for f in findings)
        mp = next(f for f in findings if f.key == "memory_pressure")
        assert mp.severity == "HIGH"
        assert mp.evidence["oom_kill_delta"] == 2
        assert mp.evidence["oom_kill_total"] == 3

    def test_memory_pressure_oom_no_delta(self):
        # Same total — no trigger
        h = _make_health(oom_kill_total=3)
        findings = self._eval(health=h, oom_kill_prev=3)
        assert not any(f.kind == "memory_pressure" for f in findings)

    def test_memory_pressure_oom_prev_none_no_trigger(self):
        # First poll after boot: prev is None → no OOM trigger even if total > 0
        h = _make_health(oom_kill_total=5)
        findings = self._eval(health=h, oom_kill_prev=None)
        assert not any(f.kind == "memory_pressure" for f in findings)

    def test_memory_pressure_psi_and_oom_severity_high(self):
        # Both PSI and OOM → HIGH wins
        h = _make_health(psi_some_avg300=15.0, oom_kill_total=2)
        findings = self._eval(health=h, mem_psi_warn_pct=10.0, oom_kill_prev=1)
        mp = next(f for f in findings if f.key == "memory_pressure")
        assert mp.severity == "HIGH"

    def test_swap_figures_in_memory_pressure_evidence(self):
        h = _make_health(swap_total_mb=1024.0, swap_used_mb=943.0, psi_some_avg300=12.0)
        findings = self._eval(health=h, mem_psi_warn_pct=10.0)
        mp = next(f for f in findings if f.key == "memory_pressure")
        assert mp.evidence["swap_used_mb"] == 943.0
        assert mp.evidence["swap_total_mb"] == 1024.0

    def test_disk_wear_warn(self):
        h = _make_health(disk_life_used_pct=85.0)
        findings = self._eval(health=h)
        assert any(f.key == "disk_wear" for f in findings)
        dw = next(f for f in findings if f.key == "disk_wear")
        assert dw.severity == "WARN"

    def test_disk_wear_critical_at_90(self):
        h = _make_health(disk_life_used_pct=92.0)
        findings = self._eval(health=h)
        dw = next(f for f in findings if f.key == "disk_wear")
        assert dw.severity == "CRITICAL"

    def test_disk_wear_none_skipped(self):
        h = _make_health(disk_life_used_pct=None)
        findings = self._eval(health=h)
        assert not any(f.kind == "disk_wear" for f in findings)

    def test_disk_wear_below_threshold(self):
        h = _make_health(disk_life_used_pct=40.0)
        findings = self._eval(health=h)
        assert not any(f.kind == "disk_wear" for f in findings)

    def test_unexpected_reboot_detected(self):
        # boot_ts changed significantly from stored value
        h = _make_health(boot_ts=1_700_100_000.0)
        findings = self._eval(
            health=h,
            last_boot_ts=1_700_000_000.0,
            pueo_reboot_recent=False,
        )
        assert any(f.key == "unexpected_reboot" for f in findings)

    def test_unexpected_reboot_suppressed_when_pueo_recent(self):
        h = _make_health(boot_ts=1_700_100_000.0)
        findings = self._eval(
            health=h,
            last_boot_ts=1_700_000_000.0,
            pueo_reboot_recent=True,
        )
        assert not any(f.kind == "unexpected_reboot" for f in findings)

    def test_unexpected_reboot_no_stored_ts(self):
        # If no stored boot_ts, can't detect unexpected reboot
        h = _make_health(boot_ts=1_700_100_000.0)
        findings = self._eval(health=h, last_boot_ts=None)
        assert not any(f.kind == "unexpected_reboot" for f in findings)

    def test_unexpected_reboot_within_tolerance(self):
        # Clock drift < 30 s should not fire
        h = _make_health(boot_ts=1_700_000_010.0)
        findings = self._eval(health=h, last_boot_ts=1_700_000_000.0)
        assert not any(f.kind == "unexpected_reboot" for f in findings)

    def test_supervisor_unhealthy(self):
        from utils.disk.host_health import ResolutionInfo

        h = _make_health(resolution=ResolutionInfo(unhealthy=["watchdog_failed"]))
        findings = self._eval(health=h)
        uf = [f for f in findings if f.kind == "supervisor_unhealthy"]
        assert len(uf) == 1
        assert "watchdog_failed" in uf[0].key
        assert uf[0].severity == "HIGH"

    def test_supervisor_unsupported(self):
        from utils.disk.host_health import ResolutionInfo

        h = _make_health(resolution=ResolutionInfo(unsupported=["unsupported_os"]))
        findings = self._eval(health=h)
        uf = [f for f in findings if f.kind == "supervisor_unsupported"]
        assert len(uf) == 1
        assert uf[0].severity == "WARN"

    def test_resolution_issue(self):
        from utils.disk.host_health import ResolutionInfo

        uuid = "abc123def456abc123def456abc12345"
        h = _make_health(
            resolution=ResolutionInfo(
                issues=[{"uuid": uuid, "type": "rpi_firmware_update_blocked"}]
            )
        )
        findings = self._eval(health=h)
        rf = [f for f in findings if f.kind == "resolution_issue"]
        assert len(rf) == 1
        assert rf[0].key == f"resolution_issue:{uuid}"
        assert rf[0].evidence["type"] == "rpi_firmware_update_blocked"

    def test_resolution_issue_deduped_by_repair(self):
        from utils.disk.host_health import ResolutionInfo

        uuid = "abc123def456abc123def456abc12345"
        h = _make_health(
            resolution=ResolutionInfo(
                issues=[{"uuid": uuid, "type": "rpi_firmware_update_blocked"}]
            )
        )
        findings = self._eval(health=h, known_repair_uuids={uuid})
        assert not any(f.kind == "resolution_issue" for f in findings)

    def test_resolution_issue_missing_uuid_skipped(self):
        from utils.disk.host_health import ResolutionInfo

        h = _make_health(resolution=ResolutionInfo(issues=[{"type": "some_issue"}]))
        findings = self._eval(health=h)
        assert not any(f.kind == "resolution_issue" for f in findings)

    def test_multiple_findings(self):
        from utils.disk.host_health import ResolutionInfo

        h = _make_health(
            disk_life_used_pct=85.0,
            swap_total_mb=1024.0,
            swap_used_mb=850.0,
            psi_some_avg300=12.0,
            resolution=ResolutionInfo(unhealthy=["something_bad"]),
        )
        s = _make_status(mem_available_mb=400.0, mem_warn=True)
        findings = self._eval(health=h, status=s, mem_psi_warn_pct=10.0)
        kinds = [f.kind for f in findings]
        assert "memory_low" in kinds
        assert "memory_pressure" in kinds
        assert "disk_wear" in kinds
        assert "supervisor_unhealthy" in kinds


# ---------------------------------------------------------------------------
# ResourcePoller._check_host_health helpers
# ---------------------------------------------------------------------------


class TestResourcePollerHostHealthHelpers:
    def _make_poller(self, tmp_path):
        import sqlite3

        from utils.disk.resource import ResourcePoller
        from utils.hitl.notify import FakeNotifier

        db = str(tmp_path / "state.db")
        # Create required tables
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS timeline_events"
                " (id INTEGER PRIMARY KEY, timestamp REAL, event_type TEXT,"
                " event_category TEXT, message TEXT, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ha_repair_history"
                " (issue_key TEXT PRIMARY KEY, first_seen_at REAL,"
                " hitl_sent_at REAL, resolved_at REAL)"
            )
        return ResourcePoller(
            ssh_client=object(),
            notifier=FakeNotifier(),
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=3.0,
            mem_warn_mb=256.0,
            db_path=db,
        )

    def test_read_write_last_boot_ts(self, tmp_path):
        poller = self._make_poller(tmp_path)
        db = str(tmp_path / "state.db")
        assert poller._read_last_boot_ts(db) is None
        poller._write_last_boot_ts(db, 1_700_000_000.0)
        assert poller._read_last_boot_ts(db) == pytest.approx(1_700_000_000.0)
        # Update
        poller._write_last_boot_ts(db, 1_700_100_000.0)
        assert poller._read_last_boot_ts(db) == pytest.approx(1_700_100_000.0)

    def test_check_pueo_reboot_recent_false_no_action(self, tmp_path):
        poller = self._make_poller(tmp_path)
        db = str(tmp_path / "state.db")
        recent, detail = poller._check_pueo_reboot_recent(db)
        assert not recent
        assert detail == ""

    def test_check_pueo_reboot_recent_true(self, tmp_path):
        from utils.ha.device_availability import record_pueo_action

        poller = self._make_poller(tmp_path)
        db = str(tmp_path / "state.db")
        record_pueo_action(db, "core_restart", "ha core restart")
        recent, detail = poller._check_pueo_reboot_recent(db)
        assert recent
        assert "core_restart" in detail

    def test_check_pueo_reboot_recent_outside_window(self, tmp_path):
        import json
        import time

        from utils.ha.device_availability import record_pueo_action

        poller = self._make_poller(tmp_path)
        db = str(tmp_path / "state.db")
        # Write an action with a timestamp well outside the window
        import sqlite3 as _sqlite3

        old_ts = time.time() - 3000
        with _sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    "pueo_action:last_restart",
                    json.dumps(
                        {
                            "ts": old_ts,
                            "kind": "core_restart",
                            "detail": "ha core restart",
                        }
                    ),
                ),
            )
        recent, detail = poller._check_pueo_reboot_recent(db)
        assert not recent

    def test_load_known_repair_uuids(self, tmp_path):
        import sqlite3

        poller = self._make_poller(tmp_path)
        db = str(tmp_path / "state.db")
        uuid = "deadbeef" * 4
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO ha_repair_history (issue_key, first_seen_at) VALUES (?, ?)",
                (f"hassio/{uuid}", 1.0),
            )
        uuids = poller._load_known_repair_uuids(db)
        assert uuid in uuids

    def test_load_known_repair_uuids_resolved_excluded(self, tmp_path):
        import sqlite3
        import time

        poller = self._make_poller(tmp_path)
        db = str(tmp_path / "state.db")
        uuid = "deadbeef" * 4
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO ha_repair_history (issue_key, first_seen_at, resolved_at) VALUES (?, ?, ?)",
                (f"hassio/{uuid}", 1.0, time.time()),
            )
        uuids = poller._load_known_repair_uuids(db)
        assert uuid not in uuids


# ---------------------------------------------------------------------------
# ResourcePoller._check_host_health integration
# ---------------------------------------------------------------------------


class TestResourcePollerCheckHostHealth:
    def _make_db(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS timeline_events"
                " (id INTEGER PRIMARY KEY, timestamp REAL, event_type TEXT,"
                " event_category TEXT, message TEXT, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ha_repair_history"
                " (issue_key TEXT PRIMARY KEY, first_seen_at REAL,"
                " hitl_sent_at REAL, resolved_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hitl_suppression"
                " (card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '',"
                " description TEXT NOT NULL DEFAULT '',"
                " first_sent_at REAL NOT NULL DEFAULT 0,"
                " last_sent_at REAL NOT NULL DEFAULT 0,"
                " send_count INTEGER NOT NULL DEFAULT 1,"
                " last_action TEXT, last_action_at REAL,"
                " rejection_count INTEGER NOT NULL DEFAULT 0,"
                " next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0,"
                " known_issue_note TEXT, resolved_at REAL)"
            )
        return db

    def _make_poller(self, tmp_path, notifier=None, llm_client=None):
        from utils.disk.resource import ResourcePoller
        from utils.hitl.notify import FakeNotifier

        db = self._make_db(tmp_path)
        return (
            ResourcePoller(
                ssh_client=object(),
                notifier=notifier or FakeNotifier(),
                interval_seconds=300,
                disk_warn_gb=5.0,
                disk_critical_gb=3.0,
                mem_warn_mb=256.0,
                llm_client=llm_client,
                db_path=db,
            ),
            db,
        )

    def test_no_health_cached_does_nothing(self, tmp_path, monkeypatch):
        import utils.disk.host_health as hh_mod

        monkeypatch.setattr(hh_mod, "_last_host_health", None)
        poller, _ = self._make_poller(tmp_path)
        status = _make_status()
        import asyncio

        asyncio.run(poller._check_host_health(status))
        # Should complete without error

    def test_fallback_notification_when_investigation_disabled(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=85.0)
        )

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        asyncio.run(poller._check_host_health(_make_status()))
        assert len(notifier.sent) == 1
        assert "disk_wear" in notifier.sent[0]["payload"]["kind"]

    def test_boot_change_records_ha_start(self, tmp_path, monkeypatch):
        """A changed boot_ts records an HA start for the device settle window."""
        import asyncio
        import sqlite3

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.ha.device_availability import load_ha_start_ts

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        poller, db = self._make_poller(tmp_path)

        monkeypatch.setattr(hh_mod, "_last_host_health", _make_health())
        asyncio.run(poller._check_host_health(_make_status()))
        with sqlite3.connect(db) as conn:
            assert load_ha_start_ts(conn) is None  # first sighting is not a boot

        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(boot_ts=1_700_050_000.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))
        with sqlite3.connect(db) as conn:
            assert load_ha_start_ts(conn) == 1_700_050_000.0

    def test_dedup_prevents_second_notification(self, tmp_path, monkeypatch):
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=85.0)
        )

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        asyncio.run(poller._check_host_health(_make_status()))
        asyncio.run(poller._check_host_health(_make_status()))
        assert len(notifier.sent) == 1  # second poll deduped via _alerted_host

    def test_clear_resolved_finding(self, tmp_path, monkeypatch):
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        # Fire finding
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=85.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))
        assert "disk_wear" in poller._alerted_host

        # Finding clears
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=40.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))
        assert "disk_wear" not in poller._alerted_host

    def test_clear_runs_verifier_and_survives_its_failure(self, tmp_path, monkeypatch):
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        import utils.disk.resource as res_mod
        from unittest import mock

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        verify = mock.AsyncMock(side_effect=[0, RuntimeError("boom")])
        monkeypatch.setattr(res_mod, "verify_host_health_sessions", verify)
        poller, _ = self._make_poller(tmp_path)

        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=85.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))
        assert verify.await_args.kwargs["active"] == {"disk_wear"}
        assert verify.await_args.kwargs["cleared"] == set()

        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=40.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))  # error is logged
        assert verify.await_args.kwargs["cleared"] == {"disk_wear"}
        assert "disk_wear" not in poller._alerted_host

    def test_backoff_blocks_submission_without_alerted(self, tmp_path, monkeypatch):
        """Investigation backoff row suppresses submission; key stays out of _alerted_host."""
        import asyncio
        import sqlite3

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.hitl_tracker import mark_investigation_backoff
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=85.0)
        )

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        # Pre-insert a 30-min backoff for the disk_wear finding
        with sqlite3.connect(db) as conn:
            mark_investigation_backoff(conn, "host_health_backoff:disk_wear")

        asyncio.run(poller._check_host_health(_make_status()))

        # Backoff blocked the submission
        assert len(notifier.sent) == 0
        # Key is NOT added to _alerted_host so next poll can retry once backoff expires
        assert "disk_wear" not in poller._alerted_host

    def test_no_backoff_allows_submission(self, tmp_path, monkeypatch):
        """Without a backoff row the finding proceeds to notification."""
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(disk_life_used_pct=85.0)
        )

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        asyncio.run(poller._check_host_health(_make_status()))

        assert len(notifier.sent) == 1
        assert "disk_wear" in poller._alerted_host

    def test_boot_change_with_pueo_action_writes_timeline_not_investigation(
        self, tmp_path, monkeypatch
    ):
        """Boot_ts changed and a Pueo action is within the window: write_timeline_event
        is called with an 'Expected reboot' message and the unexpected_reboot finding is
        suppressed (no notification sent)."""
        import asyncio
        from unittest.mock import MagicMock

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.ha.device_availability import record_pueo_action

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "PUEO_ACTION_REBOOT_WINDOW_MINUTES", 20)
        poller, db = self._make_poller(tmp_path)

        # Capture calls to write_timeline_event; it uses config.DB_PATH which the test
        # cannot easily redirect, so we patch the function itself in the source module.
        import utils.core.timeline as tl_mod

        timeline_calls: list[tuple] = []

        def _fake_write(level, source, message, detail=None):
            timeline_calls.append((level, source, message, detail))

        monkeypatch.setattr(tl_mod, "write_timeline_event", _fake_write)

        # First call: establish last_boot_ts baseline
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(boot_ts=1_700_000_000.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))

        # Record a Pueo action (simulate recent restart)
        record_pueo_action(db, "core_restart", "ha core restart")

        # Second call: boot_ts changed → looks like a reboot
        monkeypatch.setattr(
            hh_mod, "_last_host_health", _make_health(boot_ts=1_700_100_000.0)
        )
        asyncio.run(poller._check_host_health(_make_status()))

        # write_timeline_event should have been called with "Expected reboot …"
        reboot_calls = [c for c in timeline_calls if "Expected reboot" in c[2]]
        assert (
            len(reboot_calls) == 1
        ), f"Expected 1 'Expected reboot' call, got: {timeline_calls}"
        assert "core_restart" in reboot_calls[0][2]

    def test_confirm_counter_requires_n_polls_to_fire(self, tmp_path, monkeypatch):
        """memory_low WARN needs HOST_HEALTH_CONFIRM_POLLS consecutive polls before firing."""
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 3)

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)

        monkeypatch.setattr(hh_mod, "_last_host_health", health)
        # Polls 1 and 2: not yet at threshold
        asyncio.run(poller._check_host_health(status_warn))
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 0
        assert "memory_low" not in poller._alerted_host

        # Poll 3: reaches confirm threshold → fires
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 1
        assert "memory_low" in poller._alerted_host

    def test_confirm_counter_resets_on_absent_poll(self, tmp_path, monkeypatch):
        """An absent poll resets the confirm counter; the finding must re-confirm from 0."""
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 3)

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)
        status_ok = _make_status(mem_available_mb=800.0, mem_warn=False)

        monkeypatch.setattr(hh_mod, "_last_host_health", health)
        asyncio.run(poller._check_host_health(status_warn))
        asyncio.run(poller._check_host_health(status_warn))
        # Clear — counter resets
        asyncio.run(poller._check_host_health(status_ok))
        # Two more warn polls — not yet at 3 again
        asyncio.run(poller._check_host_health(status_warn))
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 0

    def test_clear_requires_n_absent_polls_for_memory_kind(self, tmp_path, monkeypatch):
        """memory_low stays in _alerted_host until HOST_HEALTH_CONFIRM_POLLS absent polls.

        Set HOST_HEALTH_REINVESTIGATE_HOURS=0 so the recurrence clear-delay window is
        instantly elapsed and the absent-poll counter governs resolution as before S3.
        """
        import asyncio

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 2)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_REINVESTIGATE_HOURS", 0)

        notifier = FakeNotifier()
        poller, _ = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)
        status_ok = _make_status(mem_available_mb=800.0, mem_warn=False)

        monkeypatch.setattr(hh_mod, "_last_host_health", health)
        # Fire (2 polls to confirm)
        asyncio.run(poller._check_host_health(status_warn))
        asyncio.run(poller._check_host_health(status_warn))
        assert "memory_low" in poller._alerted_host

        # 1 absent poll — not yet cleared (confirm counter, not window)
        asyncio.run(poller._check_host_health(status_ok))
        assert "memory_low" in poller._alerted_host

        # 2nd absent poll — now clears (window=0 so clear-delay passes immediately)
        asyncio.run(poller._check_host_health(status_ok))
        assert "memory_low" not in poller._alerted_host

    def test_oom_kill_delta_fires_immediately_bypassing_confirm(
        self, tmp_path, monkeypatch
    ):
        """HIGH memory_pressure (OOM kill) fires on the first poll without waiting."""
        import asyncio
        import sqlite3

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 3)

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        # Seed a previous OOM total so the delta is detected
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES ('host_health:oom_kill_total', '1')"
            )

        health = _make_health(oom_kill_total=3)
        monkeypatch.setattr(hh_mod, "_last_host_health", health)

        asyncio.run(poller._check_host_health(_make_status()))

        assert len(notifier.sent) == 1
        assert "memory_pressure" in poller._alerted_host
        payload = notifier.sent[0]["payload"]
        assert payload.get("severity") == "HIGH"

    def test_oom_kill_total_reset_on_boot_change(self, tmp_path, monkeypatch):
        """When boot_ts changes the stored OOM total is cleared (no false OOM delta)."""
        import asyncio
        import sqlite3

        import config as cfg_mod
        import utils.disk.host_health as hh_mod

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 1)

        poller, db = self._make_poller(tmp_path)

        # Initial state: boot_ts stored and OOM total = 2
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES ('host_health:last_boot_ts', '1700000000.0')"
            )
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES ('host_health:oom_kill_total', '2')"
            )

        # Health with changed boot_ts and oom_kill_total=5 (would trigger if prev=2)
        health = _make_health(boot_ts=1_700_100_000.0, oom_kill_total=5)
        monkeypatch.setattr(hh_mod, "_last_host_health", health)

        asyncio.run(poller._check_host_health(_make_status()))

        # OOM total should be reset to 5 (not trigger a delta with the old 2)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key='host_health:oom_kill_total'"
            ).fetchone()
        assert row is not None
        assert int(row[0]) == 5
        # No memory_pressure in alerted (OOM delta was suppressed by boot change)
        assert "memory_pressure" not in poller._alerted_host


# ---------------------------------------------------------------------------
# build_host_health_tool_registry
# ---------------------------------------------------------------------------


class TestBuildHostHealthToolRegistry:
    def test_expected_tools_registered(self):
        from utils.agent.tool_registry import build_host_health_tool_registry

        reg = build_host_health_tool_registry()
        names = set(reg.names())
        expected = {
            "get_disk_usage",
            "read_logs",
            "read_pueo_log",
            "search_log",
            "run_ha_command",
            "query_knowledge",
            "remember",
            "recall",
            "get_ha_profile",
            "call_service",
            "request_escalation",
            "discard_result",
            "finish_host_health",
        }
        assert names == expected

    def test_no_write_or_patch_tools(self):
        from utils.agent.tool_registry import build_host_health_tool_registry

        reg = build_host_health_tool_registry()
        names = set(reg.names())
        assert "write_config" not in names
        assert "propose_patch" not in names
        assert "sandbox_code" not in names


# ---------------------------------------------------------------------------
# _finish_host_health (ToolExecutor method)
# ---------------------------------------------------------------------------


def _make_hitl_db(tmp_path):
    """Create a minimal SQLite DB with hitl_suppression for finish_host_health tests."""
    import sqlite3

    db = str(tmp_path / "state.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE hitl_suppression"
            " (card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '',"
            " description TEXT NOT NULL DEFAULT '',"
            " first_sent_at REAL NOT NULL DEFAULT 0,"
            " last_sent_at REAL NOT NULL DEFAULT 0,"
            " send_count INTEGER NOT NULL DEFAULT 1,"
            " last_action TEXT, last_action_at REAL,"
            " rejection_count INTEGER NOT NULL DEFAULT 0,"
            " next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0,"
            " known_issue_note TEXT, resolved_at REAL)"
        )
    return db


class TestFinishHostHealth:
    def _make_executor(self, db, notifier=None):
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        return ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=notifier or FakeNotifier(),
            db_path=db,
        )

    def test_with_finding_sends_notification_payload(self, tmp_path):
        import asyncio

        from utils.disk.host_health import HostHealthFinding
        from utils.hitl.card_types import CARD_TYPE_HOST_HEALTH
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db, notifier=notifier)
        finding = HostHealthFinding(
            key="disk_wear",
            kind="disk_wear",
            severity="WARN",
            evidence={"disk_life_used_pct": 85.0},
        )
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Disk wear at 85%.",
                root_cause="eMMC aging.",
                outcome="monitoring",
                recommended_actions=[],
            )
        )

        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["card_type"] == CARD_TYPE_HOST_HEALTH
        assert payload["kind"] == "disk_wear"
        assert payload["severity"] == "WARN"
        assert payload["outcome"] == "monitoring"
        assert "disk_life_used_pct" in payload["evidence"]

    def test_with_finding_marks_hitl_row_sent(self, tmp_path):
        import asyncio
        import sqlite3

        from utils.disk.host_health import HostHealthFinding

        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db)
        finding = HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Disk wear at 85%.",
                root_cause="eMMC aging.",
                outcome="monitoring",
                recommended_actions=[],
            )
        )

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT card_key FROM hitl_suppression"
                " WHERE card_key = 'host_health:disk_wear'"
            ).fetchone()
        assert row is not None

    def test_without_finding_returns_summary_only(self, tmp_path):
        import asyncio

        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db, notifier=notifier)
        # No set_host_health_finding call → _host_health_finding is None

        result = asyncio.run(
            executor._finish_host_health(
                summary="Investigation complete.",
                root_cause="",
                outcome="monitoring",
                recommended_actions=[],
            )
        )

        assert result.success is True
        assert len(notifier.sent) == 0

    def test_resolved_resolution_issue_downgraded_when_still_present(self, tmp_path):
        """outcome='resolved' on a resolution_issue is overridden to 'needs_action'
        when ha resolution info still reports the issue UUID."""
        import asyncio
        import json

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.disk.host_health import HostHealthFinding
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        uuid = "aaaa-bbbb-cccc-dddd"
        res_json = json.dumps(
            {"data": {"issues": [{"uuid": uuid, "type": "no_current_backup"}]}}
        )
        ssh = FakeSSHClient(command_results={"ha resolution info": (0, res_json, "")})
        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(),
            notifier=notifier,
            db_path=db,
        )
        finding = HostHealthFinding(
            key=f"resolution_issue:{uuid}",
            kind="resolution_issue",
            severity="WARN",
            evidence={"uuid": uuid, "type": "no_current_backup"},
        )
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Issue dismissed.",
                root_cause="no full backup",
                outcome="resolved",
                recommended_actions=[],
            )
        )

        payload = notifier.sent[0]["payload"]
        assert (
            payload["outcome"] == "needs_action"
        ), "still-present issue must not be resolved"
        assert "[Re-check:" in notifier.sent[0]["body"]

    def test_resolved_resolution_issue_accepted_when_cleared(self, tmp_path):
        """outcome='resolved' is kept when ha resolution info confirms the issue is gone."""
        import asyncio
        import json

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_executor import ToolExecutor
        from utils.disk.host_health import HostHealthFinding
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        uuid = "aaaa-bbbb-cccc-dddd"
        # No issues in the response — issue has been cleared
        res_json = json.dumps({"data": {"issues": []}})
        ssh = FakeSSHClient(command_results={"ha resolution info": (0, res_json, "")})
        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(),
            notifier=notifier,
            db_path=db,
        )
        finding = HostHealthFinding(
            key=f"resolution_issue:{uuid}",
            kind="resolution_issue",
            severity="WARN",
            evidence={"uuid": uuid, "type": "no_current_backup"},
        )
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Issue resolved.",
                root_cause="no full backup",
                outcome="resolved",
                recommended_actions=[],
            )
        )

        payload = notifier.sent[0]["payload"]
        assert payload["outcome"] == "resolved"

    def test_resolved_non_resolution_issue_not_rechecked(self, tmp_path):
        """Non-resolution-issue findings skip the re-check and keep outcome='resolved'."""
        import asyncio

        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        from utils.disk.host_health import HostHealthFinding

        executor = self._make_executor(db, notifier=notifier)
        finding = HostHealthFinding(
            key="disk_wear",
            kind="disk_wear",
            severity="WARN",
            evidence={"disk_life_used_pct": 85.0},
        )
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Disk wear resolved.",
                root_cause="user replaced disk",
                outcome="resolved",
                recommended_actions=[],
            )
        )

        payload = notifier.sent[0]["payload"]
        assert payload["outcome"] == "resolved"

    def test_verification_question_and_fact_in_payload(self, tmp_path):
        import asyncio

        from utils.disk.host_health import HostHealthFinding
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db, notifier=notifier)
        finding = HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Disk wear at 85%.",
                root_cause="eMMC aging.",
                outcome="monitoring",
                recommended_actions=[],
                verification_question="Is disk wear still at 85%?",
                fact_if_yes="Host eMMC aged to 85% — monitor monthly.",
            )
        )

        payload = notifier.sent[0]["payload"]
        assert payload["verification_question"] == "Is disk wear still at 85%?"
        assert payload["fact_if_yes"] == "Host eMMC aged to 85% — monitor monthly."
        assert "signature" in payload

    def test_default_verification_question_when_omitted(self, tmp_path):
        import asyncio

        from utils.disk.host_health import HostHealthFinding
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db, notifier=notifier)
        finding = HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Disk wear at 85%.",
                root_cause="eMMC aging.",
                outcome="monitoring",
                recommended_actions=[],
            )
        )

        payload = notifier.sent[0]["payload"]
        # outcome=monitoring with no actions sets informational=True, so the default
        # question uses the informational branch.
        assert (
            payload["verification_question"]
            == "Is it right that nothing needs doing here?"
        )
        assert "fact_if_yes" not in payload
        assert "prediction" not in payload

    def test_informational_outcome_gets_default_question(self, tmp_path):
        """Informational host-health (e.g. rpi firmware) always gets a default question."""
        import asyncio

        from utils.disk.host_health import HostHealthFinding
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db, notifier=notifier)
        finding = HostHealthFinding(
            key="rpi_firmware", kind="rpi_firmware", severity="INFO"
        )
        executor.set_host_health_finding(finding)

        asyncio.run(
            executor._finish_host_health(
                summary="Firmware is current.",
                root_cause="No update needed.",
                outcome="informational",
                recommended_actions=[],
            )
        )

        payload = notifier.sent[0]["payload"]
        assert payload.get("informational") is True
        assert (
            payload["verification_question"]
            == "Is it right that nothing needs doing here?"
        )

    def test_prediction_in_payload(self, tmp_path):
        import asyncio

        from utils.agent.tool_registry import ToolCall

        from utils.disk.host_health import HostHealthFinding
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        db = _make_hitl_db(tmp_path)
        executor = self._make_executor(db, notifier=notifier)
        executor.set_host_health_finding(
            HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")
        )

        asyncio.run(
            executor.execute(
                ToolCall(
                    name="finish_host_health",
                    arguments={
                        "summary": "Disk wear at 85%.",
                        "root_cause": "eMMC aging.",
                        "outcome": "monitoring",
                        "recommended_actions": [],
                        "prediction": {"kind": "stays_offline", "within_minutes": 60},
                    },
                )
            )
        )

        payload = notifier.sent[0]["payload"]
        assert payload["prediction"] == {"kind": "stays_offline", "within_minutes": 60}
        assert payload["prediction_text"] == "stay offline within 60 min"


# ---------------------------------------------------------------------------
# run_host_health_investigation
# ---------------------------------------------------------------------------


class TestRunHostHealthInvestigation:
    def _make_db(self, tmp_path):
        return _make_hitl_db(tmp_path)

    def _patch_supervisor(self, monkeypatch):
        import utils.agent.supervisor as _sup_mod

        monkeypatch.setattr(_sup_mod, "increment_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "decrement_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "publish_activity_done", lambda *a, **kw: None)
        monkeypatch.setattr(
            _sup_mod, "make_activity_timeline_callback", lambda *a, **kw: None
        )

    def test_finish_called_no_backoff_written(self, tmp_path, monkeypatch):
        """When finish_host_health is called, outcome is success → no backoff row."""
        import asyncio
        import sqlite3

        import utils.ha.ha_environment as _env_mod
        from utils.disk.host_health import HostHealthFinding
        from utils.disk.host_health_investigation import run_host_health_investigation
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        self._patch_supervisor(monkeypatch)
        monkeypatch.setattr(_env_mod, "load_environment_profile", lambda db: None)

        db = self._make_db(tmp_path)
        finding = HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_host_health",
                                "arguments": {
                                    "summary": "Disk wear at 85%.",
                                    "root_cause": "eMMC aging.",
                                    "outcome": "monitoring",
                                    "recommended_actions": [],
                                },
                            }
                        }
                    ]
                }
            ]
        )

        asyncio.run(
            run_host_health_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=llm,
                ssh_client=FakeSSHClient(),
            )
        )

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT next_allowed_at FROM hitl_suppression"
                " WHERE card_key = 'host_health_backoff:disk_wear'"
            ).fetchone()
        assert row is None

    def test_no_terminal_tool_writes_backoff(self, tmp_path, monkeypatch):
        """When AgentLoop returns a non-success outcome, backoff row is written."""
        import asyncio
        import sqlite3

        import utils.agent.agent_loop as _al_mod
        import utils.ha.ha_environment as _env_mod
        from utils.agent.tool_registry import AgentLoopResult
        from utils.disk.host_health import HostHealthFinding
        from utils.disk.host_health_investigation import run_host_health_investigation
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        self._patch_supervisor(monkeypatch)
        monkeypatch.setattr(_env_mod, "load_environment_profile", lambda db: None)

        _original_loop = _al_mod.AgentLoop

        class _ExhaustedLoop(_original_loop):
            async def run(self, *args, **kwargs):
                return AgentLoopResult(outcome="exhausted")

        monkeypatch.setattr(_al_mod, "AgentLoop", _ExhaustedLoop)

        db = self._make_db(tmp_path)
        finding = HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")

        asyncio.run(
            run_host_health_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=FakeToolCallingLLMClient([]),
                ssh_client=FakeSSHClient(),
            )
        )

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT next_allowed_at FROM hitl_suppression"
                " WHERE card_key = 'host_health_backoff:disk_wear'"
            ).fetchone()
        assert row is not None

    def test_gate_built_with_autonomy_level(self, tmp_path, monkeypatch):
        """AutonomyGate is constructed with config.AUTONOMY_LEVEL."""
        import asyncio

        import config as cfg_mod
        import utils.agent.agent_loop as _al_mod
        import utils.agent.autonomy as _autonomy_mod
        import utils.ha.ha_environment as _env_mod
        from utils.agent.tool_registry import AgentLoopResult
        from utils.disk.host_health import HostHealthFinding
        from utils.disk.host_health_investigation import run_host_health_investigation
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        self._patch_supervisor(monkeypatch)
        monkeypatch.setattr(_env_mod, "load_environment_profile", lambda db: None)
        monkeypatch.setattr(cfg_mod, "AUTONOMY_LEVEL", 2)

        _original_loop = _al_mod.AgentLoop

        class _NoopLoop(_original_loop):
            async def run(self, *args, **kwargs):
                return AgentLoopResult(outcome="success")

        monkeypatch.setattr(_al_mod, "AgentLoop", _NoopLoop)

        captured_levels: list = []
        _original_gate = _autonomy_mod.AutonomyGate

        class _CapturingGate(_original_gate):
            def __init__(self, level, **kwargs):
                captured_levels.append(level)
                super().__init__(level, **kwargs)

        monkeypatch.setattr(_autonomy_mod, "AutonomyGate", _CapturingGate)

        db = self._make_db(tmp_path)
        finding = HostHealthFinding(key="disk_wear", kind="disk_wear", severity="WARN")

        asyncio.run(
            run_host_health_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=FakeToolCallingLLMClient([]),
                ssh_client=FakeSSHClient(),
            )
        )

        assert 2 in captured_levels


# ---------------------------------------------------------------------------
# _parse_rpi_firmware_json
# ---------------------------------------------------------------------------


class TestParseRpiFirmwareJson:
    def test_valid_blocked(self):
        from utils.disk.host_health import _parse_rpi_firmware_json

        raw = _rpi_firmware_json()
        result = _parse_rpi_firmware_json(raw)
        assert result is not None
        assert result["current"] == "2021-12-02"
        assert result["latest"] == "2021-12-02"
        assert result["update_blocked"] is True
        assert result["blocked_reason"] == "eeprom_update_unavailable"

    def test_invalid_json_returns_none(self):
        from utils.disk.host_health import _parse_rpi_firmware_json

        assert _parse_rpi_firmware_json("not json") is None

    def test_empty_returns_none(self):
        from utils.disk.host_health import _parse_rpi_firmware_json

        assert _parse_rpi_firmware_json("") is None

    def test_no_data_wrapper(self):
        from utils.disk.host_health import _parse_rpi_firmware_json

        raw = json.dumps(
            {
                "current": "2023-01-01",
                "latest": "2024-06-01",
                "update_available": True,
                "update_blocked": False,
                "blocked_reason": "",
            }
        )
        result = _parse_rpi_firmware_json(raw)
        assert result is not None
        assert result["current"] == "2023-01-01"
        assert result["update_blocked"] is False


# ---------------------------------------------------------------------------
# fetch_host_health — RPi firmware section
# ---------------------------------------------------------------------------


class TestFetchHostHealthFirmware:
    def test_rpi_firmware_parsed_when_present(self):
        from utils.disk.host_health import fetch_host_health

        ssh = _batch_ssh(
            host_raw=_host_info_json(),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(),
            fw_raw=_rpi_firmware_json(
                current="2021-12-02",
                latest="2021-12-02",
                update_blocked=True,
                blocked_reason="eeprom_update_unavailable",
            ),
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.rpi_firmware is not None
        assert hh.rpi_firmware["update_blocked"] is True
        assert hh.rpi_firmware["blocked_reason"] == "eeprom_update_unavailable"

    def test_rpi_firmware_none_when_absent(self):
        from utils.disk.host_health import fetch_host_health

        ssh = _batch_ssh(
            host_raw=_host_info_json(),
            res_raw=_resolution_json(),
            mem_raw=_meminfo(),
        )
        hh = asyncio.run(fetch_host_health(ssh))
        assert hh.rpi_firmware is None


# ---------------------------------------------------------------------------
# evaluate_host_health — informational / firmware enrichment
# ---------------------------------------------------------------------------


class TestEvaluateHostHealthFirmwareEnrichment:
    def _eval(self, health, **kwargs):
        from utils.disk.host_health import evaluate_host_health

        defaults = dict(
            status=_make_status(),
            last_boot_ts=None,
            pueo_reboot_recent=False,
            known_repair_uuids=set(),
            disk_life_warn_pct=70.0,
            mem_warn_mb=256.0,
        )
        defaults.update(kwargs)
        return evaluate_host_health(health=health, **defaults)

    def test_rpi_firmware_finding_evidence_enriched(self):
        # informational verdict belongs to the seed; code just enriches evidence
        from utils.disk.host_health import ResolutionInfo

        uuid = "abc123def456abc123def456abc12345"
        h = _make_health(
            resolution=ResolutionInfo(
                issues=[{"uuid": uuid, "type": "rpi_firmware_update_blocked"}]
            )
        )
        h.rpi_firmware = {
            "current": "2021-12-02",
            "latest": "2021-12-02",
            "update_available": False,
            "update_blocked": True,
            "blocked_reason": "eeprom_update_unavailable",
        }
        findings = self._eval(h)
        rf = [f for f in findings if f.kind == "resolution_issue"]
        assert len(rf) == 1
        assert rf[0].informational is False
        assert rf[0].evidence["blocked_reason"] == "eeprom_update_unavailable"
        assert rf[0].evidence["current"] == "2021-12-02"

    def test_other_resolution_issue_not_informational(self):
        from utils.disk.host_health import ResolutionInfo

        uuid = "abc123def456abc123def456abc12345"
        h = _make_health(
            resolution=ResolutionInfo(
                issues=[{"uuid": uuid, "type": "reboot_required"}]
            )
        )
        findings = self._eval(h)
        rf = [f for f in findings if f.kind == "resolution_issue"]
        assert len(rf) == 1
        assert rf[0].informational is False

    def test_rpi_firmware_no_fw_data_not_informational(self):
        from utils.disk.host_health import ResolutionInfo

        uuid = "abc123def456abc123def456abc12345"
        h = _make_health(
            resolution=ResolutionInfo(
                issues=[{"uuid": uuid, "type": "rpi_firmware_update_blocked"}]
            )
        )
        # No rpi_firmware data fetched
        findings = self._eval(h)
        rf = [f for f in findings if f.kind == "resolution_issue"]
        assert len(rf) == 1
        assert rf[0].informational is False


# ---------------------------------------------------------------------------
# compute_finding_fingerprint
# ---------------------------------------------------------------------------


class TestComputeFindingFingerprint:
    def test_informational_returns_string(self):
        from utils.disk.host_health import (
            HostHealthFinding,
            compute_finding_fingerprint,
        )

        f = HostHealthFinding(
            key="resolution_issue:abc",
            kind="resolution_issue",
            severity="WARN",
            evidence={"type": "rpi_firmware_update_blocked", "current": "2021-12-02"},
            informational=True,
        )
        fp = compute_finding_fingerprint(f)
        assert fp is not None
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_non_informational_returns_none(self):
        from utils.disk.host_health import (
            HostHealthFinding,
            compute_finding_fingerprint,
        )

        f = HostHealthFinding(
            key="memory_pressure",
            kind="memory_pressure",
            severity="WARN",
            evidence={"psi_some_avg300": 12.5},
            informational=False,
        )
        assert compute_finding_fingerprint(f) is None

    def test_fingerprint_stable_for_same_evidence(self):
        from utils.disk.host_health import (
            HostHealthFinding,
            compute_finding_fingerprint,
        )

        evidence = {"type": "rpi_firmware_update_blocked", "current": "2021-12-02"}
        f1 = HostHealthFinding(
            key="x",
            kind="resolution_issue",
            severity="WARN",
            evidence=evidence,
            informational=True,
        )
        f2 = HostHealthFinding(
            key="x",
            kind="resolution_issue",
            severity="WARN",
            evidence=evidence,
            informational=True,
        )
        assert compute_finding_fingerprint(f1) == compute_finding_fingerprint(f2)

    def test_fingerprint_changes_when_evidence_changes(self):
        from utils.disk.host_health import (
            HostHealthFinding,
            compute_finding_fingerprint,
        )

        f1 = HostHealthFinding(
            key="x",
            kind="resolution_issue",
            severity="WARN",
            evidence={"current": "2021-12-02"},
            informational=True,
        )
        f2 = HostHealthFinding(
            key="x",
            kind="resolution_issue",
            severity="WARN",
            evidence={"current": "2024-06-01"},
            informational=True,
        )
        assert compute_finding_fingerprint(f1) != compute_finding_fingerprint(f2)


# ---------------------------------------------------------------------------
# get_known_issue_note
# ---------------------------------------------------------------------------


class TestGetKnownIssueNote:
    def _make_db(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE hitl_suppression"
                " (card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '',"
                " description TEXT NOT NULL DEFAULT '',"
                " first_sent_at REAL NOT NULL DEFAULT 0,"
                " last_sent_at REAL NOT NULL DEFAULT 0,"
                " send_count INTEGER NOT NULL DEFAULT 1,"
                " last_action TEXT, last_action_at REAL,"
                " rejection_count INTEGER NOT NULL DEFAULT 0,"
                " next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0,"
                " known_issue_note TEXT, resolved_at REAL)"
            )
        return db

    def test_returns_none_when_no_row(self, tmp_path):
        import sqlite3

        from utils.hitl.hitl_tracker import get_known_issue_note

        db = self._make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            assert get_known_issue_note(conn, "host_health:x") is None

    def test_returns_note_for_active_known_issue(self, tmp_path):
        import sqlite3
        import time

        from utils.hitl.hitl_tracker import get_known_issue_note, mark_card_acknowledged

        db = self._make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            mark_card_acknowledged(conn, "host_health:x", note="deadbeef12345678")
        with sqlite3.connect(db) as conn:
            result = get_known_issue_note(conn, "host_health:x")
        assert result == "deadbeef12345678"

    def test_returns_none_after_resolved(self, tmp_path):
        import sqlite3

        from utils.hitl.hitl_tracker import (
            get_known_issue_note,
            mark_card_acknowledged,
            mark_card_resolved,
        )

        db = self._make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            mark_card_acknowledged(conn, "host_health:x", note="deadbeef12345678")
            mark_card_resolved(conn, "host_health:x")
        with sqlite3.connect(db) as conn:
            assert get_known_issue_note(conn, "host_health:x") is None


# ---------------------------------------------------------------------------
# Fingerprint-based re-investigation in _check_host_health
# ---------------------------------------------------------------------------


class TestCheckHostHealthFingerprintReopen:
    def _make_db(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS timeline_events"
                " (id INTEGER PRIMARY KEY, timestamp REAL, event_type TEXT,"
                " event_category TEXT, message TEXT, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ha_repair_history"
                " (issue_key TEXT PRIMARY KEY, first_seen_at REAL,"
                " hitl_sent_at REAL, resolved_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hitl_suppression"
                " (card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '',"
                " description TEXT NOT NULL DEFAULT '',"
                " first_sent_at REAL NOT NULL DEFAULT 0,"
                " last_sent_at REAL NOT NULL DEFAULT 0,"
                " send_count INTEGER NOT NULL DEFAULT 1,"
                " last_action TEXT, last_action_at REAL,"
                " rejection_count INTEGER NOT NULL DEFAULT 0,"
                " next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0,"
                " known_issue_note TEXT, resolved_at REAL)"
            )
        return db

    def test_same_fingerprint_stays_suppressed(self, tmp_path, monkeypatch):
        import sqlite3

        import config as _cfg
        from utils.disk.host_health import (
            HostHealthFinding,
            compute_finding_fingerprint,
        )
        from utils.disk.resource import ResourcePoller
        from utils.hitl.hitl_tracker import mark_card_acknowledged, should_send_card
        from utils.hitl.notify import FakeNotifier

        db = self._make_db(tmp_path)
        monkeypatch.setattr(_cfg, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(_cfg, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(_cfg, "HOST_DISK_LIFE_WARN_PCT", 70.0)

        finding = HostHealthFinding(
            key="resolution_issue:abc123",
            kind="resolution_issue",
            severity="WARN",
            evidence={"type": "rpi_firmware_update_blocked", "current": "2021-12-02"},
            informational=True,
        )
        fp = compute_finding_fingerprint(finding)
        card_key = f"host_health:{finding.key}"

        # Acknowledge the card with the current fingerprint
        with sqlite3.connect(db) as conn:
            mark_card_acknowledged(conn, card_key, note=fp)

        # Still suppressed when fingerprint matches
        with sqlite3.connect(db) as conn:
            assert not should_send_card(conn, card_key)

    def test_rpi_firmware_finding_not_informational_skips_fingerprint_check(
        self, tmp_path, monkeypatch
    ):
        # evaluate_host_health no longer pre-marks rpi_firmware findings as informational
        # (the seed/LLM owns that verdict), so the pre-investigation fingerprint check
        # in resource.py does not fire for this finding type.
        import asyncio
        import sqlite3

        import config as _cfg
        from utils.disk.host_health import (
            HostHealth,
            HostHealthFinding,
            ResolutionInfo,
            compute_finding_fingerprint,
            update_host_health,
        )
        from utils.disk.resource import ResourcePoller
        from utils.hitl.hitl_tracker import mark_card_acknowledged
        from utils.hitl.notify import FakeNotifier

        db = self._make_db(tmp_path)
        monkeypatch.setattr(_cfg, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(_cfg, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(_cfg, "HOST_DISK_LIFE_WARN_PCT", 70.0)
        monkeypatch.setattr(_cfg, "DB_PATH", db)

        uuid = "abc123def456abc123def456abc12345"
        old_evidence = {"type": "rpi_firmware_update_blocked", "current": "2021-12-02"}
        old_finding = HostHealthFinding(
            key=f"resolution_issue:{uuid}",
            kind="resolution_issue",
            severity="WARN",
            evidence=old_evidence,
            informational=True,
        )
        old_fp = compute_finding_fingerprint(old_finding)
        card_key = f"host_health:{old_finding.key}"

        # Acknowledge with old fingerprint
        with sqlite3.connect(db) as conn:
            mark_card_acknowledged(conn, card_key, note=old_fp)

        # Evidence changes, but evaluate_host_health produces informational=False,
        # so compute_finding_fingerprint returns None and the re-trigger does not fire.
        new_firmware = {
            "current": "2024-06-01",
            "latest": "2024-06-01",
            "update_available": False,
            "update_blocked": True,
            "blocked_reason": "eeprom_update_unavailable",
        }
        health = HostHealth(
            swap_total_mb=1024.0,
            swap_used_mb=100.0,
            disk_life_used_pct=None,
            boot_ts=1_700_000_000.0,
            uptime_seconds=3600.0,
            resolution=ResolutionInfo(
                issues=[{"uuid": uuid, "type": "rpi_firmware_update_blocked"}]
            ),
            fetched_at=1.0,
            rpi_firmware=new_firmware,
        )
        update_host_health(health)

        notifier = FakeNotifier()
        poller = ResourcePoller(
            ssh_client=object(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=3.0,
            mem_warn_mb=256.0,
            db_path=db,
        )
        status = _make_status()
        asyncio.run(poller._check_host_health(status))

        # known_issue stays set — fingerprint check did not fire (finding.informational=False)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT known_issue FROM hitl_suppression WHERE card_key = ?",
                (card_key,),
            ).fetchone()
        assert row is not None
        assert row[0] == 1


# ---------------------------------------------------------------------------
# S3: recurrence cooldown, living card, history block
# ---------------------------------------------------------------------------


class TestRecurrenceHelpers:
    """Unit tests for _read/write_last_investigated and _read/write_recurrences."""

    def _make_db(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT)")
        return db

    def _make_poller(self, tmp_path):
        from utils.disk.resource import ResourcePoller
        from utils.hitl.notify import FakeNotifier

        db = self._make_db(tmp_path)
        return (
            ResourcePoller(
                ssh_client=object(),
                notifier=FakeNotifier(),
                interval_seconds=300,
                disk_warn_gb=5.0,
                disk_critical_gb=3.0,
                mem_warn_mb=256.0,
                db_path=db,
            ),
            db,
        )

    def test_last_investigated_round_trip(self, tmp_path):
        poller, db = self._make_poller(tmp_path)
        ts = 1_700_000_000.0
        poller._write_last_investigated(db, "memory_low:host", ts)
        result = poller._read_last_investigated(db, "memory_low:host")
        assert result == pytest.approx(ts)

    def test_last_investigated_missing_returns_none(self, tmp_path):
        poller, db = self._make_poller(tmp_path)
        result = poller._read_last_investigated(db, "memory_low:host")
        assert result is None

    def test_last_investigated_overwrite(self, tmp_path):
        poller, db = self._make_poller(tmp_path)
        poller._write_last_investigated(db, "memory_low:host", 1_000.0)
        poller._write_last_investigated(db, "memory_low:host", 2_000.0)
        result = poller._read_last_investigated(db, "memory_low:host")
        assert result == pytest.approx(2_000.0)

    def test_recurrences_round_trip(self, tmp_path):
        poller, db = self._make_poller(tmp_path)
        timestamps = [1_700_000_000.0, 1_700_001_000.0]
        poller._write_recurrences(db, "memory_pressure:host", timestamps)
        result = poller._read_recurrences(db, "memory_pressure:host")
        assert result == pytest.approx(timestamps)

    def test_recurrences_missing_returns_empty(self, tmp_path):
        poller, db = self._make_poller(tmp_path)
        result = poller._read_recurrences(db, "memory_pressure:host")
        assert result == []

    def test_recurrences_overwrite(self, tmp_path):
        poller, db = self._make_poller(tmp_path)
        poller._write_recurrences(db, "memory_pressure:host", [1.0, 2.0])
        poller._write_recurrences(db, "memory_pressure:host", [3.0])
        result = poller._read_recurrences(db, "memory_pressure:host")
        assert result == pytest.approx([3.0])


class TestCheckHostHealthRecurrence:
    """Tests for the recurrence cooldown in _check_host_health."""

    def _make_db(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS timeline_events"
                " (id INTEGER PRIMARY KEY, timestamp REAL, event_type TEXT,"
                " event_category TEXT, message TEXT, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ha_repair_history"
                " (issue_key TEXT PRIMARY KEY, first_seen_at REAL,"
                " hitl_sent_at REAL, resolved_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hitl_suppression"
                " (card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '',"
                " description TEXT NOT NULL DEFAULT '',"
                " first_sent_at REAL NOT NULL DEFAULT 0,"
                " last_sent_at REAL NOT NULL DEFAULT 0,"
                " send_count INTEGER NOT NULL DEFAULT 1,"
                " last_action TEXT, last_action_at REAL,"
                " rejection_count INTEGER NOT NULL DEFAULT 0,"
                " next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0,"
                " known_issue_note TEXT, resolved_at REAL)"
            )
        return db

    def _make_poller(self, tmp_path, notifier=None):
        from utils.disk.resource import ResourcePoller
        from utils.hitl.notify import FakeNotifier

        db = self._make_db(tmp_path)
        return (
            ResourcePoller(
                ssh_client=object(),
                notifier=notifier or FakeNotifier(),
                interval_seconds=300,
                disk_warn_gb=5.0,
                disk_critical_gb=3.0,
                mem_warn_mb=256.0,
                db_path=db,
            ),
            db,
        )

    def test_recurrence_suppressed_within_window(self, tmp_path, monkeypatch):
        """Second fire of a recurring kind within the window is suppressed."""
        import sqlite3

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 1)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_REINVESTIGATE_HOURS", 24)

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)
        monkeypatch.setattr(hh_mod, "_last_host_health", health)

        # First fire: should send notification
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 1
        assert "memory_low" in poller._alerted_host

        # Seed last_investigated to "just now" so the window hasn't elapsed
        poller._write_last_investigated(db, "memory_low", time.time())

        # Second fire within window: should be suppressed
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 1  # still 1

        # Recurrence count should be bumped in user_prefs
        recs = poller._read_recurrences(db, "memory_low")
        assert len(recs) >= 1

    def test_recurrence_allowed_after_window(self, tmp_path, monkeypatch):
        """Re-investigation fires after the window elapses."""
        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 1)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_REINVESTIGATE_HOURS", 24)

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)
        monkeypatch.setattr(hh_mod, "_last_host_health", health)

        # First fire
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 1

        # Seed last_investigated to > 24h ago so window has elapsed
        past_ts = time.time() - (25 * 3600)
        poller._write_last_investigated(db, "memory_low", past_ts)

        # Second fire: window elapsed → re-investigate
        asyncio.run(poller._check_host_health(status_warn))
        assert len(notifier.sent) == 2

    def test_recurrence_bypass_on_severity_rise(self, tmp_path, monkeypatch):
        """Suppression is skipped when severity rises from WARN to HIGH."""
        import sqlite3

        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 1)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_REINVESTIGATE_HOURS", 24)

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        # Seed a previous OOM total so delta is 0 (no OOM)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES ('host_health:oom_kill_total', '1')"
            )

        # Use mem_warn=False so only PSI triggers memory_pressure (not memory_low)
        status_psi = _make_status(mem_available_mb=600.0, mem_warn=False)

        health_warn = _make_health(psi_some_avg300=15.0, oom_kill_total=1)
        monkeypatch.setattr(hh_mod, "_last_host_health", health_warn)

        # First fire at WARN severity (PSI only, no OOM delta since stored=1 and total=1)
        asyncio.run(poller._check_host_health(status_psi))
        assert len(notifier.sent) == 1
        assert "memory_pressure" in poller._alerted_host

        # Seed last_investigated to "just now" so normally suppressed
        poller._write_last_investigated(db, "memory_pressure", time.time())
        poller._host_key_severity["memory_pressure"] = "WARN"

        # Now fire with OOM kill delta (oom_kill_total=3 → delta=2 → new OOM)
        health_high = _make_health(psi_some_avg300=15.0, oom_kill_total=3)
        monkeypatch.setattr(hh_mod, "_last_host_health", health_high)

        asyncio.run(poller._check_host_health(status_psi))
        # Should re-fire because new_oom is True (bypass suppression)
        assert len(notifier.sent) == 2

    def test_clear_delayed_for_recurring_kind(self, tmp_path, monkeypatch):
        """Recurring kind card is NOT resolved immediately when finding clears."""
        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 1)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_REINVESTIGATE_HOURS", 24)

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)
        status_ok = _make_status(mem_available_mb=800.0, mem_warn=False)
        monkeypatch.setattr(hh_mod, "_last_host_health", health)

        # Fire first
        asyncio.run(poller._check_host_health(status_warn))
        assert "memory_low" in poller._alerted_host

        # Clear poll — within window, should NOT resolve
        asyncio.run(poller._check_host_health(status_ok))
        assert "memory_low" in poller._alerted_host  # still alerted
        assert "memory_low" in poller._host_clear_since

    def test_clear_resolves_after_full_window(self, tmp_path, monkeypatch):
        """Recurring kind card resolves after the full window has elapsed since clearing."""
        import config as cfg_mod
        import utils.disk.host_health as hh_mod
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_INVESTIGATION_ENABLED", False)
        monkeypatch.setattr(cfg_mod, "HOST_MEM_PSI_WARN_PCT", 10.0)
        monkeypatch.setattr(cfg_mod, "HOST_DISK_LIFE_WARN_PCT", 80.0)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_CONFIRM_POLLS", 1)
        monkeypatch.setattr(cfg_mod, "HOST_HEALTH_REINVESTIGATE_HOURS", 24)

        notifier = FakeNotifier()
        poller, db = self._make_poller(tmp_path, notifier=notifier)

        health = _make_health()
        status_warn = _make_status(mem_available_mb=100.0, mem_warn=True)
        status_ok = _make_status(mem_available_mb=800.0, mem_warn=False)
        monkeypatch.setattr(hh_mod, "_last_host_health", health)

        # Fire first
        asyncio.run(poller._check_host_health(status_warn))
        assert "memory_low" in poller._alerted_host

        # Seed clear_since to > 24h ago so window has elapsed
        poller._host_clear_since["memory_low"] = time.time() - (25 * 3600)
        poller._host_key_kinds["memory_low"] = "memory_low"
        poller._host_key_severity["memory_low"] = "WARN"

        # Clear poll — window elapsed → should resolve
        asyncio.run(poller._check_host_health(status_ok))
        assert "memory_low" not in poller._alerted_host
        assert "memory_low" not in poller._host_clear_since


class TestLoadRecurrenceHistory:
    """Tests for load_recurrence_history."""

    def _make_db(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE session_outcomes ("
                "  episode_id TEXT PRIMARY KEY,"
                "  signature TEXT,"
                "  session_type TEXT,"
                "  subject_key TEXT,"
                "  conclusion_json TEXT,"
                "  distill_payload_json TEXT,"
                "  created_at TEXT,"
                "  verification TEXT,"
                "  verified_at TEXT,"
                "  verified_by TEXT,"
                "  correction TEXT"
                ")"
            )
        return db

    def test_empty_db_returns_zero_times(self, tmp_path):
        from utils.disk.host_health_investigation import load_recurrence_history

        db = self._make_db(tmp_path)
        result = load_recurrence_history(db, "memory_low")
        assert result.get("times_in_7d", 0) == 0

    def test_returns_last_root_cause_and_summary(self, tmp_path):
        import json
        import sqlite3

        from utils.disk.host_health_investigation import load_recurrence_history

        db = self._make_db(tmp_path)
        conclusion = {
            "root_cause": "High memory usage by Docker",
            "summary": "Docker using 80% RAM",
        }
        # Insert a session within 7 days
        recent_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)
        )
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO session_outcomes"
                " (episode_id, session_type, subject_key, conclusion_json, created_at)"
                " VALUES (?, 'host_health', ?, ?, ?)",
                ("ep1", "memory_low", json.dumps(conclusion), recent_iso),
            )

        result = load_recurrence_history(db, "memory_low")
        assert result["last_root_cause"] == "High memory usage by Docker"
        assert result["last_summary"] == "Docker using 80% RAM"
        assert result["times_in_7d"] == 1

    def test_counts_only_within_7_days(self, tmp_path):
        import json
        import sqlite3

        from utils.disk.host_health_investigation import load_recurrence_history

        db = self._make_db(tmp_path)
        recent_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)
        )
        old_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 8 * 86400)
        )
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO session_outcomes"
                " (episode_id, session_type, subject_key, conclusion_json, created_at)"
                " VALUES (?, 'host_health', ?, ?, ?)",
                ("ep1", "memory_low", json.dumps({}), recent_iso),
            )
            conn.execute(
                "INSERT INTO session_outcomes"
                " (episode_id, session_type, subject_key, conclusion_json, created_at)"
                " VALUES (?, 'host_health', ?, ?, ?)",
                ("ep2", "memory_low", json.dumps({}), old_iso),
            )

        result = load_recurrence_history(db, "memory_low")
        assert result["times_in_7d"] == 1  # only the recent one

    def test_different_subject_key_not_counted(self, tmp_path):
        import json
        import sqlite3

        from utils.disk.host_health_investigation import load_recurrence_history

        db = self._make_db(tmp_path)
        recent_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)
        )
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO session_outcomes"
                " (episode_id, session_type, subject_key, conclusion_json, created_at)"
                " VALUES (?, 'host_health', ?, ?, ?)",
                ("ep1", "memory_pressure", json.dumps({}), recent_iso),
            )

        result = load_recurrence_history(db, "memory_low")
        assert result.get("times_in_7d", 0) == 0

    def test_history_block_prepended_to_initial_context(self, tmp_path, monkeypatch):
        """run_host_health_investigation prepends History block when times_in_7d > 0."""
        import json
        import sqlite3

        import utils.agent.agent_loop as _al_mod
        import utils.ha.ha_environment as _env_mod
        import utils.agent.supervisor as _sup_mod
        from utils.agent.tool_registry import AgentLoopResult
        from utils.disk.host_health import HostHealthFinding
        from utils.disk.host_health_investigation import run_host_health_investigation
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr(_sup_mod, "increment_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "decrement_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "publish_activity_done", lambda *a, **kw: None)
        monkeypatch.setattr(
            _sup_mod, "make_activity_timeline_callback", lambda *a, **kw: None
        )
        monkeypatch.setattr(_env_mod, "load_environment_profile", lambda db: None)

        db = _make_hitl_db(tmp_path)
        # Add session_outcomes table
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_outcomes ("
                "  episode_id TEXT PRIMARY KEY, signature TEXT, session_type TEXT,"
                "  subject_key TEXT, conclusion_json TEXT, distill_payload_json TEXT,"
                "  created_at TEXT, verification TEXT, verified_at TEXT,"
                "  verified_by TEXT, correction TEXT)"
            )

        captured_prompts: list = []

        original_run = _al_mod.AgentLoop.run

        async def _capturing_run(self, initial_context=None, **kwargs):
            if initial_context:
                captured_prompts.append(initial_context)
            return AgentLoopResult(outcome="failed", summary="x", tool_calls=[])

        monkeypatch.setattr(_al_mod.AgentLoop, "run", _capturing_run)

        finding = HostHealthFinding(
            key="memory_low", kind="memory_low", severity="WARN"
        )
        recurrence_history = {
            "times_in_7d": 3,
            "last_created_at": "2026-11-10T10:00:00Z",
            "last_root_cause": "Docker memory leak",
            "last_summary": "Recommend restarting Docker",
        }

        asyncio.run(
            run_host_health_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=None,
                ssh_client=FakeSSHClient(),
                recurrence_history=recurrence_history,
            )
        )

        assert len(captured_prompts) >= 1
        ctx = captured_prompts[0]
        assert "History" in ctx
        assert "3" in ctx
        assert "Docker memory leak" in ctx
