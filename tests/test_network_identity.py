"""Tests for utils/ha/network_identity.py — pure mapping logic."""

import asyncio
import json
import sqlite3

import pytest


# ---------------------------------------------------------------------------
# candidates_for_device
# ---------------------------------------------------------------------------


class TestCandidatesForDevice:
    """candidates_for_device returns (kind, value) tuples in reliability order."""

    def test_empty_device_returns_empty(self):
        from utils.ha.network_identity import candidates_for_device

        assert candidates_for_device({}, {}, {}) == []

    def test_mac_connection(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {
            "id": "d1",
            "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            "config_entries": [],
        }
        result = candidates_for_device(dev, {}, {})
        assert result == [("mac", "aa:bb:cc:dd:ee:ff")]

    def test_multiple_macs_all_returned(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {
            "id": "d1",
            "connections": [
                ["mac", "AA:BB:CC:DD:EE:FF"],
                ["mac", "11:22:33:44:55:66"],
            ],
            "config_entries": [],
        }
        result = candidates_for_device(dev, {}, {})
        assert result == [
            ("mac", "aa:bb:cc:dd:ee:ff"),
            ("mac", "11:22:33:44:55:66"),
        ]

    def test_ip_connection(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {"id": "d1", "connections": [["ip", "10.0.0.5"]], "config_entries": []}
        result = candidates_for_device(dev, {}, {})
        assert result == [("ip", "10.0.0.5")]

    def test_port_stripped_from_ip(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {
            "id": "d1",
            "connections": [["ip", "10.0.0.5:8123"]],
            "config_entries": [],
        }
        result = candidates_for_device(dev, {}, {})
        assert result == [("ip", "10.0.0.5")]

    def test_config_entry_host(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {"id": "d1", "connections": [], "config_entries": ["entry-1"]}
        host_by_entry = {"entry-1": "192.168.1.100"}
        result = candidates_for_device(dev, host_by_entry, {})
        assert result == [("config_entry", "192.168.1.100")]

    def test_config_entry_host_port_stripped(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {"id": "d1", "connections": [], "config_entries": ["entry-1"]}
        host_by_entry = {"entry-1": "192.168.1.100:1234"}
        result = candidates_for_device(dev, host_by_entry, {})
        assert result == [("config_entry", "192.168.1.100")]

    def test_manual_link_first(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {
            "id": "d1",
            "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            "config_entries": [],
        }
        manual_links = {"d1": "11:22:33:44:55:66"}
        result = candidates_for_device(dev, {}, manual_links)
        assert result[0] == ("manual", "11:22:33:44:55:66")
        assert result[1] == ("mac", "aa:bb:cc:dd:ee:ff")

    def test_mac_before_ip_before_config_entry(self):
        from utils.ha.network_identity import candidates_for_device

        dev = {
            "id": "d1",
            "connections": [["ip", "10.0.0.5"], ["mac", "AA:BB:CC:DD:EE:FF"]],
            "config_entries": ["e1"],
        }
        host_by_entry = {"e1": "10.0.0.99"}
        result = candidates_for_device(dev, host_by_entry, {})
        kinds = [k for k, _ in result]
        # MAC and IP appear first (from connections, in order), then config_entry
        assert "config_entry" in kinds
        assert kinds.index("ip") < kinds.index("config_entry")

    def test_apple_tv_ip_fallback(self):
        """Apple TV registry MAC differs from NetAlertX; config-entry address matches."""
        from utils.ha.network_identity import candidates_for_device

        dev = {
            "id": "atv-1",
            "connections": [["mac", "AA:BB:CC:DD:EE:01"]],
            "config_entries": ["atv-entry"],
        }
        host_by_entry = {"atv-entry": "10.0.0.42"}
        result = candidates_for_device(dev, host_by_entry, {})
        assert ("mac", "aa:bb:cc:dd:ee:01") in result
        assert ("config_entry", "10.0.0.42") in result


# ---------------------------------------------------------------------------
# match_netalertx
# ---------------------------------------------------------------------------


class TestMatchNetalertx:
    def _nax(self, mac="AA:BB:CC:DD:EE:FF", ip="10.0.0.50", present=1):
        return {
            "devMac": mac,
            "devLastIP": ip,
            "devPresentLastScan": present,
            "devLastSeen": "2026-10-01 10:00:00",
        }

    def test_empty_candidates_returns_unmapped(self):
        from utils.ha.network_identity import match_netalertx

        nax, method = match_netalertx([], [self._nax()])
        assert nax is None
        assert method == "unmapped"

    def test_mac_match(self):
        from utils.ha.network_identity import match_netalertx

        candidates = [("mac", "aa:bb:cc:dd:ee:ff")]
        nax, method = match_netalertx(candidates, [self._nax()])
        assert nax is not None
        assert method == "mac"

    def test_ip_match_when_no_mac(self):
        from utils.ha.network_identity import match_netalertx

        candidates = [("ip", "10.0.0.50")]
        nax, method = match_netalertx(candidates, [self._nax()])
        assert nax is not None
        assert method == "ip"

    def test_config_entry_match(self):
        from utils.ha.network_identity import match_netalertx

        candidates = [("config_entry", "10.0.0.50")]
        nax, method = match_netalertx(candidates, [self._nax()])
        assert nax is not None
        assert method == "config_entry"

    def test_manual_match(self):
        from utils.ha.network_identity import match_netalertx

        candidates = [("manual", "aa:bb:cc:dd:ee:ff")]
        nax, method = match_netalertx(candidates, [self._nax()])
        assert nax is not None
        assert method == "manual"

    def test_no_match_returns_not_in_netalertx(self):
        from utils.ha.network_identity import match_netalertx

        candidates = [("mac", "11:22:33:44:55:66")]
        nax, method = match_netalertx(candidates, [self._nax()])
        assert nax is None
        assert method == "not_in_netalertx"

    def test_mac_preferred_over_ip(self):
        """MAC candidate matched; IP candidate not reached."""
        from utils.ha.network_identity import match_netalertx

        nax_devices = [self._nax(mac="AA:BB:CC:DD:EE:FF", ip="10.0.0.50")]
        candidates = [("mac", "aa:bb:cc:dd:ee:ff"), ("ip", "10.0.0.50")]
        nax, method = match_netalertx(candidates, nax_devices)
        assert method == "mac"

    def test_apple_tv_ip_fallback_match(self):
        """Registry MAC doesn't match but config-entry IP does (Apple TV case)."""
        from utils.ha.network_identity import match_netalertx

        nax_devices = [self._nax(mac="00:11:22:33:44:55", ip="10.0.0.42")]
        # Registry MAC differs; config-entry IP matches
        candidates = [("mac", "aa:bb:cc:dd:ee:01"), ("config_entry", "10.0.0.42")]
        nax, method = match_netalertx(candidates, nax_devices)
        assert nax is not None
        assert method == "config_entry"

    def test_empty_nax_returns_not_in_netalertx(self):
        from utils.ha.network_identity import match_netalertx

        candidates = [("mac", "aa:bb:cc:dd:ee:ff")]
        nax, method = match_netalertx(candidates, [])
        assert nax is None
        assert method == "not_in_netalertx"


# ---------------------------------------------------------------------------
# load_config_entry_hosts
# ---------------------------------------------------------------------------


class TestLoadConfigEntryHosts:
    def _storage_json(self, entries):
        return json.dumps({"version": 1, "data": {"entries": entries}})

    def test_extracts_host(self):
        from utils.ha.network_identity import load_config_entry_hosts

        class _SSH:
            async def read_file(self, path):
                return self._storage_json(
                    [{"entry_id": "e1", "data": {"host": "10.0.0.5"}, "domain": "x"}]
                )

            _storage_json = staticmethod(
                lambda entries: json.dumps({"version": 1, "data": {"entries": entries}})
            )

        result = asyncio.run(load_config_entry_hosts(_SSH()))
        assert result == {"e1": "10.0.0.5"}

    def test_extracts_address(self):
        from utils.ha.network_identity import load_config_entry_hosts

        class _SSH:
            async def read_file(self, path):
                return json.dumps(
                    {
                        "data": {
                            "entries": [
                                {"entry_id": "e2", "data": {"address": "192.168.1.1"}}
                            ]
                        }
                    }
                )

        result = asyncio.run(load_config_entry_hosts(_SSH()))
        assert result == {"e2": "192.168.1.1"}

    def test_extracts_ip_address(self):
        from utils.ha.network_identity import load_config_entry_hosts

        class _SSH:
            async def read_file(self, path):
                return json.dumps(
                    {
                        "data": {
                            "entries": [
                                {
                                    "entry_id": "e3",
                                    "data": {"ip_address": "172.16.0.1"},
                                }
                            ]
                        }
                    }
                )

        result = asyncio.run(load_config_entry_hosts(_SSH()))
        assert result == {"e3": "172.16.0.1"}

    def test_ignores_entries_without_host(self):
        from utils.ha.network_identity import load_config_entry_hosts

        class _SSH:
            async def read_file(self, path):
                return json.dumps(
                    {
                        "data": {
                            "entries": [
                                {"entry_id": "e1", "data": {"token": "abc"}},
                                {"entry_id": "e2", "data": {"host": "10.0.0.5"}},
                            ]
                        }
                    }
                )

        result = asyncio.run(load_config_entry_hosts(_SSH()))
        assert "e1" not in result
        assert result["e2"] == "10.0.0.5"

    def test_returns_empty_on_ssh_error(self):
        from utils.ha.network_identity import load_config_entry_hosts

        class _SSH:
            async def read_file(self, path):
                raise OSError("not found")

        result = asyncio.run(load_config_entry_hosts(_SSH()))
        assert result == {}

    def test_returns_empty_on_parse_error(self):
        from utils.ha.network_identity import load_config_entry_hosts

        class _SSH:
            async def read_file(self, path):
                return "not json {"

        result = asyncio.run(load_config_entry_hosts(_SSH()))
        assert result == {}

    def test_only_reads_config_entries_path(self):
        from utils.ha.network_identity import load_config_entry_hosts

        paths_read: list = []

        class _SSH:
            async def read_file(self, path):
                paths_read.append(path)
                return json.dumps({"data": {"entries": []}})

        asyncio.run(load_config_entry_hosts(_SSH()))
        assert paths_read == ["/config/.storage/core.config_entries"]


# ---------------------------------------------------------------------------
# load_manual_links
# ---------------------------------------------------------------------------


class TestLoadManualLinks:
    def _make_db(self, tmp_path, rows):
        db = str(tmp_path / "state.db")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO user_prefs VALUES (?, ?)",
                rows,
            )
        return db

    def test_reads_link_rows(self, tmp_path):
        from utils.ha.network_identity import load_manual_links

        db = self._make_db(
            tmp_path,
            [("netalertx:link:dev-1", "AA:BB:CC:DD:EE:FF")],
        )
        result = load_manual_links(db)
        assert result == {"dev-1": "aa:bb:cc:dd:ee:ff"}

    def test_lowercases_mac(self, tmp_path):
        from utils.ha.network_identity import load_manual_links

        db = self._make_db(
            tmp_path,
            [("netalertx:link:dev-X", "AA:BB:CC:DD:EE:FF")],
        )
        result = load_manual_links(db)
        assert result["dev-X"] == "aa:bb:cc:dd:ee:ff"

    def test_ignores_non_link_rows(self, tmp_path):
        from utils.ha.network_identity import load_manual_links

        db = self._make_db(
            tmp_path,
            [
                ("netalertx:link:dev-1", "AA:BB:CC:DD:EE:FF"),
                ("other:key", "value"),
            ],
        )
        result = load_manual_links(db)
        assert "other:key" not in result
        assert len(result) == 1

    def test_returns_empty_on_missing_db(self, tmp_path):
        from utils.ha.network_identity import load_manual_links

        result = load_manual_links(str(tmp_path / "nonexistent.db"))
        assert result == {}

    def test_returns_empty_when_no_table(self, tmp_path):
        from utils.ha.network_identity import load_manual_links

        db = str(tmp_path / "empty.db")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE other (k TEXT)")
        result = load_manual_links(db)
        assert result == {}


# ---------------------------------------------------------------------------
# build_presence_context — new mapping paths
# ---------------------------------------------------------------------------


class TestBuildPresenceContextNewPaths:
    """Tests for the IP-match and unknown-mapping paths in build_presence_context."""

    def _devices(self):
        return [{"name": "Apple TV", "device_id": "dev-atv", "offline_since": 0.0}]

    def test_ip_match_via_config_entry(self):
        """Apple TV: registry MAC differs, but config-entry IP matches NAX."""
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return [
                    {
                        "devMac": "00:11:22:33:44:55",
                        "devLastIP": "10.0.0.42",
                        "devPresentLastScan": 1,
                        "devLastSeen": "2026-10-01 10:00:00",
                    }
                ]

        class _FakeWS:
            async def get_device_registry(self):
                return [
                    {
                        "id": "dev-atv",
                        # Registry MAC is different from NAX's MAC
                        "connections": [["mac", "aa:bb:cc:dd:ee:01"]],
                        "config_entries": ["atv-entry"],
                    }
                ]

        class _FakeSSH:
            async def read_file(self, path):
                return json.dumps(
                    {
                        "data": {
                            "entries": [
                                {
                                    "entry_id": "atv-entry",
                                    "data": {"host": "10.0.0.42"},
                                }
                            ]
                        }
                    }
                )

        result = asyncio.run(
            build_presence_context(
                self._devices(), _FakeNAX(), _FakeWS(), ssh_client=_FakeSSH()
            )
        )
        assert "present on LAN" in result
        assert "Apple TV" in result
        assert "matched by config_entry" in result

    def test_unmapped_reports_unknown_not_absent(self):
        """Matter/Tuya with no MAC, no IP → 'unknown', never 'absent'."""
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return [{"devMac": "FF:EE:DD:CC:BB:AA", "devLastIP": "10.0.0.1"}]

        class _FakeWS:
            async def get_device_registry(self):
                # Device has no connections and no config entries
                return [{"id": "dev-atv", "connections": [], "config_entries": []}]

        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FakeWS())
        )
        assert "unknown" in result
        assert "absent" not in result
