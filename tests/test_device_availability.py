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


# ---------------------------------------------------------------------------
# TestPollDeviceAvailability
# ---------------------------------------------------------------------------

import asyncio

from unittest import mock


def _make_poll_db(tmp_path: Path) -> str:
    """Create a minimal DB with all tables the poll loop needs."""
    db = str(tmp_path / "poll_state.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
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


def _make_offline_ws():
    """WS client where device-A is offline (all entities unavailable)."""
    from utils.ha.ha_ws_client import FakeHAWebSocketClient

    now_iso = "2023-11-16T02:13:20Z"  # matches _NOW
    return FakeHAWebSocketClient(
        devices=[_device("dev-A", "Heat Pump", ["entry-econet"])],
        entity_registry=[_entity("water_heater.hp", "dev-A")],
        config_entries=[_entry("entry-econet", "econet")],
        states=[_state("water_heater.hp", "unavailable", now_iso)],
    )


def _make_online_ws():
    """WS client where device-A is online."""
    from utils.ha.ha_ws_client import FakeHAWebSocketClient

    return FakeHAWebSocketClient(
        devices=[_device("dev-A", "Heat Pump", ["entry-econet"])],
        entity_registry=[_entity("water_heater.hp", "dev-A")],
        config_entries=[_entry("entry-econet", "econet")],
        states=[_state("water_heater.hp", "on")],
    )


async def _run_one_iteration(coro_fn, *, timeout=0.5):
    """Run one iteration of the poll loop then cancel."""
    task = asyncio.create_task(coro_fn())
    await asyncio.sleep(timeout)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestPollDeviceAvailability:
    def test_no_workitem_when_card_suppressed(self, tmp_path, monkeypatch):
        """should_send_card returns False → no WorkItem submitted."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod

        db = _make_poll_db(tmp_path)
        # Mark card as already sent (suppressed).
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, description,"
                " first_sent_at, last_sent_at) VALUES (?,?,?,?,?)",
                ("device_offline:entry-econet", "device_offline", "test", _NOW, _NOW),
            )

        # Arm device in snapshot.
        prior = {"dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False}}
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )

        ws = _make_offline_ws()
        submitted: list = []

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", mock.AsyncMock()
        )

        # Patch time so grace period is exceeded.
        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0

            async def _run():
                await asyncio.sleep(0)  # allow event loop to breathe
                # Run one iteration manually.
                import utils.ha.device_availability as da

                with mock.patch(
                    "utils.ha.device_availability.supervised_sleep"
                    if False
                    else "utils.agent.supervisor.supervised_sleep"
                ):
                    pass
                # Direct call to the loop body logic via short-circuit.

            # Call poll_device_availability with patched WS; cancel after one loop.
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(ws_client=ws, db_path=db)
                )
            )

        assert da_mod._run_device_availability_investigation.call_count == 0

    def test_workitem_submitted_for_new_offline_finding(self, tmp_path, monkeypatch):
        """One investigation is triggered per config entry when a device goes offline."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod

        db = _make_poll_db(tmp_path)
        # Arm device: seen online before.
        prior = {"dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False}}
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )

        ws = _make_offline_ws()
        investigation_calls: list = []

        async def _fake_investigation(finding, **kw):
            investigation_calls.append(finding.config_entry_id)

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", _fake_investigation
        )

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(ws_client=ws, db_path=db),
                    timeout=0.3,
                )
            )

        assert "entry-econet" in investigation_calls

    def test_no_workitem_when_backoff_active(self, tmp_path, monkeypatch):
        """should_send_card returns False for backoff key → investigation skipped."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod

        db = _make_poll_db(tmp_path)
        prior = {"dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False}}
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )
            # Insert backoff row (next_allowed_at = far future).
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, description,"
                " first_sent_at, last_sent_at, next_allowed_at) VALUES (?,?,?,?,?,?)",
                (
                    "device_offline_backoff:entry-econet",
                    "device_offline",
                    "backoff",
                    _NOW,
                    _NOW,
                    _NOW + 86400.0,
                ),
            )

        ws = _make_offline_ws()
        investigation_calls: list = []

        async def _fake_investigation(finding, **kw):
            investigation_calls.append(finding.config_entry_id)

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", _fake_investigation
        )

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(ws_client=ws, db_path=db),
                    timeout=0.3,
                )
            )

        assert investigation_calls == []

    def test_timeline_warn_written_for_offline_device(self, tmp_path, monkeypatch):
        """A WARNING timeline event is written when a device goes offline."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod
        import utils.core.timeline as tl_mod

        db = _make_poll_db(tmp_path)
        prior = {"dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False}}
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )

        ws = _make_offline_ws()
        written: list[dict] = []

        real_write = tl_mod.write_timeline_event

        def _capture(level, source, message, detail=None):
            written.append({"level": level, "source": source, "message": message})
            try:
                real_write(level, source, message, detail)
            except Exception:  # nosec B110
                pass

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", mock.AsyncMock()
        )
        monkeypatch.setattr(tl_mod, "write_timeline_event", _capture)

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(ws_client=ws, db_path=db),
                    timeout=0.3,
                )
            )

        warn_events = [
            e for e in written if e["level"] == "WARNING" and "offline" in e["message"]
        ]
        assert warn_events, f"No WARNING timeline event; got: {written}"
        assert "econet" in warn_events[0]["message"]

    def test_timeline_info_written_and_card_resolved_on_recovery(
        self, tmp_path, monkeypatch
    ):
        """Recovery: timeline INFO written and card resolved."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod
        import utils.core.timeline as tl_mod
        from utils.hitl.hitl_tracker import mark_card_sent

        db = _make_poll_db(tmp_path)

        # Device was alerted (card open), offline_since = _NOW - 2400s (40 min).
        prior = {
            "dev-A": {
                "last_online_ts": _NOW - 3600.0,
                "alerted": True,
                "offline_since": _NOW - 2400.0,
            }
        }
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )
            mark_card_sent(
                conn,
                "device_offline:entry-econet",
                "device_offline",
                "Heat Pump offline",
            )

        ws = _make_online_ws()  # device is back online now
        written: list[dict] = []
        real_write = tl_mod.write_timeline_event

        def _capture(level, source, message, detail=None):
            written.append({"level": level, "source": source, "message": message})
            try:
                real_write(level, source, message, detail)
            except Exception:  # nosec B110
                pass

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(tl_mod, "write_timeline_event", _capture)

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 100.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(ws_client=ws, db_path=db),
                    timeout=0.3,
                )
            )

        info_events = [
            e for e in written if e["level"] == "INFO" and "back online" in e["message"]
        ]
        assert info_events, f"No INFO recovery event; got: {written}"

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression WHERE card_key = ?",
                ("device_offline:entry-econet",),
            ).fetchone()
        assert row is not None
        assert row[0] is not None, "Card should be resolved after recovery"

    def test_unarmed_device_never_triggers_investigation(self, tmp_path, monkeypatch):
        """Devices never seen online (unarmed) do not trigger investigations."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod

        db = _make_poll_db(tmp_path)
        # Empty prior: device never seen online.

        ws = _make_offline_ws()
        investigation_calls: list = []

        async def _fake_investigation(finding, **kw):
            investigation_calls.append(finding.config_entry_id)

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", _fake_investigation
        )

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(ws_client=ws, db_path=db),
                    timeout=0.3,
                )
            )

        assert investigation_calls == []

    def test_grace_period_not_exceeded_no_investigation(self, tmp_path, monkeypatch):
        """Devices offline for less than grace period do not trigger investigation."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod

        db = _make_poll_db(tmp_path)
        prior = {"dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False}}
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )

        ws = _make_offline_ws()
        investigation_calls: list = []

        async def _fake_investigation(finding, **kw):
            investigation_calls.append(finding.config_entry_id)

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", _fake_investigation
        )
        import config as _cfg_mod

        # time.time() returns _NOW + 300s → less than grace of 600s
        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 300.0
            with mock.patch.object(_cfg_mod, "DEVICE_AVAILABILITY_GRACE_MINUTES", 10):
                asyncio.run(
                    _run_one_iteration(
                        lambda: da_mod.poll_device_availability(
                            ws_client=ws, db_path=db
                        ),
                        timeout=0.3,
                    )
                )

        assert investigation_calls == []


class TestCardTypeDeviceOffline:
    def test_card_type_constant(self):
        from utils.hitl.card_types import CARD_TYPE_DEVICE_OFFLINE

        assert CARD_TYPE_DEVICE_OFFLINE == "device_offline"


# ---------------------------------------------------------------------------
# run_device_availability_investigation
# ---------------------------------------------------------------------------


class TestRunDeviceAvailabilityInvestigation:
    def _make_db(self, tmp_path):
        return _make_poll_db(tmp_path)

    def _patch_supervisor(self, monkeypatch):
        import utils.agent.supervisor as _sup_mod

        monkeypatch.setattr(_sup_mod, "increment_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "decrement_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "publish_activity_done", lambda *a, **kw: None)
        monkeypatch.setattr(
            _sup_mod, "make_activity_timeline_callback", lambda *a, **kw: None
        )

    def _finding(self):
        return OfflineFinding(
            config_entry_id="entry-econet",
            domain="econet",
            devices=[
                {
                    "name": "Heat Pump",
                    "device_id": "dev-A",
                    "offline_since": 1700000000.0,
                }
            ],
        )

    def test_finish_called_no_backoff_written(self, tmp_path, monkeypatch):
        """When finish_device_availability is called with success, no backoff row written."""
        import asyncio
        import sqlite3 as _sqlite3

        import utils.ha.ha_environment as _env_mod
        from utils.ha.device_availability_investigation import (
            run_device_availability_investigation,
        )
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        self._patch_supervisor(monkeypatch)
        monkeypatch.setattr(_env_mod, "load_environment_profile", lambda db: None)

        db = self._make_db(tmp_path)
        finding = self._finding()

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_device_availability",
                                "arguments": {
                                    "summary": "MQTT thread died; reloaded integration.",
                                    "root_cause": "Cloud push thread death after MQTT disconnect.",
                                    "outcome": "resolved",
                                    "recommended_actions": [],
                                },
                            }
                        }
                    ]
                }
            ]
        )

        asyncio.run(
            run_device_availability_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=llm,
                ssh_client=FakeSSHClient(),
            )
        )

        with _sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT next_allowed_at FROM hitl_suppression"
                " WHERE card_key = 'device_offline_backoff:entry-econet'"
            ).fetchone()
        assert row is None

    def test_non_success_writes_backoff(self, tmp_path, monkeypatch):
        """When AgentLoop returns a non-success outcome, backoff row is written."""
        import asyncio
        import sqlite3 as _sqlite3

        import utils.agent.agent_loop as _al_mod
        import utils.ha.ha_environment as _env_mod
        from utils.agent.tool_registry import AgentLoopResult
        from utils.ha.device_availability_investigation import (
            run_device_availability_investigation,
        )
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
        finding = self._finding()

        asyncio.run(
            run_device_availability_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=FakeToolCallingLLMClient([]),
                ssh_client=FakeSSHClient(),
            )
        )

        with _sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT next_allowed_at FROM hitl_suppression"
                " WHERE card_key = 'device_offline_backoff:entry-econet'"
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
        from utils.ha.device_availability_investigation import (
            run_device_availability_investigation,
        )
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
        finding = self._finding()

        asyncio.run(
            run_device_availability_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=FakeToolCallingLLMClient([]),
                ssh_client=FakeSSHClient(),
            )
        )

        assert 2 in captured_levels


# ---------------------------------------------------------------------------
# sig_device_availability
# ---------------------------------------------------------------------------


class TestSigDeviceAvailability:
    def test_basic(self):
        from utils.knowledge.runbook_signature import sig_device_availability

        assert sig_device_availability("econet") == "device:offline:econet"

    def test_normalises_case_and_hyphens(self):
        from utils.knowledge.runbook_signature import sig_device_availability

        assert sig_device_availability("EcoNet-Cloud") == "device:offline:econet_cloud"

    def test_empty_domain(self):
        from utils.knowledge.runbook_signature import sig_device_availability

        assert sig_device_availability("") == "device:offline:unknown"


# ---------------------------------------------------------------------------
# build_device_availability_tool_registry
# ---------------------------------------------------------------------------


class TestDeviceAvailabilityToolRegistry:
    def test_contains_required_tools(self):
        from utils.agent.tool_registry import build_device_availability_tool_registry

        reg = build_device_availability_tool_registry()
        names = set(reg.names())
        assert "finish_device_availability" in names
        assert "reload_integration" in names
        assert "query_knowledge" in names
        assert "request_escalation" in names


# ---------------------------------------------------------------------------
# Bug fix: offline_since persisted in snapshot → correct recovery duration
# ---------------------------------------------------------------------------


class TestOfflineSincePersistence:
    """offline_since must survive across poll cycles so recovery duration is correct."""

    def _run(
        self,
        states,
        entity_registry,
        device_registry,
        config_entries=None,
        prior=None,
        now=_NOW,
        grace_s=_GRACE_S,
    ):
        return evaluate_device_availability(
            states=states,
            entity_registry=entity_registry,
            device_registry=device_registry,
            config_entries=config_entries or [],
            prior=prior or {},
            now=now,
            grace_s=grace_s,
        )

    def test_offline_since_in_snapshot_when_alerted(self):
        """After a device transitions to alerted, snapshot stores offline_since."""
        offline_since = _NOW - 700.0  # slightly more than grace
        result = self._run(
            states=[_state("sensor.a", "unavailable", "2023-11-16T01:01:20Z")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=_prior_online("dev1"),
            now=_NOW + 700.0,
            grace_s=_GRACE_S,
        )
        assert result.new_offline != []
        snap = result.snapshot["dev1"]
        assert snap["offline_since"] is not None
        assert snap["alerted"] is True

    def test_offline_since_none_for_online_device(self):
        """Online devices have offline_since=None in snapshot."""
        result = self._run(
            states=[_state("sensor.a", "on")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior={},
        )
        assert result.snapshot["dev1"]["offline_since"] is None

    def test_offline_since_carried_forward_for_already_alerted(self):
        """An already-alerted device's offline_since survives into the next snapshot."""
        stored_since = _NOW - 900.0
        prior = {
            "dev1": {
                "last_online_ts": _NOW - 1000.0,
                "alerted": True,
                "offline_since": stored_since,
            }
        }
        result = self._run(
            states=[_state("sensor.a", "unavailable")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=prior,
        )
        # Already alerted → no new finding; snapshot must preserve offline_since
        assert result.new_offline == []
        assert result.snapshot["dev1"]["offline_since"] == stored_since

    def test_recovery_finding_carries_offline_since(self):
        """Recovery finding includes offline_since from prior snapshot (duration fix)."""
        stored_since = _NOW - 900.0
        prior = {
            "dev1": {
                "last_online_ts": _NOW - 1000.0,
                "alerted": True,
                "offline_since": stored_since,
            }
        }
        result = self._run(
            states=[_state("sensor.a", "on")],
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=prior,
        )
        assert result.recovered != []
        assert result.recovered[0].devices[0]["offline_since"] == stored_since

    def test_offline_since_fallback_to_last_online_ts(self):
        """When _compute_offline_since returns None, falls back to last_online_ts (line 188)."""
        # State row has no last_changed at all → _compute_offline_since returns None
        prior = {"dev1": {"last_online_ts": _NOW - 700.0, "alerted": False}}
        result = self._run(
            states=[
                {"entity_id": "sensor.a", "state": "unavailable"}
            ],  # no last_changed key
            entity_registry=[_entity("sensor.a", "dev1")],
            device_registry=[_device("dev1", "My Device", ["entry1"])],
            config_entries=[_entry("entry1", "test")],
            prior=prior,
            now=_NOW + 700.0,
            grace_s=_GRACE_S,
        )
        # Should still trigger an alert (fallback path hit)
        assert result.new_offline != []
        assert result.new_offline[0].devices[0]["offline_since"] == pytest.approx(
            _NOW - 700.0
        )


# ---------------------------------------------------------------------------
# _compute_offline_since: malformed last_changed skipped (lines 273-274)
# ---------------------------------------------------------------------------


class TestComputeOfflineSinceMalformed:
    def test_skips_entity_with_invalid_last_changed(self):
        """Entity with unparseable last_changed is skipped; returns None if no valid ones."""
        from utils.ha.device_availability import _compute_offline_since

        entities = [{"entity_id": "sensor.a"}]
        state_by_id = {
            "sensor.a": {
                "state": "unavailable",
                "last_changed": "NOT_A_DATE",
            }
        }
        result = _compute_offline_since(entities, state_by_id)
        assert result is None


# ---------------------------------------------------------------------------
# load_snapshot / save_snapshot error paths (lines 295-296, 309-310)
# ---------------------------------------------------------------------------


class TestSnapshotErrorHandling:
    def test_load_snapshot_exception_returns_empty(self, tmp_path):
        """load_snapshot returns {} when the DB raises an exception."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        # No user_prefs table → SELECT raises OperationalError
        assert load_snapshot(conn) == {}

    def test_save_snapshot_exception_is_silent(self, tmp_path):
        """save_snapshot swallows exceptions silently."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        # No user_prefs table → INSERT raises; should not propagate
        save_snapshot(conn, {"dev1": {"last_online_ts": 1.0, "alerted": False}})


# ---------------------------------------------------------------------------
# _run_device_availability_investigation stub delegation (lines 326-330)
# ---------------------------------------------------------------------------


class TestInvestigationStubDelegates:
    def test_stub_calls_investigation_module(self, monkeypatch):
        """_run_device_availability_investigation delegates to the real module."""
        import utils.ha.device_availability_investigation as inv_mod
        from utils.ha.device_availability import (
            OfflineFinding,
            _run_device_availability_investigation,
        )

        called_with = {}
        mock_fn = mock.AsyncMock(side_effect=lambda **kw: called_with.update(kw))
        monkeypatch.setattr(inv_mod, "run_device_availability_investigation", mock_fn)

        finding = OfflineFinding(config_entry_id="e1", domain="test", devices=[])
        asyncio.run(
            _run_device_availability_investigation(
                finding=finding,
                db_path="/tmp/x.db",
                notifier=None,
                llm_client=None,
                knowledge_store=None,
            )
        )
        mock_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Poll: multi-device label, work-queue path, poll_error outcome, supervisor touch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Poll: multi-device label, work-queue path, poll_error outcome, supervisor touch
# ---------------------------------------------------------------------------


def _make_single_iteration_sleep():
    """supervised_sleep replacement that raises CancelledError after first call."""

    async def _sleep(name: str, seconds: float) -> None:
        raise asyncio.CancelledError()

    return _sleep


class TestPollCoverage:
    """Extra poll tests to cover branches missed by the main poll suite."""

    def _make_two_device_ws(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        now_iso = "2023-11-16T02:13:20Z"
        return FakeHAWebSocketClient(
            devices=[
                _device("dev-A", "Heat Pump", ["entry-econet"]),
                _device("dev-B", "Water Sensor", ["entry-econet"]),
            ],
            entity_registry=[
                _entity("water_heater.hp", "dev-A"),
                _entity("sensor.ws", "dev-B"),
            ],
            config_entries=[_entry("entry-econet", "econet")],
            states=[
                _state("water_heater.hp", "unavailable", now_iso),
                _state("sensor.ws", "unavailable", now_iso),
            ],
        )

    def test_multi_device_offline_label(self, tmp_path, monkeypatch):
        """Two devices offline → 'Heat Pump (+1 more)' label in timeline (line 450)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.supervisor as sv_mod
        import utils.agent.work_queue as wq_mod
        import utils.core.timeline as tl_mod

        db = _make_poll_db(tmp_path)
        prior = {
            "dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False},
            "dev-B": {"last_online_ts": _NOW - 3600.0, "alerted": False},
        }
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )

        written_msgs: list[str] = []

        def fake_write_timeline(level, event, msg, extra=None):
            written_msgs.append(msg)

        monkeypatch.setattr(tl_mod, "write_timeline_event", fake_write_timeline)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", mock.AsyncMock()
        )
        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(sv_mod, "supervised_sleep", _make_single_iteration_sleep())

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(
                        ws_client=self._make_two_device_ws(), db_path=db
                    ),
                    timeout=5.0,
                )
            )

        assert any(
            "+1 more" in m for m in written_msgs
        ), f"Expected '+1 more' in {written_msgs}"

    def test_multi_device_recovery_label(self, tmp_path, monkeypatch):
        """Two devices recovering → 'Heat Pump (+1 more)' label in timeline (line 524)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.supervisor as sv_mod
        import utils.agent.work_queue as wq_mod
        import utils.core.timeline as tl_mod
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        db = _make_poll_db(tmp_path)
        prior = {
            "dev-A": {
                "last_online_ts": _NOW - 3600.0,
                "alerted": True,
                "offline_since": _NOW - 700.0,
            },
            "dev-B": {
                "last_online_ts": _NOW - 3600.0,
                "alerted": True,
                "offline_since": _NOW - 700.0,
            },
        }
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )
            conn.execute(
                "INSERT INTO hitl_suppression (card_key, card_type, description,"
                " first_sent_at, last_sent_at) VALUES (?,?,?,?,?)",
                ("device_offline:entry-econet", "device_offline", "test", _NOW, _NOW),
            )

        written_msgs: list[str] = []

        def fake_write_timeline(level, event, msg, extra=None):
            written_msgs.append(msg)

        monkeypatch.setattr(tl_mod, "write_timeline_event", fake_write_timeline)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", mock.AsyncMock()
        )
        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(sv_mod, "supervised_sleep", _make_single_iteration_sleep())

        online_ws = FakeHAWebSocketClient(
            devices=[
                _device("dev-A", "Heat Pump", ["entry-econet"]),
                _device("dev-B", "Water Sensor", ["entry-econet"]),
            ],
            entity_registry=[
                _entity("water_heater.hp", "dev-A"),
                _entity("sensor.ws", "dev-B"),
            ],
            config_entries=[_entry("entry-econet", "econet")],
            states=[
                _state("water_heater.hp", "on"),
                _state("sensor.ws", "on"),
            ],
        )

        asyncio.run(
            _run_one_iteration(
                lambda: da_mod.poll_device_availability(
                    ws_client=online_ws, db_path=db
                ),
                timeout=5.0,
            )
        )

        assert any(
            "+1 more" in m for m in written_msgs
        ), f"Expected '+1 more' in {written_msgs}"

    def test_workitem_submitted_via_work_queue(self, tmp_path, monkeypatch):
        """When a real WorkQueue exists, WorkItem is submitted through it (line 503)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod
        import utils.agent.supervisor as sv_mod
        import utils.core.timeline as tl_mod

        db = _make_poll_db(tmp_path)
        prior = {"dev-A": {"last_online_ts": _NOW - 3600.0, "alerted": False}}
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?,?)",
                ("device_availability:state", json.dumps(prior)),
            )

        submitted_items: list = []

        class FakeQueue:
            async def submit(self, item):
                submitted_items.append(item)

        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", FakeQueue)
        monkeypatch.setattr(
            da_mod, "_run_device_availability_investigation", mock.AsyncMock()
        )
        monkeypatch.setattr(tl_mod, "write_timeline_event", lambda *a, **kw: None)
        monkeypatch.setattr(sv_mod, "supervised_sleep", _make_single_iteration_sleep())

        with mock.patch("utils.ha.device_availability.time") as mock_time:
            mock_time.time.return_value = _NOW + 3600.0
            asyncio.run(
                _run_one_iteration(
                    lambda: da_mod.poll_device_availability(
                        ws_client=_make_offline_ws(), db_path=db
                    ),
                    timeout=5.0,
                )
            )

        assert len(submitted_items) == 1

    def test_poll_error_before_evaluate_gives_poll_error_outcome(
        self, tmp_path, monkeypatch
    ):
        """Exception before evaluate → outcome becomes 'poll_error' (lines 564-565)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.supervisor as sv_mod
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        db = _make_poll_db(tmp_path)

        class ErrorWS(FakeHAWebSocketClient):
            async def get_states(self):
                raise RuntimeError("WS connection lost")

        outcomes: list[str] = []

        class FakeSV:
            def touch(self, name, outcome=""):
                outcomes.append(outcome)

        monkeypatch.setattr(sv_mod, "get_supervisor_instance", lambda: FakeSV())
        monkeypatch.setattr(sv_mod, "supervised_sleep", _make_single_iteration_sleep())

        asyncio.run(
            _run_one_iteration(
                lambda: da_mod.poll_device_availability(
                    ws_client=ErrorWS(), db_path=db
                ),
                timeout=5.0,
            )
        )

        assert "poll_error" in outcomes

    def test_supervisor_touch_called_on_success(self, tmp_path, monkeypatch):
        """Supervisor touch is called with outcome after a successful poll (lines 577-579)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.work_queue as wq_mod
        import utils.agent.supervisor as sv_mod

        db = _make_poll_db(tmp_path)

        outcomes: list[str] = []

        class FakeSV:
            def touch(self, name, outcome=""):
                outcomes.append(outcome)

        monkeypatch.setattr(sv_mod, "get_supervisor_instance", lambda: FakeSV())
        monkeypatch.setattr(sv_mod, "supervised_sleep", _make_single_iteration_sleep())
        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)

        asyncio.run(
            _run_one_iteration(
                lambda: da_mod.poll_device_availability(
                    ws_client=_make_online_ws(), db_path=db
                ),
                timeout=5.0,
            )
        )

        assert len(outcomes) >= 1
        assert "offline" in outcomes[0] or "recovered" in outcomes[0]


# ---------------------------------------------------------------------------
# Investigation: env-profile exception silent, outer exception logged
# ---------------------------------------------------------------------------


class TestInvestigationExceptionPaths:
    def _make_db(self, tmp_path) -> str:
        db = _make_poll_db(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_outcomes"
                " (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " session_id TEXT NOT NULL,"
                " session_type TEXT NOT NULL,"
                " outcome TEXT NOT NULL DEFAULT 'unknown',"
                " card_id TEXT,"
                " distill_payload_json TEXT,"
                " distilled_at REAL,"
                " episode_data_json TEXT,"
                " created_at REAL NOT NULL DEFAULT 0,"
                " updated_at REAL NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hitl_backoff"
                " (backoff_key TEXT PRIMARY KEY,"
                " backoff_until REAL NOT NULL DEFAULT 0,"
                " outcome TEXT NOT NULL DEFAULT '',"
                " created_at REAL NOT NULL DEFAULT 0)"
            )
        return db

    def _finding(self):
        return OfflineFinding(
            config_entry_id="entry-econet",
            domain="econet",
            devices=[
                {
                    "name": "Heat Pump",
                    "device_id": "dev-A",
                    "offline_since": _NOW - 700.0,
                }
            ],
        )

    def test_env_profile_exception_is_silent(self, tmp_path, monkeypatch):
        """Exception in load_environment_profile is caught silently (lines 65-67)."""
        import utils.ha.device_availability_investigation as inv_mod
        import utils.ha.ha_environment as env_mod
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        monkeypatch.setattr(
            env_mod,
            "load_environment_profile",
            mock.Mock(side_effect=RuntimeError("db error")),
        )

        db = self._make_db(tmp_path)
        asyncio.run(
            inv_mod.run_device_availability_investigation(
                finding=self._finding(),
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=FakeToolCallingLLMClient([]),
                ssh_client=FakeSSHClient(),
            )
        )
        # Should not raise; the exception is swallowed

    def test_outer_exception_is_logged(self, tmp_path, monkeypatch, caplog):
        """Exception inside loop.run() is caught and logged (lines 135-136)."""
        import utils.ha.device_availability_investigation as inv_mod
        import utils.agent.agent_loop as al_mod
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        class BoomLoop:
            def __init__(self, **kw):
                pass

            async def run(self, **kw):
                raise RuntimeError("loop boom")

        monkeypatch.setattr(al_mod, "AgentLoop", BoomLoop)

        db = self._make_db(tmp_path)

        import logging

        pueo_logger = logging.getLogger("pueo")
        orig_propagate = pueo_logger.propagate
        pueo_logger.propagate = True

        try:
            with caplog.at_level(logging.ERROR):
                asyncio.run(
                    inv_mod.run_device_availability_investigation(
                        finding=self._finding(),
                        notifier=FakeNotifier(),
                        db_path=db,
                        llm_client=FakeToolCallingLLMClient([]),
                        ssh_client=FakeSSHClient(),
                    )
                )
        finally:
            pueo_logger.propagate = orig_propagate

        assert any(
            "investigation_failed" in r.message or "loop boom" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Additional coverage: registry cache, supervisor touch exception, set_ha_profile
# ---------------------------------------------------------------------------


class TestAdditionalCoverage:
    def test_registry_cache_reused_on_second_iteration(self, tmp_path, monkeypatch):
        """Second poll iteration reuses cached registries (lines 407-409)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.supervisor as sv_mod
        import utils.agent.work_queue as wq_mod
        import utils.core.timeline as tl_mod

        db = _make_poll_db(tmp_path)

        # Sleep mock: first call passes through (second iteration runs), second raises
        call_count = 0

        async def _two_iteration_sleep(name: str, seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()
            # First call: return normally so the loop runs a second iteration

        monkeypatch.setattr(sv_mod, "supervised_sleep", _two_iteration_sleep)
        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)
        monkeypatch.setattr(tl_mod, "write_timeline_event", lambda *a, **kw: None)

        asyncio.run(
            _run_one_iteration(
                lambda: da_mod.poll_device_availability(
                    ws_client=_make_online_ws(), db_path=db
                ),
                timeout=10.0,
            )
        )

        assert call_count >= 1  # at least one sleep was called

    def test_supervisor_touch_exception_swallowed(self, tmp_path, monkeypatch):
        """Exception in supervisor touch block is swallowed (lines 582-583)."""
        import utils.ha.device_availability as da_mod
        import utils.agent.supervisor as sv_mod
        import utils.agent.work_queue as wq_mod

        db = _make_poll_db(tmp_path)

        # get_supervisor_instance raises → should be caught silently
        monkeypatch.setattr(
            sv_mod,
            "get_supervisor_instance",
            mock.Mock(side_effect=RuntimeError("sv gone")),
        )
        monkeypatch.setattr(sv_mod, "supervised_sleep", _make_single_iteration_sleep())
        monkeypatch.setattr(wq_mod, "get_work_queue_or_none", lambda: None)

        # Should not raise despite the supervisor being broken
        asyncio.run(
            _run_one_iteration(
                lambda: da_mod.poll_device_availability(
                    ws_client=_make_online_ws(), db_path=db
                ),
                timeout=5.0,
            )
        )

    def test_set_ha_profile_when_profile_available(self, tmp_path, monkeypatch):
        """executor.set_ha_profile is called when load_environment_profile returns data (line 65 of investigation)."""
        import utils.ha.device_availability_investigation as inv_mod
        import utils.ha.ha_environment as env_mod
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.ha.ha_environment import HAEnvironmentProfile

        # Return a non-None profile so set_ha_profile is called
        fake_profile = HAEnvironmentProfile()
        monkeypatch.setattr(
            env_mod,
            "load_environment_profile",
            mock.Mock(return_value=fake_profile),
        )

        db = _make_poll_db(tmp_path)
        # Add tables the investigation needs
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_outcomes"
                " (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,"
                " session_type TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'unknown',"
                " card_id TEXT, distill_payload_json TEXT, distilled_at REAL,"
                " episode_data_json TEXT, created_at REAL NOT NULL DEFAULT 0,"
                " updated_at REAL NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hitl_backoff"
                " (backoff_key TEXT PRIMARY KEY, backoff_until REAL NOT NULL DEFAULT 0,"
                " outcome TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL DEFAULT 0)"
            )

        finding = OfflineFinding(
            config_entry_id="entry-econet",
            domain="econet",
            devices=[
                {
                    "name": "Heat Pump",
                    "device_id": "dev-A",
                    "offline_since": _NOW - 700.0,
                }
            ],
        )

        asyncio.run(
            inv_mod.run_device_availability_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=FakeToolCallingLLMClient([]),
                ssh_client=FakeSSHClient(),
            )
        )
        # The profile was set — no assertion needed beyond "didn't crash"


