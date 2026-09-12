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
        from agents.ha_lovelace_monitor import _extract_entity_refs

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

    def test_sections_layout(self):
        from agents.ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Main",
                    "type": "sections",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [
                                {"type": "tile", "entity": "sensor.in_section"},
                                {"type": "tile", "entity": "light.also_in_section"},
                            ],
                        }
                    ],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        entity_ids = {r.entity_id for r in refs}
        assert entity_ids == {"sensor.in_section", "light.also_in_section"}

    def test_entity_id_key(self):
        from agents.ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "picture-entity", "entity_id": "camera.front"}],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        assert len(refs) == 1
        assert refs[0].entity_id == "camera.front"

    def test_entity_id_in_entities_list(self):
        from agents.ha_lovelace_monitor import _extract_entity_refs

        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [
                        {
                            "type": "entities",
                            "entities": [{"entity_id": "sensor.custom_card"}],
                        }
                    ],
                }
            ]
        }
        refs = _extract_entity_refs(cfg)
        assert len(refs) == 1
        assert refs[0].entity_id == "sensor.custom_card"

    def test_entities_list_mixed(self):
        from agents.ha_lovelace_monitor import _extract_entity_refs

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
        from agents.ha_lovelace_monitor import _extract_entity_refs

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
        from agents.ha_lovelace_monitor import _extract_entity_refs

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
        from agents.ha_lovelace_monitor import _extract_entity_refs

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
        from agents.ha_lovelace_monitor import _extract_entity_refs

        refs = _extract_entity_refs({})
        assert refs == []


# ---------------------------------------------------------------------------
# _fuzzy_candidates
# ---------------------------------------------------------------------------


class TestFuzzyCandidates:
    def test_same_domain_preferred(self):
        from utils.ha.lovelace_utils import _fuzzy_candidates

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
        from utils.ha.lovelace_utils import _fuzzy_candidates

        registry_ids = {"light.ceiling", "switch.fan"}
        candidates = _fuzzy_candidates("sensor.foo", registry_ids)
        assert candidates == []


# ---------------------------------------------------------------------------
# FakeHAWebSocketClient — lovelace + entity_registry methods
# ---------------------------------------------------------------------------


