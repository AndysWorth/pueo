"""Tests for utils/disk/memory_usage.py."""

import asyncio
import json


def _make_apps_list_json(addons: list[dict]) -> str:
    return json.dumps({"result": "ok", "data": {"addons": addons}})


def _make_stats_json(memory_usage: int, memory_limit: int = 7516192768) -> str:
    return json.dumps(
        {
            "result": "ok",
            "data": {
                "cpu_percent": 1.0,
                "memory_usage": memory_usage,
                "memory_limit": memory_limit,
                "memory_percent": round(memory_usage / memory_limit * 100, 2),
                "network_rx": 0,
                "network_tx": 0,
                "blk_read": 0,
                "blk_write": 0,
            },
        }
    )


_MEMINFO = "MemTotal: 8000000 kB\nMemFree: 100000 kB\nMemAvailable: 4000000 kB\n"
_TOTAL_MB = 8000000 / 1024
_AVAIL_MB = 4000000 / 1024


def _batch_output(sections: list[tuple[str, str]]) -> str:
    """Build fake batched stats output from (header, json_body) pairs."""
    lines = []
    for header, body in sections:
        lines.append(f"@@{header}")
        if body:
            lines.append(body)
    return "\n".join(lines)


class TestMemoryConsumer:
    def test_valid_construction(self):
        from utils.disk.memory_usage import MemoryConsumer

        c = MemoryConsumer(
            kind="sys",
            id="core",
            name="HA Core",
            used_mb=500.0,
            limit_mb=7168.0,
            pct_host=6.3,
        )
        assert c.kind == "sys"
        assert c.used_mb == 500.0
        assert c.pct_host == 6.3

    def test_limit_mb_can_be_none(self):
        from utils.disk.memory_usage import MemoryConsumer

        c = MemoryConsumer(
            kind="app",
            id="myapp",
            name="My App",
            used_mb=50.0,
            limit_mb=None,
            pct_host=0.6,
        )
        assert c.limit_mb is None


class TestMemoryBreakdown:
    def test_valid_construction(self):
        from utils.disk.memory_usage import MemoryBreakdown

        bd = MemoryBreakdown(total_mb=8192.0, available_mb=4096.0)
        assert bd.total_mb == 8192.0
        assert bd.consumers == []
        assert bd.errors == []
        assert bd.stopped_apps == []

    def test_json_round_trip(self):
        import dataclasses

        from utils.disk.memory_usage import MemoryBreakdown

        bd = MemoryBreakdown(total_mb=8192.0, available_mb=4096.0, other_mb=256.0)
        d = dataclasses.asdict(bd)
        assert d["total_mb"] == 8192.0
        assert d["other_mb"] == 256.0


class TestFetchStartedApps:
    def test_separates_started_and_stopped(self):
        from utils.disk.memory_usage import _fetch_started_apps
        from utils.ha.ssh_client import FakeSSHClient

        apps_json = _make_apps_list_json(
            [
                {"slug": "myapp", "name": "My App", "state": "started"},
                {"slug": "stoppedapp", "name": "Stopped App", "state": "stopped"},
            ]
        )
        ssh = FakeSSHClient(command_results={"ha apps list": (0, apps_json, "")})
        started, stopped = asyncio.run(_fetch_started_apps(ssh))
        assert ("myapp", "My App") in started
        assert "stoppedapp" in stopped

    def test_rejects_invalid_slugs(self):
        from utils.disk.memory_usage import _fetch_started_apps
        from utils.ha.ssh_client import FakeSSHClient

        apps_json = _make_apps_list_json(
            [{"slug": "bad slug!", "name": "Bad", "state": "started"}]
        )
        ssh = FakeSSHClient(command_results={"ha apps list": (0, apps_json, "")})
        started, stopped = asyncio.run(_fetch_started_apps(ssh))
        assert started == []

    def test_returns_empty_on_malformed_json(self):
        from utils.disk.memory_usage import _fetch_started_apps
        from utils.ha.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha apps list": (0, "not json", "")})
        started, stopped = asyncio.run(_fetch_started_apps(ssh))
        assert started == []
        assert stopped == []