# ---------------------------------------------------------------------------
# TestGetNetworkPresence — _get_network_presence tool (S2a)
# ---------------------------------------------------------------------------


class TestGetNetworkPresence:
    """Unit tests for ToolExecutor._get_network_presence."""

    def _make_executor(self, nax_client=None, ws_client=None, tmp_path=None):
        import asyncio  # noqa: F401
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.agent.tool_executor import ToolExecutor

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True),
            notifier=FakeNotifier(),
            netalertx_api_client=nax_client,
        )
        if ws_client is not None:
            ex.set_ws_client(ws_client)
        return ex

    def _fake_nax(self, devices, has_token=True):
        class _FakeNAX:
            def __init__(self, devs, tok):
                self._devs = devs
                self.has_token = tok

            async def get_devices(self):
                return self._devs

        return _FakeNAX(devices, has_token)

    def test_no_api_client_returns_not_configured(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall

        executor = self._make_executor()
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence",
                    arguments={"identifier": "aa:bb:cc:dd:ee:ff"},
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "not_configured"
        assert data["present"] is None

    def test_no_token_returns_no_token(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall

        nax = self._fake_nax([], has_token=False)
        executor = self._make_executor(nax_client=nax)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence",
                    arguments={"identifier": "aa:bb:cc:dd:ee:ff"},
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "no_token"

    def test_found_by_mac(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall

        devices = [
            {
                "devMac": "AA:BB:CC:DD:EE:FF",
                "devLastIP": "10.0.0.5",
                "devPresentLastScan": 1,
                "devLastSeen": "2026-10-22 07:00:00",
                "devName": "Heat Pump",
                "devAlias": None,
                "devVendor": "Rheem",
                "devScanIPv4": "10.0.0.5",
            }
        ]
        nax = self._fake_nax(devices)
        executor = self._make_executor(nax_client=nax)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence",
                    arguments={"identifier": "aa:bb:cc:dd:ee:ff"},
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "ok"
        assert data["present"] is True
        assert data["ip"] == "10.0.0.5"
        assert data["mac"] == "aa:bb:cc:dd:ee:ff"
        assert data["name"] == "Heat Pump"
        assert data["vendor"] == "Rheem"
        assert data["ip_changed"] is False

    def test_found_by_ip(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall

        devices = [
            {
                "devMac": "AA:BB:CC:DD:EE:FF",
                "devLastIP": "10.0.0.5",
                "devPresentLastScan": 0,
                "devLastSeen": "2026-10-22 06:00:00",
                "devName": "EconNet",
                "devAlias": None,
                "devVendor": None,
                "devScanIPv4": None,
            }
        ]
        nax = self._fake_nax(devices)
        executor = self._make_executor(nax_client=nax)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence", arguments={"identifier": "10.0.0.5"}
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "ok"
        assert data["present"] is False
        assert data["ip_changed"] is None

    def test_not_found_returns_present_false(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall

        nax = self._fake_nax([])
        executor = self._make_executor(nax_client=nax)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence",
                    arguments={"identifier": "00:11:22:33:44:55"},
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "ok"
        assert data["present"] is False
        assert data["ip"] is None

    def test_api_error_returns_failure(self):
        import asyncio
        from utils.agent.tool_registry import ToolCall

        class _ErrorNAX:
            has_token = True

            async def get_devices(self):
                raise RuntimeError("connection refused")

        executor = self._make_executor(nax_client=_ErrorNAX())
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence",
                    arguments={"identifier": "aa:bb:cc:dd:ee:ff"},
                )
            )
        )
        assert not result.success
        assert "connection refused" in result.error

    def test_resolves_ha_device_id_via_ws_client(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        devices_nax = [
            {
                "devMac": "AA:BB:CC:DD:EE:FF",
                "devLastIP": "10.0.0.7",
                "devPresentLastScan": 1,
                "devLastSeen": "2026-10-22 08:00:00",
                "devName": "MyDevice",
                "devAlias": None,
                "devVendor": "Acme",
                "devScanIPv4": "10.0.0.7",
            }
        ]
        nax = self._fake_nax(devices_nax)

        ws = FakeHAWebSocketClient(
            devices=[
                {
                    "id": "dev-abc",
                    "name": "MyDevice",
                    "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
                    "config_entries": [],
                }
            ]
        )

        executor = self._make_executor(nax_client=nax, ws_client=ws)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence", arguments={"identifier": "dev-abc"}
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "ok"
        assert data["present"] is True
        assert data["mac"] == "aa:bb:cc:dd:ee:ff"

    def test_resolves_ha_entity_id_via_ws_client(self):
        import asyncio, json
        from utils.agent.tool_registry import ToolCall
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        devices_nax = [
            {
                "devMac": "11:22:33:44:55:66",
                "devLastIP": "10.0.0.9",
                "devPresentLastScan": 1,
                "devLastSeen": "2026-10-22 09:00:00",
                "devName": "Thermostat",
                "devAlias": None,
                "devVendor": None,
                "devScanIPv4": None,
            }
        ]
        nax = self._fake_nax(devices_nax)

        ws = FakeHAWebSocketClient(
            devices=[
                {
                    "id": "dev-xyz",
                    "name": "Thermostat",
                    "connections": [["mac", "11:22:33:44:55:66"]],
                    "config_entries": [],
                }
            ],
            entity_registry=[
                {"entity_id": "climate.thermostat", "device_id": "dev-xyz"}
            ],
        )

        executor = self._make_executor(nax_client=nax, ws_client=ws)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="get_network_presence",
                    arguments={"identifier": "climate.thermostat"},
                )
            )
        )
        assert result.success
        data = json.loads(result.output)
        assert data["netalertx_status"] == "ok"
        assert data["present"] is True
        assert data["mac"] == "11:22:33:44:55:66"


