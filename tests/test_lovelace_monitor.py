"""Tests for ha_lovelace_monitor — entity extraction, analysis, polling loop."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# _extract_entity_refs
# ---------------------------------------------------------------------------


class TestExtractEntityRefs:
    def test_flat_card_entity(self):
        from ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "entity", "entity": "sensor.foo"}],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        assert len(refs) == 1
        assert refs[0].entity_id == "sensor.foo"
        assert refs[0].view_title == "Home"
        assert refs[0].card_index == 0

    def test_entities_list_mixed(self):
        from ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Living Room",
                    "cards": [
                        {
                            "type": "entities",
                            "entities": [
                                "light.ceiling",
                                {"entity": "switch.fan"},
                                "sensor.temp",
                            ],
                        }
                    ],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        entity_ids = {r.entity_id for r in refs}
        assert entity_ids == {"light.ceiling", "switch.fan", "sensor.temp"}

    def test_nested_cards(self):
        from ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Overview",
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {"type": "entity", "entity": "sensor.outer"},
                                {
                                    "type": "horizontal-stack",
                                    "cards": [
                                        {"type": "entity", "entity": "sensor.inner"}
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        entity_ids = {r.entity_id for r in refs}
        assert entity_ids == {"sensor.outer", "sensor.inner"}

    def test_badges(self):
        from ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Status",
                    "badges": [
                        "binary_sensor.door",
                        {"entity": "binary_sensor.motion"},
                    ],
                    "cards": [],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        entity_ids = {r.entity_id for r in refs}
        assert entity_ids == {"binary_sensor.door", "binary_sensor.motion"}

    def test_deduplication(self):
        from ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Main",
                    "cards": [
                        {"type": "entity", "entity": "sensor.dup"},
                        {"type": "entity", "entity": "sensor.dup"},
                    ],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        assert len(refs) == 1
        assert refs[0].entity_id == "sensor.dup"

    def test_empty_config(self):
        from ha_lovelace_monitor import _extract_entity_refs

        refs = _extract_entity_refs({})
        assert refs == []


# ---------------------------------------------------------------------------
# _fuzzy_candidates
# ---------------------------------------------------------------------------


class TestFuzzyCandidates:
    def test_same_domain_preferred(self):
        from ha_lovelace_monitor import _fuzzy_candidates

        registry_ids = {
            "sensor.temperature_bedroom",
            "sensor.temperature_living_room",
            "light.bedroom",
        }
        candidates = _fuzzy_candidates("sensor.temperature_room", registry_ids)
        # should include sensor domain only
        for c in candidates:
            assert c.startswith("sensor.")

    def test_no_candidates_when_domain_absent(self):
        from ha_lovelace_monitor import _fuzzy_candidates

        registry_ids = {"light.ceiling", "switch.fan"}
        candidates = _fuzzy_candidates("sensor.foo", registry_ids)
        assert candidates == []


# ---------------------------------------------------------------------------
# DashboardEntityAnalysis
# ---------------------------------------------------------------------------


class TestDashboardEntityAnalysis:
    def test_valid_construction(self):
        from ha_lovelace_monitor import DashboardEntityAnalysis

        a = DashboardEntityAnalysis(
            explanation="Entity was renamed.",
            likely_cause="renamed",
            action="replace",
            proposed_entity_id="sensor.new_name",
        )
        assert a.action == "replace"
        assert a.proposed_entity_id == "sensor.new_name"

    def test_construction_investigate_no_proposed(self):
        from ha_lovelace_monitor import DashboardEntityAnalysis

        a = DashboardEntityAnalysis(
            explanation="Unknown.", likely_cause="deleted", action="investigate"
        )
        assert a.proposed_entity_id is None

    def test_json_round_trip(self):
        from ha_lovelace_monitor import DashboardEntityAnalysis

        raw = json.dumps(
            {
                "explanation": "Entity deleted.",
                "likely_cause": "deleted",
                "action": "remove",
                "proposed_entity_id": None,
            }
        )
        a = DashboardEntityAnalysis.model_validate_json(raw)
        assert a.action == "remove"
        assert a.proposed_entity_id is None


# ---------------------------------------------------------------------------
# FakeHAWebSocketClient — get_entity_registry
# ---------------------------------------------------------------------------


class TestFakeWsGetEntityRegistry:
    def test_returns_entity_list(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        entities = [{"entity_id": "sensor.foo"}, {"entity_id": "light.bar"}]
        ws = FakeHAWebSocketClient(entity_registry=entities)
        result = asyncio.run(ws.get_entity_registry())
        assert result == entities
        assert "get_entity_registry" in ws.calls

    def test_empty_by_default(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        result = asyncio.run(ws.get_entity_registry())
        assert result == []


# ---------------------------------------------------------------------------
# FakeHARestClient — post
# ---------------------------------------------------------------------------


class TestFakeRestPost:
    def test_post_records_call(self):
        from utils.ha_rest_client import FakeHARestClient

        rest = FakeHARestClient()
        payload = {"views": []}
        result = asyncio.run(rest.post("/api/lovelace/config", payload))
        assert result == {}
        assert rest.posted == [("/api/lovelace/config", payload)]


# ---------------------------------------------------------------------------
# poll_for_dashboard_entity_issues — unit-level tests via fakes
# ---------------------------------------------------------------------------


def _make_hitl_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE hitl_suppression (
                card_key TEXT PRIMARY KEY,
                card_type TEXT DEFAULT '',
                description TEXT DEFAULT '',
                first_sent_at REAL,
                last_sent_at REAL,
                send_count INTEGER DEFAULT 1,
                known_issue INTEGER DEFAULT 0,
                known_issue_note TEXT DEFAULT '',
                last_action TEXT,
                last_action_at REAL,
                rejection_count INTEGER DEFAULT 0,
                next_allowed_at REAL,
                resolved_at REAL
            )
            """
        )
    return db_path


