"""Tests for utils.ha.lovelace_utils pure-logic functions."""

from __future__ import annotations

import pytest


class TestExtractEntityRefs:
    """_extract_entity_refs walks Lovelace config and collects entity references."""

    def _extract(self, cfg):
        from utils.ha.lovelace_utils import _extract_entity_refs

        return {ref.entity_id for ref in _extract_entity_refs(cfg)}

    def test_plain_entity_key_in_card(self):
        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [{"type": "entity", "entity": "light.living_room"}],
                }
            ]
        }
        assert "light.living_room" in self._extract(cfg)

    def test_entities_list_in_card(self):
        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [
                        {
                            "type": "entities",
                            "entities": [
                                "sensor.temp",
                                {"entity": "switch.fan"},
                            ],
                        }
                    ],
                }
            ]
        }
        refs = self._extract(cfg)
        assert "sensor.temp" in refs
        assert "switch.fan" in refs

    def test_nested_cards(self):
        cfg = {
            "views": [
                {
                    "title": "Grid",
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {"type": "entity", "entity": "binary_sensor.door"}
                            ],
                        }
                    ],
                }
            ]
        }
        assert "binary_sensor.door" in self._extract(cfg)

    def test_sections_layout(self):
        cfg = {
            "views": [
                {
                    "title": "Sections",
                    "sections": [
                        {"cards": [{"type": "entity", "entity": "cover.garage"}]}
                    ],
                }
            ]
        }
        assert "cover.garage" in self._extract(cfg)

    def test_deduplication(self):
        cfg = {
            "views": [
                {
                    "title": "Home",
                    "cards": [
                        {"type": "entity", "entity": "light.lamp"},
                        {"type": "entities", "entities": ["light.lamp"]},
                    ],
                }
            ]
        }
        refs = list(
            ref
            for ref in __import__(
                "utils.ha.lovelace_utils", fromlist=["_extract_entity_refs"]
            )._extract_entity_refs(cfg)
            if ref.entity_id == "light.lamp"
        )
        assert len(refs) == 1

    def test_empty_config_returns_empty(self):
        assert self._extract({}) == set()

    def test_badges(self):
        cfg = {
            "views": [
                {
                    "title": "Home",
                    "badges": ["person.alice"],
                    "cards": [],
                }
            ]
        }
        assert "person.alice" in self._extract(cfg)


class TestFuzzyCandidates:
    """_fuzzy_candidates returns same-domain entities ranked by edit distance."""

    def _candidates(self, entity_id, registry_ids, max_results=5):
        from utils.ha.lovelace_utils import _fuzzy_candidates

        return _fuzzy_candidates(entity_id, set(registry_ids), max_results)

    def test_exact_match_returned_first(self):
        result = self._candidates(
            "light.living_room",
            ["light.living_room", "light.bedroom", "switch.fan"],
        )
        assert result[0] == "light.living_room"

    def test_only_same_domain_returned(self):
        result = self._candidates(
            "sensor.temp",
            ["sensor.temperature", "light.bulb", "binary_sensor.motion"],
        )
        assert all(r.startswith("sensor.") for r in result)

    def test_empty_registry_returns_empty(self):
        assert self._candidates("light.x", []) == []

    def test_max_results_honored(self):
        registry = [f"light.lamp_{i}" for i in range(20)]
        result = self._candidates("light.lamp_0", registry, max_results=3)
        assert len(result) <= 3
