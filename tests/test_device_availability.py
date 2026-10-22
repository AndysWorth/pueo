"""Tests for utils/ha/device_availability.py and related config keys."""

import importlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import yaml

from utils.ha.device_availability import (
    AvailabilityResult,
    OfflineFinding,
    _compute_offline_since,
    evaluate_device_availability,
    load_snapshot,
    save_snapshot,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_NOW = 1_700_100_000.0
_GRACE_S = 600.0  # 10 min


def _entity(entity_id: str, device_id: str, disabled_by: str | None = None) -> dict:
    return {
        "entity_id": entity_id,
        "device_id": device_id,
        "disabled_by": disabled_by,
    }


def _state(entity_id: str, state: str, last_changed: str | None = None) -> dict:
    if last_changed is None:
        # _NOW = 1_700_100_000 ≈ 2023-11-16T02:13:20Z; default is 2 h earlier
        last_changed = "2023-11-16T00:00:00Z"
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": last_changed,
    }


def _device(device_id: str, name: str, config_entry_ids: list[str]) -> dict:
    return {
        "id": device_id,
        "name": name,
        "name_by_user": None,
        "config_entries": config_entry_ids,
    }


def _entry(entry_id: str, domain: str) -> dict:
    return {"entry_id": entry_id, "domain": domain}


def _prior_online(device_id: str) -> dict:
    return {device_id: {"last_online_ts": _NOW - 3600.0, "alerted": False}}


def _prior_alerted(device_id: str) -> dict:
    return {device_id: {"last_online_ts": _NOW - 3600.0, "alerted": True}}


# ---------------------------------------------------------------------------
# TestEvaluator
# ---------------------------------------------------------------------------


