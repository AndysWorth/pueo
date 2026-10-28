"""Tests for ADR 050: prediction checks, the daily digest and supersede."""

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest

from utils.knowledge.session_verification import (
    ACTIONABLE_CARD_TYPES,
    PendingPrediction,
    PredictionVerdict,
    card_is_actionable,
    collect_digest_items,
    default_verification_question,
    describe_prediction,
    digest_due,
    judge_prediction,
    load_pending_for_subject,
    load_pending_predictions,
    maybe_send_verification_digest,
    normalize_prediction,
    resolve_prediction,
    session_actions,
)

_T0 = 1_700_000_000.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _make_db(tmp_path: Path) -> str:
    db = str(tmp_path / "state.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE session_outcomes (episode_id TEXT PRIMARY KEY,"
            " signature TEXT, session_type TEXT, subject_key TEXT,"
            " conclusion_json TEXT, distill_payload_json TEXT, created_at TEXT,"
            " verification TEXT, verified_at TEXT, verified_by TEXT,"
            " correction TEXT, distilled_at TEXT, card_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE runbook_usage (id INTEGER PRIMARY KEY, strategy_id TEXT,"
            " episode_id TEXT, signature TEXT, outcome TEXT, verification TEXT,"
            " verified_at TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    return db


def _insert_session(
    db: str,
    episode_id: str,
    *,
    subject_key: str = "subj",
    card_id: str = "",
    created_ts: float = _T0,
    prediction: object = None,
    steps: list | None = None,
    verification: str = "pending",
    conclusion_json: str | None = None,
) -> None:
    conclusion: dict = {"root_cause": "r"}
    if prediction is not None:
        conclusion["prediction"] = prediction
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO session_outcomes (episode_id, signature, session_type,"
            " subject_key, conclusion_json, distill_payload_json, created_at,"
            " verification, card_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                episode_id,
                "device:offline:econet",
                "device_availability",
                subject_key,
                conclusion_json or json.dumps(conclusion),
                json.dumps({"steps": steps or []}),
                _iso(created_ts),
                verification,
                card_id,
            ),
        )


def _row(db: str, episode_id: str) -> tuple:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT verification, verified_by, correction, distill_payload_json"
            " FROM session_outcomes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()


def _step(name: str, success: bool) -> dict:
    return {
        "step_number": 1,
        "tool_call": {"name": name, "arguments": {}},
        "tool_result": {"tool_name": name, "success": success, "output": ""},
        "timestamp": 0.0,
    }


# ---------------------------------------------------------------------------
# normalize / describe
# ---------------------------------------------------------------------------


class TestNormalizePrediction:
    def test_valid(self):
        assert normalize_prediction(
            {"kind": "stays_offline", "within_minutes": "30"}
        ) == {"kind": "stays_offline", "within_minutes": 30}

    def test_clamped_to_a_week(self):
        p = normalize_prediction(
            {"kind": "recovers_without_action", "within_minutes": 10**9}
        )
        assert p == {"kind": "recovers_without_action", "within_minutes": 7 * 24 * 60}

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "stays_offline",
            {"kind": "maybe", "within_minutes": 5},
            {"kind": "stays_offline"},
            {"kind": "stays_offline", "within_minutes": 0},
            {"kind": "stays_offline", "within_minutes": "soon"},
            {"kind": "stays_offline", "within_minutes": None},
        ],
    )
    def test_invalid(self, raw):
        assert normalize_prediction(raw) is None

    def test_describe(self):
        assert (
            describe_prediction({"kind": "recovers_after_reload", "within_minutes": 15})
            == "recover after an integration reload within 15 min"
        )


# ---------------------------------------------------------------------------
# judge_prediction
# ---------------------------------------------------------------------------


def _judge(kind, recovered_after=None, now_after=60, actioned=False, reloaded=False):
    pred = {"kind": kind, "within_minutes": 30}
    recovered_at = None if recovered_after is None else _T0 + recovered_after * 60
    return judge_prediction(
        pred, _T0, _T0 + now_after * 60, recovered_at, actioned, reloaded
    )