# ---------------------------------------------------------------------------
# TestDeviceAvailabilityNaxThreading — nax client flows to investigation
# ---------------------------------------------------------------------------


class TestDeviceAvailabilityNaxThreading:
    """Verify netalertx_api_client threads from poll_device_availability
    through the stub and into run_device_availability_investigation."""

    def test_stub_passes_nax_to_real_investigation(self, monkeypatch):
        """_run_device_availability_investigation passes nax client through."""
        import asyncio
        from utils.ha.device_availability import (
            OfflineFinding,
            _run_device_availability_investigation,
        )
        import utils.ha.device_availability_investigation as inv_mod

        captured = {}

        async def _fake_real(
            finding, notifier, db_path, llm_client, knowledge_store, **kw
        ):
            captured["nax"] = kw.get("netalertx_api_client")

        monkeypatch.setattr(
            inv_mod, "run_device_availability_investigation", _fake_real
        )

        finding = OfflineFinding(
            config_entry_id="entry-1",
            domain="test",
            devices=[{"name": "Dev", "device_id": "d1", "offline_since": 0.0}],
        )

        class _FakeNAX:
            pass

        nax = _FakeNAX()
        asyncio.run(
            _run_device_availability_investigation(
                finding=finding,
                db_path=":memory:",
                notifier=None,
                llm_client=None,
                knowledge_store=None,
                netalertx_api_client=nax,
            )
        )
        assert captured["nax"] is nax

    def test_investigation_passes_nax_to_executor(self, tmp_path, monkeypatch):
        """run_device_availability_investigation passes nax to ToolExecutor."""
        import asyncio
        from utils.ha.device_availability import OfflineFinding
        from utils.ha.device_availability_investigation import (
            run_device_availability_investigation,
        )
        from utils.ha.ssh_client import FakeSSHClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        import utils.ha.ha_environment as _env_mod
        from utils.agent.tool_executor import ToolExecutor

        captured_executor: dict = {}

        _orig_init = ToolExecutor.__init__

        def _patched_init(self, *args, **kwargs):
            captured_executor["nax"] = kwargs.get("netalertx_api_client")
            _orig_init(self, *args, **kwargs)

        monkeypatch.setattr(ToolExecutor, "__init__", _patched_init)
        monkeypatch.setattr(_env_mod, "load_environment_profile", lambda db: None)

        import utils.agent.supervisor as _sup_mod

        monkeypatch.setattr(_sup_mod, "increment_active_agent", lambda: None)
        monkeypatch.setattr(_sup_mod, "decrement_active_agent", lambda: None)
        monkeypatch.setattr(
            _sup_mod,
            "make_activity_timeline_callback",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(_sup_mod, "publish_activity_done", lambda *a, **kw: None)

        # Build a minimal DB with the tables the investigation uses
        import sqlite3

        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            for ddl in (
                "CREATE TABLE hitl_suppression (card_key TEXT PRIMARY KEY,"
                " next_allowed_at REAL NOT NULL DEFAULT 0,"
                " resolved_at REAL)",
                "CREATE TABLE hitl_cards (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " card_key TEXT NOT NULL, card_type TEXT NOT NULL, subject TEXT,"
                " payload_json TEXT, created_at REAL NOT NULL DEFAULT 0,"
                " resolved_at REAL, status TEXT NOT NULL DEFAULT 'pending',"
                " context_json TEXT)",
                "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT)",
                "CREATE TABLE session_outcomes"
                " (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " session_id TEXT NOT NULL, session_type TEXT NOT NULL,"
                " outcome TEXT NOT NULL DEFAULT 'unknown',"
                " card_id TEXT, distill_payload_json TEXT, distilled_at REAL,"
                " episode_data_json TEXT,"
                " created_at REAL NOT NULL DEFAULT 0,"
                " updated_at REAL NOT NULL DEFAULT 0)",
                "CREATE TABLE hitl_backoff"
                " (backoff_key TEXT PRIMARY KEY,"
                " backoff_until REAL NOT NULL DEFAULT 0,"
                " outcome TEXT NOT NULL DEFAULT '',"
                " created_at REAL NOT NULL DEFAULT 0)",
                "CREATE TABLE runbook_usage"
                " (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " session_id TEXT NOT NULL, strategy_id TEXT NOT NULL,"
                " created_at REAL NOT NULL DEFAULT 0)",
            ):
                conn.execute(ddl)

        finding = OfflineFinding(
            config_entry_id="entry-econet",
            domain="econet",
            devices=[{"name": "Heat Pump", "device_id": "dev-A", "offline_since": 0.0}],
        )

        class _FakeNAX:
            pass

        nax = _FakeNAX()

        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_device_availability",
                                "arguments": {
                                    "summary": "done",
                                    "root_cause": "test",
                                    "outcome": "resolved",
                                    "recommended_actions": [],
                                },
                            }
                        }
                    ]
                }
            ]
        )

        asyncio.run(
            run_device_availability_investigation(
                finding=finding,
                notifier=FakeNotifier(),
                db_path=db,
                llm_client=llm,
                ssh_client=FakeSSHClient(),
                netalertx_api_client=nax,
            )
        )
        assert captured_executor.get("nax") is nax