class TestPollMissingEntity:
    def test_missing_entity_sends_card(self, tmp_path):
        from ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha_rest_client import FakeHARestClient
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "entity", "entity": "sensor.missing"}],
                }
            ]
        }
        rest = FakeHARestClient(raw_responses={"/api/lovelace/config": lovelace})
        ws = FakeHAWebSocketClient(entity_registry=[{"entity_id": "sensor.present"}])
        notifier = FakeNotifier()

        analysis_json = json.dumps(
            {
                "explanation": "Entity was deleted.",
                "likely_cause": "deleted",
                "action": "remove",
                "proposed_entity_id": None,
            }
        )
        llm = FakeLLMClient(analysis_json)

        async def _run():
            coro = poll_for_dashboard_entity_issues(
                rest_client=rest,
                ws_client=ws,
                notifier=notifier,
                db_path=db_path,
                interval_minutes=0,
                llm_client=llm,
            )
            task = asyncio.create_task(coro)
            # Allow the loop to run through one iteration (sleep(0) then body).
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["payload"]["entity_id"] == "sensor.missing"
        assert notifier.sent[0]["payload"]["action"] == "remove"

    def test_present_entity_no_card(self, tmp_path):
        from ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha_rest_client import FakeHARestClient
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "entity", "entity": "sensor.present"}],
                }
            ]
        }
        rest = FakeHARestClient(raw_responses={"/api/lovelace/config": lovelace})
        ws = FakeHAWebSocketClient(entity_registry=[{"entity_id": "sensor.present"}])
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
                    rest_client=rest,
                    ws_client=ws,
                    notifier=notifier,
                    db_path=db_path,
                    interval_minutes=0,
                    llm_client=llm,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert len(notifier.sent) == 0

    def test_duplicate_suppression(self, tmp_path):
        from ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha_rest_client import FakeHARestClient
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed the suppression table so the card is already "sent".
        from utils.hitl_tracker import mark_card_sent

        with sqlite3.connect(db_path) as conn:
            mark_card_sent(
                conn, "dashboard_entity:sensor.gone", "dashboard_entity", "test"
            )

        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "entity", "entity": "sensor.gone"}],
                }
            ]
        }
        rest = FakeHARestClient(raw_responses={"/api/lovelace/config": lovelace})
        ws = FakeHAWebSocketClient(entity_registry=[])
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
                    rest_client=rest,
                    ws_client=ws,
                    notifier=notifier,
                    db_path=db_path,
                    interval_minutes=0,
                    llm_client=llm,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        # No new card since one is already pending.
        assert len(notifier.sent) == 0

    def test_reconcile_resolved(self, tmp_path):
        from ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha_rest_client import FakeHARestClient
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed a pending card for sensor.recovered.
        from utils.hitl_tracker import mark_card_sent

        with sqlite3.connect(db_path) as conn:
            mark_card_sent(
                conn,
                "dashboard_entity:sensor.recovered",
                "dashboard_entity",
                "test",
            )

        # Lovelace now has sensor.recovered present in the registry.
        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "entity", "entity": "sensor.recovered"}],
                }
            ]
        }
        rest = FakeHARestClient(raw_responses={"/api/lovelace/config": lovelace})
        ws = FakeHAWebSocketClient(entity_registry=[{"entity_id": "sensor.recovered"}])
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
                    rest_client=rest,
                    ws_client=ws,
                    notifier=notifier,
                    db_path=db_path,
                    interval_minutes=0,
                    llm_client=llm,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert len(notifier.sent) == 0  # entity is present — no new card
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression"
                " WHERE card_key = 'dashboard_entity:sensor.recovered'",
            ).fetchone()
        assert row is not None and row[0] is not None  # resolved_at set