class TestJudgePrediction:
    def test_still_offline_before_deadline_undecided(self):
        assert _judge("recovers_without_action", now_after=10) is None

    def test_still_offline_after_deadline(self):
        assert _judge("stays_offline") == PredictionVerdict("success")
        v = _judge("recovers_without_action")
        assert v.verification == "failure"
        assert "still not recovered after 60 min" in v.correction

    def test_stays_offline_but_recovered_early(self):
        v = _judge("stays_offline", recovered_after=10)
        assert v.verification == "failure"
        assert "recovered after 10 min" in v.correction

    def test_stays_offline_recovered_after_window(self):
        assert _judge("stays_offline", recovered_after=45) == PredictionVerdict(
            "success"
        )

    def test_recovers_without_action(self):
        assert _judge("recovers_without_action", recovered_after=10) == (
            PredictionVerdict("success")
        )
        late = _judge("recovers_without_action", recovered_after=45)
        assert late.verification == "failure"
        assert "took 45 min" in late.correction

    def test_recovers_without_action_but_actioned_undecidable(self):
        assert (
            _judge("recovers_without_action", recovered_after=10, actioned=True) is None
        )

    def test_recovers_after_reload(self):
        assert _judge(
            "recovers_after_reload", recovered_after=10, reloaded=True
        ) == PredictionVerdict("success")
        late = _judge("recovers_after_reload", recovered_after=45, reloaded=True)
        assert late.verification == "failure"
        assert "after the reload" in late.correction
        no_reload = _judge("recovers_after_reload", recovered_after=10)
        assert no_reload.verification == "failure"
        assert "without a reload" in no_reload.correction


class TestSessionActions:
    def test_actions(self):
        assert session_actions([]) == (False, False)
        assert session_actions(None) == (False, False)  # type: ignore[arg-type]
        assert session_actions(["junk", _step("read_logs", True)]) == (False, False)
        assert session_actions([_step("reload_integration", False)]) == (False, False)
        assert session_actions([_step("call_service", True)]) == (True, False)
        assert session_actions([_step("reload_integration", True)]) == (True, True)


# ---------------------------------------------------------------------------
# Loaders + resolve
# ---------------------------------------------------------------------------