class TestParseStatsJson:
    def test_extracts_data_field(self):
        from utils.disk.memory_usage import _parse_stats_json

        raw = json.dumps({"result": "ok", "data": {"memory_usage": 1048576}})
        result = _parse_stats_json(raw)
        assert result == {"memory_usage": 1048576}

    def test_returns_none_on_empty(self):
        from utils.disk.memory_usage import _parse_stats_json

        assert _parse_stats_json("") is None
        assert _parse_stats_json("   ") is None

    def test_returns_none_on_invalid_json(self):
        from utils.disk.memory_usage import _parse_stats_json

        assert _parse_stats_json("not json") is None


class TestStatsToConsumer:
    def test_converts_bytes_to_mb(self):
        from utils.disk.memory_usage import _stats_to_consumer

        data = {"memory_usage": 512 * 1024 * 1024, "memory_limit": 8192 * 1024 * 1024}
        c = _stats_to_consumer("sys", "core", "HA Core", data, total_mb=8192.0)
        assert c is not None
        assert c.used_mb == 512.0
        assert c.limit_mb == 8192.0

    def test_returns_none_when_memory_usage_missing(self):
        from utils.disk.memory_usage import _stats_to_consumer

        c = _stats_to_consumer("sys", "core", "HA Core", {}, total_mb=8192.0)
        assert c is None

    def test_limit_mb_none_when_memory_limit_zero(self):
        from utils.disk.memory_usage import _stats_to_consumer

        data = {"memory_usage": 104857600, "memory_limit": 0}
        c = _stats_to_consumer("sys", "core", "HA Core", data, total_mb=8192.0)
        assert c is not None
        assert c.limit_mb is None

    def test_pct_host_computed_from_total(self):
        from utils.disk.memory_usage import _stats_to_consumer

        data = {"memory_usage": 819200000}  # ~781 MB
        c = _stats_to_consumer("sys", "core", "HA Core", data, total_mb=8000.0)
        assert c is not None
        assert c.pct_host == round(c.used_mb / 8000.0 * 100, 1)

    def test_pct_host_zero_when_total_zero(self):
        from utils.disk.memory_usage import _stats_to_consumer

        data = {"memory_usage": 1048576}
        c = _stats_to_consumer("sys", "core", "HA Core", data, total_mb=0.0)
        assert c is not None
        assert c.pct_host == 0.0


class TestParseBatchOutput:
    def test_parses_system_and_app_sections(self):
        from utils.disk.memory_usage import _parse_batch_output

        batch = _batch_output(
            [
                ("sys:core", _make_stats_json(512 * 1024 * 1024)),
                ("app:myapp", _make_stats_json(64 * 1024 * 1024, 512 * 1024 * 1024)),
            ]
        )
        consumers, errors = _parse_batch_output(
            batch,
            started_apps=[("myapp", "My App")],
            total_mb=8192.0,
        )
        assert len(consumers) == 2
        assert errors == []
        sys_consumer = next(c for c in consumers if c.id == "core")
        assert sys_consumer.name == "HA Core"
        app_consumer = next(c for c in consumers if c.id == "myapp")
        assert app_consumer.name == "My App"
        assert app_consumer.kind == "app"

    def test_skips_empty_sections(self):
        from utils.disk.memory_usage import _parse_batch_output

        batch = _batch_output(
            [("sys:dns", ""), ("sys:core", _make_stats_json(50 * 1024 * 1024))]
        )
        consumers, errors = _parse_batch_output(batch, started_apps=[], total_mb=8192.0)
        assert len(consumers) == 1
        assert errors == []

    def test_records_error_for_nonempty_unparseable_section(self):
        from utils.disk.memory_usage import _parse_batch_output

        batch = _batch_output([("sys:core", "not json but not empty")])
        consumers, errors = _parse_batch_output(batch, started_apps=[], total_mb=8192.0)
        assert consumers == []
        assert "core" in errors

    def test_records_error_for_missing_memory_usage_field(self):
        from utils.disk.memory_usage import _parse_batch_output

        raw = json.dumps({"result": "ok", "data": {"cpu_percent": 1.0}})
        batch = _batch_output([("sys:core", raw)])
        consumers, errors = _parse_batch_output(batch, started_apps=[], total_mb=8192.0)
        assert consumers == []
        assert "core" in errors

    def test_system_display_names_used(self):
        from utils.disk.memory_usage import _parse_batch_output

        batch = _batch_output([("sys:supervisor", _make_stats_json(100 * 1024 * 1024))])
        consumers, _ = _parse_batch_output(batch, started_apps=[], total_mb=8192.0)
        assert consumers[0].name == "Supervisor"