# ---------------------------------------------------------------------------
# _analyze_missing_entity — unit test with fake LLM
# ---------------------------------------------------------------------------


class TestAnalyzeMissingEntity:
    def test_returns_replace_analysis(self):
        from ha_lovelace_monitor import EntityRef, _analyze_missing_entity
        from utils.ollama_client import FakeLLMClient

        raw = json.dumps(
            {
                "explanation": "Renamed.",
                "likely_cause": "renamed",
                "action": "replace",
                "proposed_entity_id": "sensor.new_temp",
            }
        )
        llm = FakeLLMClient(raw)
        ref = EntityRef("sensor.old_temp", "Main", 0, "card[0].entity")
        registry = [{"entity_id": "sensor.new_temp"}]

        result = asyncio.run(_analyze_missing_entity(ref, registry, llm))
        assert result.action == "replace"
        assert result.proposed_entity_id == "sensor.new_temp"
        assert result.likely_cause == "renamed"

    def test_returns_remove_analysis(self):
        from ha_lovelace_monitor import EntityRef, _analyze_missing_entity
        from utils.ollama_client import FakeLLMClient

        raw = json.dumps(
            {
                "explanation": "Integration removed.",
                "likely_cause": "integration_removed",
                "action": "remove",
                "proposed_entity_id": None,
            }
        )
        llm = FakeLLMClient(raw)
        ref = EntityRef("sensor.gone", "Main", 1, "card[1].entity")
        registry: list[dict] = []

        result = asyncio.run(_analyze_missing_entity(ref, registry, llm))
        assert result.action == "remove"
        assert result.proposed_entity_id is None

    def test_llm_failure_returns_safe_default(self):
        from ha_lovelace_monitor import EntityRef, _analyze_missing_entity
        from utils.ollama_client import FakeLLMClient

        llm = FakeLLMClient("NOT VALID JSON !!!")
        ref = EntityRef("sensor.bad", "Main", 0, "card[0].entity")

        result = asyncio.run(_analyze_missing_entity(ref, [], llm))
        assert result.action == "investigate"
        assert "sensor.bad" in result.explanation