class TestLoadPending:
    def test_loads_only_valid_pending_predictions(self, tmp_path):
        db = _make_db(tmp_path)
        pred = {"kind": "stays_offline", "within_minutes": 30}
        _insert_session(
            db,
            "e1",
            card_id="c1",
            prediction=pred,
            steps=[_step("reload_integration", True)],
        )
        _insert_session(db, "e2", prediction=pred, verification="success")
        _insert_session(db, "e3")  # no prediction
        _insert_session(db, "e4", prediction={"kind": "bogus", "within_minutes": 1})
        _insert_session(db, "e5", conclusion_json='{"prediction": broken')
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO session_outcomes (episode_id, conclusion_json,"
                " created_at, verification) VALUES ('e6', ?, 'yesterday', 'pending')",
                (json.dumps({"prediction": pred}),),
            )

        rows = load_pending_predictions(db)
        assert [r.episode_id for r in rows] == ["e1"]
        assert rows[0].card_id == "c1"
        assert rows[0].created_ts == _T0
        assert rows[0].actioned and rows[0].reloaded

    def test_for_subject_includes_unpredicted(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_session(db, "e1", subject_key="k")
        _insert_session(db, "e2", subject_key="other")
        rows = load_pending_for_subject(db, "k")
        assert [(r.episode_id, r.prediction) for r in rows] == [("e1", None)]

    def test_missing_table_returns_empty(self, tmp_path):
        assert load_pending_predictions(str(tmp_path / "empty.db")) == []


class TestResolvePrediction:
    def _pending(self) -> PendingPrediction:
        return PendingPrediction(
            episode_id="e1",
            subject_key="k",
            card_id="c",
            created_ts=_T0,
            prediction={"kind": "stays_offline", "within_minutes": 5},
            actioned=False,
            reloaded=False,
        )

    def test_marks_system_and_enqueues(self, tmp_path, monkeypatch):
        import agents.ha_agent_advanced as adv
        import utils.knowledge.runbook_distiller as rd

        db = _make_db(tmp_path)
        _insert_session(db, "e1")
        monkeypatch.setattr(adv, "DB_PATH", db)
        enqueue = mock.AsyncMock(return_value=True)
        monkeypatch.setattr(rd, "enqueue_distillation_after_verification", enqueue)

        store = object()
        asyncio.run(
            resolve_prediction(
                db, self._pending(), PredictionVerdict("failure", "wrong"), store
            )
        )
        assert _row(db, "e1")[:3] == ("failure", "system", "wrong")
        assert enqueue.await_args.kwargs["verification"] == "failure"
        assert enqueue.await_args.kwargs["correction"] == "wrong"

    def test_without_store_skips_distillation(self, tmp_path, monkeypatch):
        import agents.ha_agent_advanced as adv
        import utils.knowledge.runbook_distiller as rd

        db = _make_db(tmp_path)
        _insert_session(db, "e1")
        monkeypatch.setattr(adv, "DB_PATH", db)
        enqueue = mock.AsyncMock()
        monkeypatch.setattr(rd, "enqueue_distillation_after_verification", enqueue)

        asyncio.run(
            resolve_prediction(db, self._pending(), PredictionVerdict("success"))
        )
        assert _row(db, "e1")[:2] == ("success", "system")
        enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Supersede (write_session_outcome)
# ---------------------------------------------------------------------------


class TestSupersede:
    def test_resend_supersedes_older_pending_rows(self, tmp_path, monkeypatch):
        import agents.ha_agent_advanced as adv

        db = _make_db(tmp_path)
        monkeypatch.setattr(adv, "DB_PATH", db)
        _insert_session(db, "old", card_id="nid1")
        _insert_session(db, "done", card_id="nid1", verification="failure")
        _insert_session(db, "other", card_id="nid2")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO runbook_usage (strategy_id, episode_id, verification)"
                " VALUES ('s', 'old', 'pending')"
            )

        adv.write_session_outcome("new", "sig", "t", "k", "{}", "{}", "nid1")

        old = _row(db, "old")
        assert old[0] == "superseded" and old[3] is None
        assert _row(db, "done")[0] == "failure"
        assert _row(db, "other")[0] == "pending"
        assert _row(db, "new")[0] == "pending"
        with sqlite3.connect(db) as conn:
            usage = conn.execute(
                "SELECT verification FROM runbook_usage WHERE episode_id = 'old'"
            ).fetchone()
        assert usage == ("superseded",)

    def test_without_runbook_usage_table_still_writes(self, tmp_path, monkeypatch):
        import agents.ha_agent_advanced as adv

        db = _make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE runbook_usage")
        monkeypatch.setattr(adv, "DB_PATH", db)
        _insert_session(db, "old", card_id="nid1")

        adv.write_session_outcome("new", "sig", "t", "k", "{}", "{}", "nid1")
        assert _row(db, "old")[0] == "superseded"
        assert _row(db, "new")[0] == "pending"

    def test_superseded_excluded_from_signature_stats(self, tmp_path):
        from utils.knowledge.kb_meta_analyzer import _load_signature_stats

        db = _make_db(tmp_path)
        _insert_session(db, "a", created_ts=time.time())
        _insert_session(db, "b", created_ts=time.time(), verification="superseded")
        stats, _ = _load_signature_stats(db)
        row = next(s for s in stats if s.signature == "device:offline:econet")
        assert row.sessions_30d == 1
        assert row.pending == 1


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


def _write_card(watch: Path, nid: str, payload: dict, sent_at: int = 1) -> None:
    payload = {"notification_id": nid, **payload}
    (watch / f"{nid}.json").write_text(
        json.dumps(
            {
                "notification_id": nid,
                "subject": f"Subject {nid}",
                "sent_at": sent_at,
                "payload": payload,
            }
        )
    )


class TestDefaultVerificationQuestion:
    def test_host_health_informational(self):
        q = default_verification_question("host_health", {"informational": True})
        assert q == "Is it right that nothing needs doing here?"

    def test_host_health_needs_action(self):
        q = default_verification_question("host_health", {"outcome": "needs_action"})
        assert q == "Was Pueo's diagnosis and recommended action right?"

    def test_host_health_other(self):
        q = default_verification_question("host_health", {"outcome": "monitoring"})
        assert q == "Was Pueo's assessment right?"

    def test_update_with_component(self):
        q = default_verification_question("update", {"component": "core"})
        assert q == "Was the core update assessment right?"

    def test_update_no_component(self):
        q = default_verification_question("update", {})
        assert "update" in q.lower()

    def test_device_offline_with_name(self):
        q = default_verification_question(
            "device_offline", {"devices": [{"name": "Lamp"}]}
        )
        assert "Lamp" in q

    def test_device_offline_no_devices(self):
        q = default_verification_question("device_offline", {})
        assert "device" in q.lower()

    def test_ha_repair(self):
        q = default_verification_question("ha_repair", {})
        assert q == "Was Pueo's analysis of this repair issue right?"

    def test_ha_config_issue(self):
        q = default_verification_question("ha_config_issue", {})
        assert q == "Was Pueo's config issue assessment right?"

    def test_notification_card(self):
        q = default_verification_question("unknown", {"is_notification_card": True})
        assert "notification" in q.lower()

    def test_unknown_type_fallback(self):
        q = default_verification_question("some_other_type", {})
        assert q  # non-empty