class TestFakeWsGetEntityRegistry:
    def test_returns_entity_list(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        entities = [{"entity_id": "sensor.foo"}, {"entity_id": "light.bar"}]
        ws = FakeHAWebSocketClient(entity_registry=entities)
        result = asyncio.run(ws.get_entity_registry())
        assert result == entities
        assert "get_entity_registry" in ws.calls

    def test_empty_by_default(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        result = asyncio.run(ws.get_entity_registry())
        assert result == []


class TestFakeWsGetStates:
    def test_returns_states_list(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        states = [
            {"entity_id": "sensor.high_tide", "state": "2.3"},
            {"entity_id": "light.lamp", "state": "on"},
        ]
        ws = FakeHAWebSocketClient(states=states)
        result = asyncio.run(ws.get_states())
        assert result == states
        assert "get_states" in ws.calls

    def test_empty_by_default(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        result = asyncio.run(ws.get_states())
        assert result == []


class TestFakeWsLovelace:
    def test_get_lovelace_dashboards(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        dashboards = [{"url_path": "dashboard-dashy", "title": "Dashy"}]
        ws = FakeHAWebSocketClient(lovelace_dashboards=dashboards)
        result = asyncio.run(ws.get_lovelace_dashboards())
        assert result == dashboards
        assert "get_lovelace_dashboards" in ws.calls

    def test_get_lovelace_config_default(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        cfg = {"views": [{"title": "Main", "cards": []}]}
        ws = FakeHAWebSocketClient(lovelace_configs={None: cfg})
        result = asyncio.run(ws.get_lovelace_config(None))
        assert result == cfg
        assert "get_lovelace_config:None" in ws.calls

    def test_get_lovelace_config_named(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        cfg = {"views": [{"title": "Dashy", "cards": []}]}
        ws = FakeHAWebSocketClient(lovelace_configs={"dashboard-dashy": cfg})
        result = asyncio.run(ws.get_lovelace_config("dashboard-dashy"))
        assert result == cfg

    def test_get_lovelace_config_missing_raises(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        with pytest.raises(RuntimeError):
            asyncio.run(ws.get_lovelace_config(None))


# ---------------------------------------------------------------------------
# FakeHARestClient — post
# ---------------------------------------------------------------------------


class TestFakeRestPost:
    def test_post_records_call(self):
        from utils.ha.ha_rest_client import FakeHARestClient

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


def _make_ws(lovelace_cfg: dict, entity_registry: list[dict]):  # type: ignore[return]
    """Helper: fake WS client with a single default dashboard config."""
    from utils.ha.ha_ws_client import FakeHAWebSocketClient

    return FakeHAWebSocketClient(
        entity_registry=entity_registry,
        lovelace_configs={None: lovelace_cfg},
    )


class TestPollMissingEntity:
    def test_truly_missing_entity_routed_to_investigation(self, tmp_path):
        """Entities absent from both registry and states are routed to AgentLoop, not a direct card."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.hitl.notify import FakeNotifier
        from unittest import mock

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "entity", "entity": "sensor.missing"}],
                }
            ]
        }
        ws = _make_ws(lovelace, [{"entity_id": "sensor.present"}])
        notifier = FakeNotifier()
        investigation_calls: list[list[dict]] = []

        async def _fake_investigation(suspicious, **kw):
            investigation_calls.append(list(suspicious))

        with mock.patch(
            "agents.ha_lovelace_monitor._run_lovelace_investigation",
            side_effect=_fake_investigation,
        ):

            async def _run():
                task = asyncio.create_task(
                    poll_for_dashboard_entity_issues(
                        ws_client=ws,
                        notifier=notifier,
                        db_path=db_path,
                        interval_minutes=0,
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
        assert len(investigation_calls) >= 1
        first_call = investigation_calls[0]
        assert any(e["entity_id"] == "sensor.missing" for e in first_call)
        missing = next(e for e in first_call if e["entity_id"] == "sensor.missing")
        assert missing["has_state"] is False

    def test_present_entity_no_card(self, tmp_path):
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "entity", "entity": "sensor.present"}],
                }
            ]
        }
        ws = _make_ws(lovelace, [{"entity_id": "sensor.present"}])
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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

    def test_reconcile_resolved(self, tmp_path):
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed a pending card for sensor.recovered.
        from utils.hitl.hitl_tracker import mark_card_sent

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
        ws = _make_ws(lovelace, [{"entity_id": "sensor.recovered"}])
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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

    def test_named_dashboard_truly_missing_entity_routed(self, tmp_path):
        """Entity absent from a named dashboard is routed to AgentLoop, not a direct card."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from unittest import mock

        db_path = _make_hitl_db(tmp_path)
        dashy_cfg = {
            "views": [
                {
                    "title": "Dashy Main",
                    "cards": [{"type": "tile", "entity": "sensor.in_named_dash"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_dashboards=[{"url_path": "dashboard-dashy", "title": "Dashy"}],
            lovelace_configs={"dashboard-dashy": dashy_cfg},
        )
        notifier = FakeNotifier()
        investigation_calls: list[list[dict]] = []

        async def _fake_investigation(suspicious, **kw):
            investigation_calls.append(list(suspicious))

        with mock.patch(
            "agents.ha_lovelace_monitor._run_lovelace_investigation",
            side_effect=_fake_investigation,
        ):

            async def _run():
                task = asyncio.create_task(
                    poll_for_dashboard_entity_issues(
                        ws_client=ws,
                        notifier=notifier,
                        db_path=db_path,
                        interval_minutes=0,
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
        assert len(investigation_calls) >= 1
        assert any(
            e["entity_id"] == "sensor.in_named_dash" for e in investigation_calls[0]
        )

    def test_unregistered_but_present_in_states_goes_to_investigation(self, tmp_path):
        """Entities in hass.states but not the registry are passed to _run_lovelace_investigation."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Home",
                    "cards": [
                        {
                            "type": "entities",
                            "title": "Gloucester Harbor (tomorrow)",
                            "entities": ["sensor.high_tide"],
                        }
                    ],
                }
            ]
        }
        # entity_registry is empty (no unique_id), but hass.states has the entity.
        # The poll loop now passes this to _run_lovelace_investigation (AgentLoop).
        # FakeLLMClient does not implement chat_with_tools, so the investigation fails
        # silently — no direct card is sent by the poll loop itself.
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_configs={None: lovelace},
            states=[{"entity_id": "sensor.high_tide", "state": "2.3"}],
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        assert "get_states" in ws.calls
        # No direct card from the poll loop — classification is delegated to AgentLoop
        assert len(notifier.sent) == 0

    def test_absent_from_both_registry_and_states_routed(self, tmp_path):
        """Entities absent from both registry and hass.states are routed to investigation."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from unittest import mock

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "entity", "entity": "sensor.truly_gone"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_configs={None: lovelace},
            states=[{"entity_id": "sensor.something_else", "state": "ok"}],
        )
        notifier = FakeNotifier()
        investigation_calls: list[list[dict]] = []

        async def _fake_investigation(suspicious, **kw):
            investigation_calls.append(list(suspicious))

        with mock.patch(
            "agents.ha_lovelace_monitor._run_lovelace_investigation",
            side_effect=_fake_investigation,
        ):

            async def _run():
                task = asyncio.create_task(
                    poll_for_dashboard_entity_issues(
                        ws_client=ws,
                        notifier=notifier,
                        db_path=db_path,
                        interval_minutes=0,
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
        assert len(investigation_calls) >= 1
        truly_gone = next(
            e for e in investigation_calls[0] if e["entity_id"] == "sensor.truly_gone"
        )
        assert truly_gone["has_state"] is False

    def test_sections_layout_missing_entity_routed(self, tmp_path):
        """Truly missing entity in a sections-layout dashboard is routed to AgentLoop."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.hitl.notify import FakeNotifier
        from unittest import mock

        db_path = _make_hitl_db(tmp_path)
        sections_cfg = {
            "views": [
                {
                    "title": "Main",
                    "type": "sections",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [{"type": "tile", "entity": "sensor.in_section"}],
                        }
                    ],
                }
            ]
        }
        ws = _make_ws(sections_cfg, [])
        notifier = FakeNotifier()
        investigation_calls: list[list[dict]] = []

        async def _fake_investigation(suspicious, **kw):
            investigation_calls.append(list(suspicious))

        with mock.patch(
            "agents.ha_lovelace_monitor._run_lovelace_investigation",
            side_effect=_fake_investigation,
        ):

            async def _run():
                task = asyncio.create_task(
                    poll_for_dashboard_entity_issues(
                        ws_client=ws,
                        notifier=notifier,
                        db_path=db_path,
                        interval_minutes=0,
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
        assert len(investigation_calls) >= 1
        assert any(
            e["entity_id"] == "sensor.in_section" for e in investigation_calls[0]
        )

    def test_unregistered_entity_delegated_to_investigation(self, tmp_path):
        """Entities with state but no registry entry are delegated to _run_lovelace_investigation."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "sensor", "entity": "sensor.high_tide"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_configs={None: lovelace},
            states=[{"entity_id": "sensor.high_tide"}],
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        # Classification delegated to AgentLoop — poll loop sends no card directly
        assert len(notifier.sent) == 0

    def test_unregistered_entity_resolves_when_registered(self, tmp_path):
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.hitl_tracker import mark_card_sent
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed: entity was previously flagged as unregistered.
        with sqlite3.connect(db_path) as conn:
            mark_card_sent(
                conn,
                "unregistered_entity:sensor.high_tide",
                "unregistered_entity",
                "has no unique_id",
            )

        # Next poll: entity now appears in the registry.
        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "sensor", "entity": "sensor.high_tide"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.high_tide"}],
            lovelace_configs={None: lovelace},
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression"
                " WHERE card_key = 'unregistered_entity:sensor.high_tide'"
            ).fetchone()
        assert row is not None and row[0] is not None

    def test_unregistered_entity_no_direct_card_sent(self, tmp_path):
        """Unregistered entities with live state produce no direct card — AgentLoop investigates."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "weather", "entity": "sun.sun"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_configs={None: lovelace},
            states=[{"entity_id": "sun.sun", "state": "above_horizon"}],
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        # Poll loop no longer fetches config_entries directly (AgentLoop does that)
        assert "get_config_entries" not in ws.calls
        # No card from the poll loop itself — investigation is delegated to AgentLoop
        assert len(notifier.sent) == 0

    def test_unregistered_entity_delegated_not_directly_typed(self, tmp_path):
        """Entities with state but absent from registry are not given ha_config_issue cards directly."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        lovelace = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "sensor", "entity": "sensor.high_tide"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_configs={None: lovelace},
            states=[{"entity_id": "sensor.high_tide", "state": "2.3"}],
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        # Classification is done by AgentLoop; poll loop sends nothing directly
        assert len(notifier.sent) == 0

    def test_legacy_unregistered_card_resolved_when_entity_joins_registry(
        self, tmp_path
    ):
        """A legacy unregistered_entity card is resolved when the entity gains a registry entry."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.hitl_tracker import mark_card_sent
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed: sun.sun was previously flagged.
        with sqlite3.connect(db_path) as conn:
            mark_card_sent(
                conn,
                "unregistered_entity:sun.sun",
                "unregistered_entity",
                "has no unique_id",
            )

        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "weather", "entity": "sun.sun"}],
                }
            ]
        }
        # Entity now appears in the entity registry — card should be resolved.
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sun.sun"}],
            lovelace_configs={None: lovelace},
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression"
                " WHERE card_key = 'unregistered_entity:sun.sun'"
            ).fetchone()
        assert row is not None and row[0] is not None, "card must be auto-resolved"


# ---------------------------------------------------------------------------
# FakeHAWebSocketClient — new get_all_config_entries / get_ha_components
# ---------------------------------------------------------------------------


class TestFakeWSNewMethods:
    def test_get_all_config_entries_returns_all(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        entries = [
            {"domain": "sun", "state": "loaded"},
            {"domain": "zha", "state": "not_loaded"},
        ]
        ws = FakeHAWebSocketClient(config_entries=entries)
        result = asyncio.run(ws.get_all_config_entries())
        assert result == entries
        assert "get_all_config_entries" in ws.calls

    def test_get_ha_components_returns_list(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        components = ["sun", "binary_sensor", "sun.binary_sensor"]
        ws = FakeHAWebSocketClient(ha_components=components)
        result = asyncio.run(ws.get_ha_components())
        assert result == components
        assert "get_ha_components" in ws.calls

    def test_get_all_config_entries_empty_by_default(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        result = asyncio.run(ws.get_all_config_entries())
        assert result == []


# ---------------------------------------------------------------------------
# _run_lovelace_investigation — integration test with FakeToolCallingLLMClient
# ---------------------------------------------------------------------------


class TestRunLovelaceInvestigation:
    def test_empty_suspicious_list_is_noop(self, tmp_path):
        from agents.ha_lovelace_monitor import _run_lovelace_investigation
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier

        ws = FakeHAWebSocketClient()
        notifier = FakeNotifier()
        db_path = _make_hitl_db(tmp_path)

        asyncio.run(
            _run_lovelace_investigation(
                suspicious=[],
                ws_client=ws,
                db_path=db_path,
                notifier=notifier,
            )
        )
        assert len(notifier.sent) == 0

    def test_investigation_sends_ha_config_issue_card(self, tmp_path):
        """AgentLoop that calls finish_lovelace_investigation creates an ha_config_issue card."""
        from agents.ha_lovelace_monitor import _run_lovelace_investigation
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        db_path = _make_hitl_db(tmp_path)
        ws = FakeHAWebSocketClient(
            states=[{"entity_id": "sensor.high_tide", "state": "2.3"}],
            ha_components=["sensor"],
        )
        notifier = FakeNotifier()

        findings = [
            {
                "entity_ids": ["sensor.high_tide"],
                "title": "YAML entity missing unique_id",
                "description": "sensor.high_tide has live state but no unique_id",
                "suggested_actions": ["Add unique_id: sensor_high_tide to YAML"],
                "chat_needed": False,
                "initial_chat_message": "",
            }
        ]
        llm = FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_lovelace_investigation",
                                "arguments": {"findings": findings},
                            }
                        }
                    ]
                }
            ]
        )

        suspicious = [
            {
                "entity_id": "sensor.high_tide",
                "has_state": True,
                "dashboard": "Default",
                "view": "Main",
                "card_title": None,
            }
        ]

        asyncio.run(
            _run_lovelace_investigation(
                suspicious=suspicious,
                ws_client=ws,
                db_path=db_path,
                notifier=notifier,
                llm_client=llm,
            )
        )

        assert len(notifier.sent) == 1
        p = notifier.sent[0]["payload"]
        assert p["card_type"] == "ha_config_issue"
        assert "sensor.high_tide" in p["entity_ids"]

    def test_ha_config_issue_reconciliation(self, tmp_path):
        """ha_config_issue card is resolved when entity joins the entity registry."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.hitl_tracker import mark_card_sent
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)
        card_key = "ha_config_issue:sensor.high_tide"

        with sqlite3.connect(db_path) as conn:
            mark_card_sent(conn, card_key, "ha_config_issue", "yaml entity no uid")

        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "sensor", "entity": "sensor.high_tide"}],
                }
            ]
        }
        # Entity is now in the registry — card should be resolved
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.high_tide"}],
            lovelace_configs={None: lovelace},
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression WHERE card_key = ?",
                (card_key,),
            ).fetchone()
        assert (
            row is not None and row[0] is not None
        ), "ha_config_issue card must be resolved"


# ---------------------------------------------------------------------------
# Benign entity suppression
# ---------------------------------------------------------------------------


def _make_minimal_executor(db_path: str):
    """Return a ToolExecutor wired to a real SQLite DB with fake dependencies."""
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.hitl.notify import FakeNotifier

    class _NullSSH:
        async def read_file(self, path: str) -> str:
            return ""

        async def write_file(self, path: str, content: str) -> None:
            pass

        async def run(self, command: str, check: bool = False) -> tuple[int, str, str]:
            return (0, "", "")

        async def stream_lines(self, command: str):  # type: ignore[return]
            pass

    return ToolExecutor(
        ha_ssh_client=_NullSSH(),  # type: ignore[arg-type]
        gate=FakeAutonomyGate(auto_execute_result=True),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
        db_path=db_path,
    )


class TestBenignSuppression:
    def test_finish_benign_writes_suppression_records(self, tmp_path):
        """Empty findings with set_lovelace_suspicious writes a lovelace_benign row per entity."""
        db_path = _make_hitl_db(tmp_path)
        executor = _make_minimal_executor(db_path)
        executor.set_lovelace_suspicious(["sensor.high_tide", "sun.sun"])

        asyncio.run(executor._finish_lovelace_investigation(findings=[]))

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT card_key, card_type, resolved_at FROM hitl_suppression"
                " WHERE card_type = 'lovelace_benign'"
            ).fetchall()
        keys = {r[0] for r in rows}
        assert "lovelace_benign:sensor.high_tide" in keys
        assert "lovelace_benign:sun.sun" in keys
        # Records are active (not resolved)
        for _, _, resolved_at in rows:
            assert resolved_at is None

    def test_finish_with_findings_does_not_write_benign(self, tmp_path):
        """When findings are present, no lovelace_benign rows are written."""
        db_path = _make_hitl_db(tmp_path)
        executor = _make_minimal_executor(db_path)
        executor.set_lovelace_suspicious(["sensor.foo"])

        findings = [
            {
                "entity_ids": ["sensor.foo"],
                "title": "YAML entity missing unique_id",
                "description": "No unique_id",
                "suggested_actions": [],
                "chat_needed": False,
                "initial_chat_message": "",
            }
        ]
        asyncio.run(executor._finish_lovelace_investigation(findings=findings))

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT card_key FROM hitl_suppression WHERE card_type = 'lovelace_benign'"
            ).fetchone()
        assert row is None

    def test_poll_skips_benign_suppressed_entity(self, tmp_path):
        """An entity with an active lovelace_benign record is not passed to _run_lovelace_investigation."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.hitl_tracker import mark_card_sent
        from utils.hitl.notify import FakeNotifier
        from unittest import mock

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed a benign suppression record for sensor.high_tide.
        with sqlite3.connect(db_path) as conn:
            mark_card_sent(
                conn,
                "lovelace_benign:sensor.high_tide",
                "lovelace_benign",
                "Benign sub-platform entity",
            )

        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "sensor", "entity": "sensor.high_tide"}],
                }
            ]
        }
        ws = FakeHAWebSocketClient(
            entity_registry=[],
            lovelace_configs={None: lovelace},
            states=[{"entity_id": "sensor.high_tide", "state": "2.3"}],
        )
        notifier = FakeNotifier()
        investigation_calls: list[list[dict]] = []

        async def _fake_investigation(suspicious, **kw):
            investigation_calls.append(list(suspicious))

        with mock.patch(
            "agents.ha_lovelace_monitor._run_lovelace_investigation",
            side_effect=_fake_investigation,
        ):

            async def _run():
                task = asyncio.create_task(
                    poll_for_dashboard_entity_issues(
                        ws_client=ws,
                        notifier=notifier,
                        db_path=db_path,
                        interval_minutes=0,
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(_run())

        # Entity is benign-suppressed — investigation should not be triggered
        assert investigation_calls == [] or all(
            not any(e["entity_id"] == "sensor.high_tide" for e in call)
            for call in investigation_calls
        )

    def test_benign_record_cleared_when_entity_joins_registry(self, tmp_path):
        """A lovelace_benign record is resolved when the entity gains a registry entry."""
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues
        from utils.ha.ha_ws_client import FakeHAWebSocketClient
        from utils.hitl.hitl_tracker import mark_card_sent
        from utils.hitl.notify import FakeNotifier
        from utils.llm.ollama_client import FakeLLMClient

        db_path = _make_hitl_db(tmp_path)

        # Pre-seed a benign suppression record.
        with sqlite3.connect(db_path) as conn:
            mark_card_sent(
                conn,
                "lovelace_benign:sensor.high_tide",
                "lovelace_benign",
                "Benign sub-platform entity",
            )

        lovelace = {
            "views": [
                {
                    "title": "Main",
                    "cards": [{"type": "sensor", "entity": "sensor.high_tide"}],
                }
            ]
        }
        # Entity now in the registry — benign record should be cleared.
        ws = FakeHAWebSocketClient(
            entity_registry=[{"entity_id": "sensor.high_tide"}],
            lovelace_configs={None: lovelace},
        )
        notifier = FakeNotifier()
        llm = FakeLLMClient("{}")

        async def _run():
            task = asyncio.create_task(
                poll_for_dashboard_entity_issues(
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
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM hitl_suppression"
                " WHERE card_key = 'lovelace_benign:sensor.high_tide'"
            ).fetchone()
        assert row is not None and row[0] is not None, "benign record must be cleared"