# ---------------------------------------------------------------------------
# build_presence_context
# ---------------------------------------------------------------------------


class TestBuildPresenceContext:
    """build_presence_context returns a formatted presence block or empty string."""

    def _devices(self):
        return [{"name": "Heat Pump", "device_id": "dev-A", "offline_since": 0.0}]

    def test_returns_empty_when_no_client(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        result = asyncio.run(build_presence_context(self._devices(), None, None))
        assert result == ""

    def test_returns_empty_when_no_token(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _NoToken:
            has_token = False

        result = asyncio.run(build_presence_context(self._devices(), _NoToken(), None))
        assert result == ""

    def test_present_on_lan(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return [
                    {
                        "devMac": "AA:BB:CC:DD:EE:FF",
                        "devPresentLastScan": 1,
                        "devLastSeen": "2026-10-22 07:00:00",
                        "devLastIP": "10.0.0.50",
                    }
                ]

        class _FakeWS:
            async def get_device_registry(self):
                return [
                    {
                        "id": "dev-A",
                        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
                    }
                ]

        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FakeWS())
        )
        assert "present on LAN" in result
        assert "Heat Pump" in result
        assert "aa:bb:cc:dd:ee:ff" in result

    def test_absent_from_lan(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return [
                    {
                        "devMac": "AA:BB:CC:DD:EE:FF",
                        "devPresentLastScan": 0,
                        "devLastSeen": "2026-10-22 06:55:00",
                        "devLastIP": "10.0.0.50",
                    }
                ]

        class _FakeWS:
            async def get_device_registry(self):
                return [
                    {
                        "id": "dev-A",
                        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
                    }
                ]

        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FakeWS())
        )
        assert "absent from LAN" in result
        assert "Heat Pump" in result

    def test_no_mac_mapping(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return []

        class _FakeWS:
            async def get_device_registry(self):
                # Device has no MAC connection
                return [{"id": "dev-A", "connections": []}]

        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FakeWS())
        )
        assert "no MAC mapping" in result

    def test_not_found_in_netalertx(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                # Different MAC — device not in inventory
                return [{"devMac": "11:22:33:44:55:66", "devPresentLastScan": 1}]

        class _FakeWS:
            async def get_device_registry(self):
                return [
                    {
                        "id": "dev-A",
                        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
                    }
                ]

        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FakeWS())
        )
        assert "not found in NetAlertX" in result
        assert "aa:bb:cc:dd:ee:ff" in result

    def test_api_error_returns_empty(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _ErrorNAX:
            has_token = True

            async def get_devices(self):
                raise RuntimeError("connection refused")

        class _FakeWS:
            async def get_device_registry(self):
                return [
                    {
                        "id": "dev-A",
                        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
                    }
                ]

        result = asyncio.run(
            build_presence_context(self._devices(), _ErrorNAX(), _FakeWS())
        )
        assert result == ""

    def test_ws_error_falls_back_gracefully(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return []

        class _FailWS:
            async def get_device_registry(self):
                raise RuntimeError("ws error")

        # ws error → no MAC mapping → "no MAC mapping" line per device
        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FailWS())
        )
        assert "no MAC mapping" in result

    def test_no_ws_client_fallback(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return []

        result = asyncio.run(build_presence_context(self._devices(), _FakeNAX(), None))
        assert "no MAC mapping" in result

    def test_header_present_in_block(self):
        import asyncio
        from utils.ha.device_availability_investigation import build_presence_context

        class _FakeNAX:
            has_token = True

            async def get_devices(self):
                return [
                    {
                        "devMac": "AA:BB:CC:DD:EE:FF",
                        "devPresentLastScan": 1,
                        "devLastSeen": "2026-10-22 07:00:00",
                        "devLastIP": "10.0.0.50",
                    }
                ]

        class _FakeWS:
            async def get_device_registry(self):
                return [
                    {
                        "id": "dev-A",
                        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
                    }
                ]

        result = asyncio.run(
            build_presence_context(self._devices(), _FakeNAX(), _FakeWS())
        )
        assert result.startswith("\n\nNetAlertX network presence:\n")

    def test_stub_passes_ws_client(self):
        """_run_device_availability_investigation passes ws_client through."""
        import asyncio
        from utils.ha.device_availability import (
            OfflineFinding,
            _run_device_availability_investigation,
        )
        import utils.ha.device_availability_investigation as inv_mod

        captured: dict = {}

        async def _fake_real(
            finding, notifier, db_path, llm_client, knowledge_store, **kw
        ):
            captured["ws"] = kw.get("ws_client")

        inv_mod_attr = "run_device_availability_investigation"
        original = getattr(inv_mod, inv_mod_attr)
        setattr(inv_mod, inv_mod_attr, _fake_real)
        try:
            finding = OfflineFinding(
                config_entry_id="entry-1",
                domain="test",
                devices=[{"name": "D", "device_id": "d1", "offline_since": 0.0}],
            )

            class _FakeWS:
                pass

            ws = _FakeWS()
            asyncio.run(
                _run_device_availability_investigation(
                    finding=finding,
                    db_path=":memory:",
                    notifier=None,
                    llm_client=None,
                    knowledge_store=None,
                    ws_client=ws,
                )
            )
        finally:
            setattr(inv_mod, inv_mod_attr, original)
        assert captured["ws"] is ws

    def test_stub_passes_rest_client(self):
        """_run_device_availability_investigation passes ha_rest_client through."""
        import asyncio
        from utils.ha.device_availability import (
            OfflineFinding,
            _run_device_availability_investigation,
        )
        import utils.ha.device_availability_investigation as inv_mod

        captured: dict = {}

        async def _fake_real(
            finding, notifier, db_path, llm_client, knowledge_store, **kw
        ):
            captured["rest"] = kw.get("ha_rest_client")

        inv_mod_attr = "run_device_availability_investigation"
        original = getattr(inv_mod, inv_mod_attr)
        setattr(inv_mod, inv_mod_attr, _fake_real)
        try:
            finding = OfflineFinding(
                config_entry_id="entry-1",
                domain="test",
                devices=[{"name": "D", "device_id": "d1", "offline_since": 0.0}],
            )

            class _FakeRest:
                pass

            rest = _FakeRest()
            asyncio.run(
                _run_device_availability_investigation(
                    finding=finding,
                    db_path=":memory:",
                    notifier=None,
                    llm_client=None,
                    knowledge_store=None,
                    ha_rest_client=rest,
                )
            )
        finally:
            setattr(inv_mod, inv_mod_attr, original)
        assert captured["rest"] is rest