class TestCardIsActionable:
    def test_rule(self):
        assert card_is_actionable({"card_type": "update"})
        assert card_is_actionable({"card_type": "host_health"})
        assert not card_is_actionable(
            {"card_type": "host_health", "informational": True}
        )
        assert not card_is_actionable({"card_type": "device_offline"})
        assert card_is_actionable({"card_type": "device_offline", "has_fix": True})
        assert card_is_actionable({"card_type": "x", "pending_fix_yaml": "a: 1"})

    def test_matches_queue_template(self):
        tpl = (
            Path(__file__).resolve().parent.parent / "web/templates/index.html"
        ).read_text()
        m = re.search(r"set is_actionable = ct in \(([^)]*)\)", tpl)
        assert m, "is_actionable tuple not found in index.html"
        template_types = set(re.findall(r'"([a-z_]+)"', m.group(1)))
        assert template_types == set(ACTIONABLE_CARD_TYPES)


class TestDigestDue:
    def _at(self, hour: int) -> float:
        lt = list(time.localtime(_T0))
        lt[3], lt[4], lt[5] = hour, 0, 0
        return time.mktime(time.struct_time(lt))

    def test_due(self):
        now = self._at(10)
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        assert digest_due(now, 9, None) == today
        assert digest_due(now, 9, "2000-01-01") == today

    def test_not_due(self):
        now = self._at(8)
        assert digest_due(now, 9, None) is None
        today = time.strftime("%Y-%m-%d", time.localtime(self._at(10)))
        assert digest_due(self._at(10), 9, today) is None
        assert digest_due(self._at(10), -1, None) is None


class TestCollectDigestItems:
    def test_collects_open_questions_for_pending_sessions(self, tmp_path):
        db = _make_db(tmp_path)
        watch = tmp_path / "hitl"
        watch.mkdir()
        _insert_session(db, "e1")
        _insert_session(db, "e2")
        _insert_session(db, "e3", verification="success")
        _insert_session(db, "e4")
        q = {"verification_question": "Did it recover?"}
        _write_card(
            watch,
            "dev",
            {
                "card_type": "device_offline",
                "episode_id": "e1",
                "devices": [{"name": "Lamp"}],
                **q,
            },
            sent_at=5,
        )
        _write_card(
            watch, "upd", {"card_type": "update", "episode_id": "e2", **q}, sent_at=2
        )
        _write_card(watch, "done", {"card_type": "update", "episode_id": "e3", **q})
        _write_card(watch, "noq", {"card_type": "update", "episode_id": "e2"})
        _write_card(watch, "answered", {"card_type": "update", "episode_id": "e4", **q})
        (watch / "answered.approved").touch()
        (watch / "bad.json").write_text("{not json")

        items = collect_digest_items(watch, db)
        assert [i["nid"] for i in items] == ["upd", "dev"]
        upd, dev = items
        assert upd["acknowledge"] is False and upd["can_turn_off"] is False
        assert dev["acknowledge"] is True and dev["can_turn_off"] is True
        assert dev["question"] == "Did it recover?"
        assert dev["subject"] == "Subject dev"

    def test_informational_host_health_card_with_default_question(self, tmp_path):
        """An informational host_health card with the default question appears in the digest."""
        db = _make_db(tmp_path)
        watch = tmp_path / "hitl"
        watch.mkdir()
        _insert_session(db, "e1")
        _write_card(
            watch,
            "rpi",
            {
                "card_type": "host_health",
                "informational": True,
                "episode_id": "e1",
                "verification_question": "Is it right that nothing needs doing here?",
            },
        )
        items = collect_digest_items(watch, db)
        assert [i["nid"] for i in items] == ["rpi"]
        assert items[0]["acknowledge"] is True
        assert "nothing needs doing" in items[0]["question"]

    def test_missing_dir_and_db(self, tmp_path):
        db = _make_db(tmp_path)
        assert collect_digest_items(tmp_path / "nope", db) == []
        assert collect_digest_items(tmp_path, str(tmp_path / "empty.db")) == []


