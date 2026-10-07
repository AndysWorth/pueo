"""Tests for create_automation and delete_automation in FakeHARestClient."""

from __future__ import annotations

import asyncio


class TestCreateAutomation:
    def test_create_records_in_posted(self):
        """create_automation appends to posted with the correct path."""
        from utils.ha.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient()
        config = {
            "alias": "Turn off lights",
            "description": "Turns off all lights at midnight",
            "mode": "single",
            "trigger": [{"platform": "time", "at": "00:00:00"}],
            "action": [{"service": "light.turn_off", "target": {"entity_id": "all"}}],
        }
        asyncio.run(
            fake.create_automation("pueo_auto_turn_off_lights_abc12345", config)
        )
        assert len(fake.posted) == 1
        path, payload = fake.posted[0]
        assert "pueo_auto_turn_off_lights_abc12345" in path
        assert "/api/config/automation/config/" in path
        assert payload["alias"] == "Turn off lights"
        assert payload["mode"] == "single"

    def test_create_multiple_automations(self):
        """Each create_automation call appends a distinct entry."""
        from utils.ha.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient()
        asyncio.run(
            fake.create_automation("pueo_auto_first_aaa11111", {"alias": "First"})
        )
        asyncio.run(
            fake.create_automation("pueo_auto_second_bbb22222", {"alias": "Second"})
        )
        assert len(fake.posted) == 2
        assert "pueo_auto_first_aaa11111" in fake.posted[0][0]
        assert "pueo_auto_second_bbb22222" in fake.posted[1][0]


class TestDeleteAutomation:
    def test_delete_records_in_deleted(self):
        """delete_automation appends the path to deleted."""
        from utils.ha.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient()
        asyncio.run(fake.delete_automation("pueo_auto_some_id_12345678"))
        assert len(fake.deleted) == 1
        assert "pueo_auto_some_id_12345678" in fake.deleted[0]
        assert "/api/config/automation/config/" in fake.deleted[0]

    def test_delete_multiple_unique_ids(self):
        """Each delete_automation call appends a distinct path."""
        from utils.ha.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient()
        asyncio.run(fake.delete_automation("pueo_auto_id_one_aaaaaaaa"))
        asyncio.run(fake.delete_automation("pueo_auto_id_two_bbbbbbbb"))
        assert len(fake.deleted) == 2
        assert "pueo_auto_id_one_aaaaaaaa" in fake.deleted[0]
        assert "pueo_auto_id_two_bbbbbbbb" in fake.deleted[1]