class TestEvaluator:
    def _run(
        self,
        states,
        entity_registry,
        device_registry,
        config_entries=None,
        prior=None,
        now=_NOW,
        grace_s=_GRACE_S,
    ) -> AvailabilityResult:
        return evaluate_device_availability(
            states=states,
            entity_registry=entity_registry,
            device_registry=device_registry,
            config_entries=config_entries or [],
            prior=prior or {},
            now=now,
            grace_s=grace_s,
        )

    def test_all_unavailable_triggers_finding(self):
        result = self._run(
            states=[
                _state("sensor.a", "unavailable"),
                _state("sensor.b", "unavailable"),
            ],
            entity_registry=[_entity("sensor.a", "dev1"), _entity("sensor.b", "dev1")],
            device_registry=[_device("dev1", "Heat Pump", ["entry1"])],
            config_entries=[_entry("entry1", "econet")],
            prior=_prior_online("dev1"),
        )
        assert len(result.new_offline) == 1
        assert result.new_offline[0].config_entry_id == "entry1"
        assert result.new_offline[0].domain == "econet"
        assert result.new_offline[0].devices[0]["name"] == "Heat Pump"
        assert len(result.recovered) == 0

    def test_one_available_entity_keeps_device_online(self):
        result = self._run(
            states=[_state("sensor.a", "unavailable"), _state("sensor.b", "on")],
            entity_registry=[_entity("sensor.a", "dev1"), _entity("sensor.b", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=_prior_online("dev1"),
        )
        assert result.new_offline == []

    def test_disabled_entities_are_ignored(self):
        """A device with one unavailable and one disabled entity is offline."""
        result = self._run(
            states=[_state("sensor.a", "unavailable"), _state("sensor.b", "on")],
            entity_registry=[
                _entity("sensor.a", "dev1"),
                _entity("sensor.b", "dev1", disabled_by="user"),
            ],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=_prior_online("dev1"),
        )
        assert len(result.new_offline) == 1

    def test_unarmed_device_never_alerts(self):
        """No last_online_ts in prior → device is unarmed; no finding."""
        result = self._run(
            states=[_state("sensor.a", "unavailable")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "Unset Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={},  # never seen online
        )
        assert result.new_offline == []

    def test_grace_period_not_yet_expired_no_alert(self):
        """Offline for 9 min 59 s with 10-min grace → no alert yet."""
        # last_changed just under grace ago
        offline_since_ts = _NOW - (600 - 1)  # 9 min 59 s ago
        lc_iso = "2023-11-16T02:03:19Z"  # arbitrary; _compute_offline_since uses this
        result = self._run(
            states=[_state("sensor.a", "unavailable", last_changed=lc_iso)],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={"dev1": {"last_online_ts": offline_since_ts - 1, "alerted": False}},
            now=_NOW,
            grace_s=600.0,
        )
        # The offline_since is determined by last_changed timestamp parse.
        # We don't control the exact timestamp here, so just test the boundary
        # logic using a custom now/grace combination.
        # Specifically: use grace=600 and manually set last_changed so the
        # duration is exactly 599 s:
        import datetime

        ts = _NOW - 599
        lc_near = datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        result2 = self._run(
            states=[_state("sensor.a", "unavailable", last_changed=lc_near)],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={"dev1": {"last_online_ts": _NOW - 700, "alerted": False}},
            now=_NOW,
            grace_s=600.0,
        )
        assert result2.new_offline == []

    def test_grace_period_expired_triggers_alert(self):
        """Offline for exactly grace_s → alert fires."""
        import datetime

        ts = _NOW - 600
        lc_iso = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        result = self._run(
            states=[_state("sensor.a", "unavailable", last_changed=lc_iso)],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={"dev1": {"last_online_ts": _NOW - 700, "alerted": False}},
            now=_NOW,
            grace_s=600.0,
        )
        assert len(result.new_offline) == 1

    def test_grouping_by_config_entry(self):
        """Two offline devices on the same entry produce one OfflineFinding."""
        result = self._run(
            states=[
                _state("sensor.a", "unavailable"),
                _state("sensor.b", "unavailable"),
            ],
            entity_registry=[_entity("sensor.a", "dev1"), _entity("sensor.b", "dev2")],
            device_registry=[
                _device("dev1", "Device 1", ["entry1"]),
                _device("dev2", "Device 2", ["entry1"]),
            ],
            config_entries=[_entry("entry1", "econet")],
            prior={
                "dev1": {"last_online_ts": _NOW - 3600, "alerted": False},
                "dev2": {"last_online_ts": _NOW - 3600, "alerted": False},
            },
        )
        assert len(result.new_offline) == 1
        assert len(result.new_offline[0].devices) == 2

    def test_different_entries_produce_separate_findings(self):
        """Two offline devices on different entries each get their own finding."""
        result = self._run(
            states=[
                _state("sensor.a", "unavailable"),
                _state("sensor.b", "unavailable"),
            ],
            entity_registry=[_entity("sensor.a", "dev1"), _entity("sensor.b", "dev2")],
            device_registry=[
                _device("dev1", "Device 1", ["entry1"]),
                _device("dev2", "Device 2", ["entry2"]),
            ],
            config_entries=[_entry("entry1", "eco"), _entry("entry2", "kasa")],
            prior={
                "dev1": {"last_online_ts": _NOW - 3600, "alerted": False},
                "dev2": {"last_online_ts": _NOW - 3600, "alerted": False},
            },
        )
        assert len(result.new_offline) == 2

    def test_recovery_detected(self):
        """Previously-alerted device coming back online appears in recovered."""
        result = self._run(
            states=[_state("sensor.a", "on")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "Heat Pump", ["entry1"])],
            config_entries=[_entry("entry1", "econet")],
            prior=_prior_alerted("dev1"),
        )
        assert result.recovered != []
        assert result.recovered[0].devices[0]["name"] == "Heat Pump"
        assert result.new_offline == []

    def test_entities_without_device_are_ignored(self):
        """Entity with no device_id doesn't cause any finding."""
        result = self._run(
            states=[_state("sensor.orphan", "unavailable")],
            entity_registry=[
                {"entity_id": "sensor.orphan", "device_id": None, "disabled_by": None}
            ],
            device_registry=[],
            prior={},
        )
        assert result.new_offline == []

    def test_already_alerted_device_not_doubled(self):
        """A device already alerted does not generate a new finding on the next poll."""
        result = self._run(
            states=[_state("sensor.a", "unavailable")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=_prior_alerted("dev1"),
        )
        assert result.new_offline == []

    def test_snapshot_contains_seen_devices(self):
        """The returned snapshot includes all evaluated devices."""
        result = self._run(
            states=[_state("sensor.a", "on")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={},
        )
        assert "dev1" in result.snapshot
        assert result.snapshot["dev1"]["last_online_ts"] == pytest.approx(_NOW)
        assert result.snapshot["dev1"]["alerted"] is False

    def test_snapshot_round_trip(self, tmp_path):
        """save_snapshot then load_snapshot returns the same data."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()

        snap = {"dev1": {"last_online_ts": 12345.0, "alerted": True}}
        save_snapshot(conn, snap)
        loaded = load_snapshot(conn)
        assert loaded == snap

    def test_load_snapshot_empty_when_no_row(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        assert load_snapshot(conn) == {}

    def test_save_snapshot_upserts(self, tmp_path):
        """Calling save_snapshot twice with different data uses upsert."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()

        save_snapshot(conn, {"dev1": {"last_online_ts": 1.0, "alerted": False}})
        save_snapshot(conn, {"dev1": {"last_online_ts": 2.0, "alerted": True}})
        loaded = load_snapshot(conn)
        assert loaded["dev1"]["last_online_ts"] == 2.0
        assert loaded["dev1"]["alerted"] is True

    def test_device_with_no_entities_skipped(self):
        """A device_registry entry with no matching entity_registry rows is ignored."""
        result = self._run(
            states=[],
            entity_registry=[],
            device_registry=[_device("dev1", "Ghost Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={},
        )
        assert "dev1" not in result.snapshot

    def test_entity_missing_state_row_treated_as_unknown_not_offline(self):
        """If a device has an entity with no state row, we don't call it offline."""
        result = self._run(
            states=[],  # no states at all
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=_prior_online("dev1"),
        )
        assert result.new_offline == []


# ---------------------------------------------------------------------------
# TestComputeOfflineSince
# ---------------------------------------------------------------------------


class TestComputeOfflineSince:
    def test_returns_latest_last_changed(self):
        import datetime

        t1 = 1_700_000_000.0
        t2 = 1_700_001_000.0
        lc1 = datetime.datetime.fromtimestamp(t1, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        lc2 = datetime.datetime.fromtimestamp(t2, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entities = [_entity("sensor.a", "dev"), _entity("sensor.b", "dev")]
        state_by_id = {
            "sensor.a": {"state": "unavailable", "last_changed": lc1},
            "sensor.b": {"state": "unavailable", "last_changed": lc2},
        }
        result = _compute_offline_since(entities, state_by_id)
        assert result == pytest.approx(t2)

    def test_ignores_non_unavailable_entities(self):
        import datetime

        t = 1_700_000_000.0
        lc = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entities = [_entity("sensor.a", "dev"), _entity("sensor.b", "dev")]
        state_by_id = {
            "sensor.a": {"state": "on", "last_changed": lc},
            "sensor.b": {"state": "unavailable", "last_changed": lc},
        }
        result = _compute_offline_since(entities, state_by_id)
        assert result == pytest.approx(t)

    def test_returns_none_when_no_unavailable(self):
        entities = [_entity("sensor.a", "dev")]
        state_by_id = {
            "sensor.a": {"state": "on", "last_changed": "2023-01-01T00:00:00Z"}
        }
        result = _compute_offline_since(entities, state_by_id)
        assert result is None

    def test_handles_missing_last_changed(self):
        entities = [_entity("sensor.a", "dev")]
        state_by_id = {"sensor.a": {"state": "unavailable"}}
        result = _compute_offline_since(entities, state_by_id)
        assert result is None


# ---------------------------------------------------------------------------
# TestConfigKeys
# ---------------------------------------------------------------------------


class TestConfigKeys:
    def test_device_availability_enabled_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEVICE_AVAILABILITY_ENABLED is True

    def test_device_availability_grace_minutes_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEVICE_AVAILABILITY_GRACE_MINUTES == 10

    def test_device_availability_enabled_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"device_availability_enabled": False}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEVICE_AVAILABILITY_ENABLED is False

    def test_device_availability_grace_minutes_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"device_availability_grace_minutes": 5}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEVICE_AVAILABILITY_GRACE_MINUTES == 5