class TestFetchMemoryBreakdown:
    def _make_ssh(self, batch_sections: list[tuple[str, str]], started_apps=None):
        from utils.ha.ssh_client import FakeSSHClient

        apps_json = _make_apps_list_json(started_apps or [])
        return FakeSSHClient(
            command_results={
                "cat /proc/meminfo": (0, _MEMINFO, ""),
                "ha apps list": (0, apps_json, ""),
                "@@": (0, _batch_output(batch_sections), ""),
            }
        )

    def test_returns_breakdown_with_consumers(self):
        from utils.disk.memory_usage import fetch_memory_breakdown

        ssh = self._make_ssh(
            [
                ("sys:core", _make_stats_json(500 * 1024 * 1024)),
                ("sys:supervisor", _make_stats_json(100 * 1024 * 1024)),
            ]
        )
        bd = asyncio.run(fetch_memory_breakdown(ssh))
        assert bd.total_mb == round(_TOTAL_MB, 1)
        assert bd.available_mb == round(_AVAIL_MB, 1)
        assert len(bd.consumers) == 2
        assert bd.fetched_at > 0

    def test_other_mb_computed_correctly(self):
        from utils.disk.memory_usage import fetch_memory_breakdown

        used_mb = _TOTAL_MB - _AVAIL_MB  # ~3906 MB
        container_mb = 500.0
        ssh = self._make_ssh(
            [("sys:core", _make_stats_json(int(container_mb * 1024 * 1024)))]
        )
        bd = asyncio.run(fetch_memory_breakdown(ssh))
        expected_other = max(0.0, round(used_mb - container_mb, 1))
        assert abs(bd.other_mb - expected_other) < 1.0

    def test_other_mb_never_negative(self):
        from utils.disk.memory_usage import fetch_memory_breakdown

        # huge container — other_mb clamps to 0
        big = int(10000 * 1024 * 1024)
        ssh = self._make_ssh([("sys:core", _make_stats_json(big))])
        bd = asyncio.run(fetch_memory_breakdown(ssh))
        assert bd.other_mb >= 0.0

    def test_stopped_apps_tracked(self):
        from utils.disk.memory_usage import fetch_memory_breakdown
        from utils.ha.ssh_client import FakeSSHClient

        apps_json = _make_apps_list_json(
            [{"slug": "stoppedapp", "name": "Stopped App", "state": "stopped"}]
        )
        ssh = FakeSSHClient(
            command_results={
                "cat /proc/meminfo": (0, _MEMINFO, ""),
                "ha apps list": (0, apps_json, ""),
                "@@": (0, "", ""),
            }
        )
        bd = asyncio.run(fetch_memory_breakdown(ssh))
        assert "stoppedapp" in bd.stopped_apps

    def test_returns_empty_breakdown_when_no_parts(self):
        from utils.disk.memory_usage import fetch_memory_breakdown
        from utils.ha.ssh_client import FakeSSHClient

        # No system components in output, no apps — parts list would still have system
        # components, so we simulate an all-empty batch output
        ssh = FakeSSHClient(
            command_results={
                "cat /proc/meminfo": (0, _MEMINFO, ""),
                "ha apps list": (0, _make_apps_list_json([]), ""),
                "@@": (0, "", ""),
            }
        )
        bd = asyncio.run(fetch_memory_breakdown(ssh))
        assert bd.total_mb == round(_TOTAL_MB, 1)
        assert bd.consumers == []


class TestMemoryBreakdownCache:
    def test_update_and_get(self, monkeypatch):
        import utils.disk.memory_usage as _mod
        from utils.disk.memory_usage import (
            MemoryBreakdown,
            get_memory_breakdown,
            update_memory_breakdown,
        )

        monkeypatch.setattr(_mod, "_last_memory_breakdown", None)
        assert get_memory_breakdown() is None
        bd = MemoryBreakdown(total_mb=8192.0, available_mb=4096.0)
        update_memory_breakdown(bd)
        assert get_memory_breakdown() is bd
        monkeypatch.setattr(_mod, "_last_memory_breakdown", None)