class TestMaybeSendDigest:
    def _setup(self, tmp_path):
        db = _make_db(tmp_path)
        watch = tmp_path / "hitl"
        watch.mkdir()
        return db, watch

    def test_sends_once_per_day(self, tmp_path):
        from utils.hitl.notify import FakeNotifier

        db, watch = self._setup(tmp_path)
        _insert_session(db, "e1")
        _write_card(
            watch,
            "dev",
            {
                "card_type": "device_offline",
                "episode_id": "e1",
                "verification_question": "Off?",
            },
        )
        notifier = FakeNotifier()
        now = time.time()
        hour = time.localtime(now).tm_hour

        assert asyncio.run(
            maybe_send_verification_digest(notifier, db, str(watch), hour, now)
        )
        assert not asyncio.run(
            maybe_send_verification_digest(notifier, db, str(watch), hour, now)
        )
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["card_type"] == "verification_digest"
        assert [i["nid"] for i in payload["items"]] == ["dev"]
        assert notifier.sent[0]["subject"] == "Pueo: 1 question to confirm"

    def test_empty_day_counts_as_done(self, tmp_path):
        from utils.hitl.notify import FakeNotifier

        db, watch = self._setup(tmp_path)
        notifier = FakeNotifier()
        now = time.time()
        hour = time.localtime(now).tm_hour
        assert not asyncio.run(
            maybe_send_verification_digest(notifier, db, str(watch), hour, now)
        )
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?",
                ("verification_digest:last_date",),
            ).fetchone() == (time.strftime("%Y-%m-%d", time.localtime(now)),)
        assert notifier.sent == []

    def test_disabled_and_missing_prefs(self, tmp_path):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        assert not asyncio.run(
            maybe_send_verification_digest(
                notifier, str(tmp_path / "empty.db"), str(tmp_path), -1
            )
        )
        assert notifier.sent == []


# ---------------------------------------------------------------------------
# Device poll verifier
# ---------------------------------------------------------------------------


class TestCheckDevicePredictions:
    def _setup(self, tmp_path, monkeypatch, kind, created_ts):
        import agents.ha_agent_advanced as adv
        from utils.hitl.hitl_tracker import stable_nid

        db = _make_db(tmp_path)
        monkeypatch.setattr(adv, "DB_PATH", db)
        _insert_session(
            db,
            "e1",
            card_id=stable_nid("device_offline:entry-a"),
            created_ts=created_ts,
            prediction={"kind": kind, "within_minutes": 30},
        )
        _insert_session(
            db,
            "e2",
            card_id=stable_nid("device_offline:entry-z"),
            prediction={"kind": kind, "within_minutes": 30},
        )
        return db

    def test_recovered_matches_prediction(self, tmp_path, monkeypatch):
        from utils.ha.device_availability import check_device_predictions

        now = time.time()
        db = self._setup(tmp_path, monkeypatch, "recovers_without_action", now - 600)
        n = asyncio.run(
            check_device_predictions(db, now, {"device_offline:entry-a": False}, set())
        )
        assert n == 1
        assert _row(db, "e1")[:2] == ("success", "system")
        assert _row(db, "e2")[0] == "pending"

    def test_actioned_recovery_is_undecided(self, tmp_path, monkeypatch):
        from utils.ha.device_availability import check_device_predictions

        now = time.time()
        db = self._setup(tmp_path, monkeypatch, "recovers_without_action", now - 600)
        n = asyncio.run(
            check_device_predictions(db, now, {"device_offline:entry-a": True}, set())
        )
        assert n == 0
        assert _row(db, "e1")[0] == "pending"

    def test_still_offline_past_deadline_fails(self, tmp_path, monkeypatch):
        from utils.ha.device_availability import check_device_predictions

        now = time.time()
        db = self._setup(tmp_path, monkeypatch, "recovers_without_action", now - 3600)
        n = asyncio.run(
            check_device_predictions(db, now, {}, {"device_offline:entry-a"})
        )
        assert n == 1
        verification, by, correction, _ = _row(db, "e1")
        assert (verification, by) == ("failure", "system")
        assert "still not recovered" in correction

    def test_row_without_prediction_is_skipped(self, monkeypatch):
        import utils.knowledge.session_verification as sv
        from utils.ha.device_availability import check_device_predictions
        from utils.hitl.hitl_tracker import stable_nid

        row = PendingPrediction(
            episode_id="e1",
            subject_key="k",
            card_id=stable_nid("device_offline:entry-a"),
            created_ts=_T0,
            prediction=None,
            actioned=False,
            reloaded=False,
        )
        monkeypatch.setattr(sv, "load_pending_predictions", lambda db: [row])
        assert (
            asyncio.run(
                check_device_predictions(
                    "unused", _T0, {"device_offline:entry-a": False}, set()
                )
            )
            == 0
        )

    def test_nothing_to_check(self, tmp_path):
        from utils.ha.device_availability import check_device_predictions

        assert asyncio.run(check_device_predictions("unused", 0.0, {}, set())) == 0


# ---------------------------------------------------------------------------
# Host-health verifier
# ---------------------------------------------------------------------------


class TestVerifyHostHealthSessions:
    def _setup(self, tmp_path, monkeypatch):
        import agents.ha_agent_advanced as adv

        db = _make_db(tmp_path)
        monkeypatch.setattr(adv, "DB_PATH", db)
        return db

    def test_unexpected_reboot_clear_never_verifies(self, tmp_path, monkeypatch):
        from utils.disk.resource import verify_host_health_sessions

        db = self._setup(tmp_path, monkeypatch)
        _insert_session(
            db,
            "e1",
            subject_key="unexpected_reboot",
            prediction={"kind": "recovers_without_action", "within_minutes": 60},
            steps=[_step("call_service", True)],
        )
        n = asyncio.run(
            verify_host_health_sessions(db, time.time(), {"unexpected_reboot"}, set())
        )
        assert n == 0
        assert _row(db, "e1")[0] == "pending"

    def test_cleared_without_prediction_or_action_stays_pending(
        self, tmp_path, monkeypatch
    ):
        from utils.disk.resource import verify_host_health_sessions

        db = self._setup(tmp_path, monkeypatch)
        _insert_session(db, "e1", subject_key="swap_pressure")
        n = asyncio.run(
            verify_host_health_sessions(db, time.time(), {"swap_pressure"}, set())
        )
        assert n == 0
        assert _row(db, "e1")[0] == "pending"

    def test_cleared_after_action_is_success(self, tmp_path, monkeypatch):
        from utils.disk.resource import verify_host_health_sessions

        db = self._setup(tmp_path, monkeypatch)
        _insert_session(
            db, "e1", subject_key="memory_low", steps=[_step("call_service", True)]
        )
        n = asyncio.run(
            verify_host_health_sessions(db, time.time(), {"memory_low"}, set())
        )
        assert n == 1
        assert _row(db, "e1")[:2] == ("success", "system")

    def test_prediction_judged_on_clear_and_deadline(self, tmp_path, monkeypatch):
        from utils.disk.resource import verify_host_health_sessions

        db = self._setup(tmp_path, monkeypatch)
        now = time.time()
        _insert_session(
            db,
            "cleared",
            subject_key="supervisor_unhealthy:docker",
            created_ts=now - 600,
            prediction={"kind": "recovers_without_action", "within_minutes": 30},
        )
        _insert_session(
            db,
            "stuck",
            subject_key="disk_wear",
            created_ts=now - 7200,
            prediction={"kind": "stays_offline", "within_minutes": 60},
        )
        _insert_session(
            db,
            "early",
            subject_key="swap_pressure",
            created_ts=now - 60,
            prediction={"kind": "stays_offline", "within_minutes": 60},
        )
        n = asyncio.run(
            verify_host_health_sessions(
                db,
                now,
                cleared={"supervisor_unhealthy:docker"},
                active={"disk_wear", "swap_pressure"},
            )
        )
        assert n == 2
        assert _row(db, "cleared")[0] == "success"
        assert _row(db, "stuck")[0] == "success"
        assert _row(db, "early")[0] == "pending"
