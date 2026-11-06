"""Tests for utils/ha/config_flow_driver.py."""

import asyncio
import logging

import pytest

from utils.ha.config_flow_driver import (
    FlowResult,
    drive_config_flow,
    drive_repair_flow,
    summarize_flow_step,
)
from utils.ha.ha_rest_client import FakeHARestClient


# ---------------------------------------------------------------------------
# summarize_flow_step
# ---------------------------------------------------------------------------


class TestSummarizeFlowStep:
    def test_menu_step_no_translations(self):
        resp = {
            "type": "menu",
            "step_id": "user",
            "menu_options": ["location", "surf"],
        }
        out = summarize_flow_step(resp)
        assert "Step: user (menu)" in out
        assert "- location: location" in out
        assert "- surf: surf" in out

    def test_menu_step_with_translations(self):
        translations = {
            "config": {
                "step": {
                    "user": {
                        "menu_options": {
                            "location": "Weather & Surf",
                            "surf": "Surf only",
                        }
                    }
                }
            }
        }
        resp = {
            "type": "menu",
            "step_id": "user",
            "menu_options": ["location", "surf"],
        }
        out = summarize_flow_step(resp, translations)
        assert "- location: Weather & Surf" in out
        assert "- surf: Surf only" in out

    def test_form_step_basic(self):
        resp = {
            "type": "form",
            "step_id": "location",
            "data_schema": [
                {"name": "latitude", "required": True, "default": 42.6},
                {"name": "longitude", "required": True},
            ],
        }
        out = summarize_flow_step(resp)
        assert "Step: location (form)" in out
        assert "latitude" in out
        assert "required" in out
        assert "42.6" in out
        assert "longitude" in out

    def test_form_step_with_select_selector(self):
        resp = {
            "type": "form",
            "step_id": "office",
            "data_schema": [
                {
                    "name": "office_code",
                    "required": True,
                    "selector": {"select": {"options": ["BOX", "GYX", "CAR"]}},
                }
            ],
        }
        out = summarize_flow_step(resp)
        assert "BOX" in out
        assert "choices=" in out

    def test_form_step_select_options_as_dicts(self):
        resp = {
            "type": "form",
            "step_id": "office",
            "data_schema": [
                {
                    "name": "office_code",
                    "required": True,
                    "selector": {
                        "select": {
                            "options": [
                                {"value": "BOX", "label": "Boston"},
                                {"value": "GYX", "label": "Gray"},
                            ]
                        }
                    },
                }
            ],
        }
        out = summarize_flow_step(resp)
        assert "BOX" in out

    def test_form_step_select_truncated_at_five(self):
        resp = {
            "type": "form",
            "step_id": "s",
            "data_schema": [
                {
                    "name": "x",
                    "required": True,
                    "selector": {"select": {"options": ["a", "b", "c", "d", "e", "f"]}},
                }
            ],
        }
        out = summarize_flow_step(resp)
        assert "…" in out

    def test_form_step_with_translations(self):
        translations = {
            "config": {
                "step": {
                    "location": {
                        "data": {
                            "latitude": "Latitude",
                            "longitude": "Longitude",
                        }
                    }
                }
            }
        }
        resp = {
            "type": "form",
            "step_id": "location",
            "data_schema": [
                {"name": "latitude", "required": True},
            ],
        }
        out = summarize_flow_step(resp, translations)
        assert "(Latitude)" in out

    def test_errors_included(self):
        resp = {
            "type": "form",
            "step_id": "location",
            "data_schema": [],
            "errors": {"latitude": "invalid_value"},
        }
        out = summarize_flow_step(resp)
        assert "Errors" in out

    def test_no_step_id_uses_type(self):
        resp = {"type": "create_entry"}
        out = summarize_flow_step(resp)
        assert "Type: create_entry" in out

    def test_optional_field(self):
        resp = {
            "type": "form",
            "step_id": "opts",
            "data_schema": [{"name": "hours", "required": False}],
        }
        out = summarize_flow_step(resp)
        assert "optional" in out


# ---------------------------------------------------------------------------
# drive_config_flow
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


class TestDriveConfigFlowHappyPath:
    def test_single_form_step_then_create_entry(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "flow_id": "f1",
                    "type": "form",
                    "step_id": "user",
                    "data_schema": [],
                },
                "submit:f1": {
                    "type": "create_entry",
                    "result": {"entry_id": "entry-abc"},
                },
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={"user": {"key": "val"}}))
        assert result.success is True
        assert result.entry_ids == ["entry-abc"]
        assert result.abort_reason == ""

    def test_direct_create_entry_on_start(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "type": "create_entry",
                    "result": {"entry_id": "entry-xyz"},
                }
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={}))
        assert result.success is True
        assert result.entry_ids == ["entry-xyz"]

    def test_menu_then_form_then_create_entry(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "flow_id": "f2",
                    "type": "menu",
                    "step_id": "user",
                    "menu_options": ["location", "surf"],
                },
                "submit:f2": {
                    "flow_id": "f2",
                    "type": "form",
                    "step_id": "location",
                    "data_schema": [{"name": "latitude", "required": True}],
                },
            }
        )
        # The second submit uses flow_id="f2" again, so we pre-load that key too
        rest._flow_responses["submit:f2"] = {
            "flow_id": "f2",
            "type": "form",
            "step_id": "location",
            "data_schema": [],
        }
        # Override: first submit returns form, second returns create_entry
        call_count = [0]
        original_submit = rest.submit_config_flow

        async def patched_submit(flow_id, data):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "flow_id": flow_id,
                    "type": "form",
                    "step_id": "location",
                    "data_schema": [],
                }
            return {"type": "create_entry", "result": {"entry_id": "entry-123"}}

        rest.submit_config_flow = patched_submit  # type: ignore[method-assign]

        result = _run(
            drive_config_flow(
                rest,
                "mydom",
                plan={
                    "user": {"menu_choice": "location"},
                    "location": {"latitude": 42.6},
                },
            )
        )
        assert result.success is True
        assert result.entry_ids == ["entry-123"]

    def test_wildcard_plan_key_used_when_step_not_in_plan(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "flow_id": "fw",
                    "type": "form",
                    "step_id": "user",
                    "data_schema": [],
                },
                "submit:fw": {
                    "type": "create_entry",
                    "result": {"entry_id": "e-wildcard"},
                },
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={"*": {}}))
        assert result.success is True
        assert result.entry_ids == ["e-wildcard"]

    def test_empty_entry_id_list_when_result_missing(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {"type": "create_entry", "result": {}},
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={}))
        assert result.success is True
        assert result.entry_ids == []


class TestDriveConfigFlowAbortPaths:
    def test_abort_from_ha_on_start(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {"type": "abort", "reason": "already_configured"},
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={}))
        assert result.success is False
        assert result.abort_reason == "already_configured"

    def test_no_plan_for_step_aborts_flow(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "flow_id": "fa",
                    "type": "form",
                    "step_id": "unknown_step",
                    "data_schema": [],
                },
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={}))
        assert result.success is False
        assert result.abort_reason == "no_plan"
        # abort DELETE was called
        assert "/api/config/config_entries/flow/fa" in rest.deleted

    def test_step_errors_abort_flow(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "flow_id": "fe",
                    "type": "form",
                    "step_id": "user",
                    "data_schema": [],
                },
                "submit:fe": {
                    "type": "form",
                    "step_id": "user",
                    "errors": {"base": "invalid"},
                    "data_schema": [],
                },
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={"user": {}}))
        assert result.success is False
        assert result.abort_reason == "step_errors"
        assert "/api/config/config_entries/flow/fe" in rest.deleted

    def test_unexpected_flow_type_returns_error(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {"flow_id": "fx", "type": "unknown_type"},
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={}))
        assert result.success is False
        assert "Unexpected flow type" in result.error_message

    def test_max_steps_exceeded_aborts(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": {
                    "flow_id": "fm",
                    "type": "form",
                    "step_id": "loop",
                    "data_schema": [],
                },
                "submit:fm": {
                    "flow_id": "fm",
                    "type": "form",
                    "step_id": "loop",
                    "data_schema": [],
                },
            }
        )
        result = _run(drive_config_flow(rest, "mydom", plan={"loop": {}}, max_steps=3))
        assert result.success is False
        assert result.abort_reason == "max_steps"
        assert "/api/config/config_entries/flow/fm" in rest.deleted

    def test_exception_during_start_returns_error(self):
        import httpx

        rest = FakeHARestClient(
            flow_responses={
                "start:mydom": Exception("network error"),  # type: ignore[dict-item]
            }
        )

        async def broken_start(handler):
            raise httpx.ConnectError("network error")

        rest.start_config_flow = broken_start  # type: ignore[method-assign]
        result = _run(drive_config_flow(rest, "mydom", plan={}))
        assert result.success is False
        assert result.abort_reason == "exception"
        assert "network error" in result.error_message


class TestDriveConfigFlowInvalidHandler:
    def test_invalid_handler_retries_once(self):
        call_count = [0]

        async def patched_start(handler):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"type": "invalid_handler"}
            return {
                "flow_id": "f-retry",
                "type": "form",
                "step_id": "user",
                "data_schema": [],
            }

        rest = FakeHARestClient(
            flow_responses={
                "submit:f-retry": {
                    "type": "create_entry",
                    "result": {"entry_id": "e-retry"},
                }
            }
        )
        rest.start_config_flow = patched_start  # type: ignore[method-assign]
        result = _run(drive_config_flow(rest, "mydom", plan={"user": {}}))
        assert result.success is True
        assert call_count[0] == 2


# ---------------------------------------------------------------------------
# FlowResult NamedTuple contract
# ---------------------------------------------------------------------------


class TestFlowResult:
    def test_valid_construction(self):
        r = FlowResult(
            success=True,
            entry_ids=["e1"],
            abort_reason="",
            error_message="",
        )
        assert r.success is True
        assert r.entry_ids == ["e1"]

    def test_failure_fields(self):
        r = FlowResult(
            success=False,
            entry_ids=[],
            abort_reason="already_configured",
            error_message="Flow aborted: already_configured",
        )
        assert r.success is False
        assert r.abort_reason == "already_configured"

    def test_json_round_trip(self):
        import json

        r = FlowResult(
            success=False,
            entry_ids=[],
            abort_reason="no_plan",
            error_message="No answer",
        )
        d = r._asdict()
        assert json.loads(json.dumps(d)) == d


# ---------------------------------------------------------------------------
# drive_repair_flow
# ---------------------------------------------------------------------------


class TestDriveRepairFlowHappyPath:
    def test_single_confirm_step_succeeds(self):
        """Typical Spook repair: start returns form, submit returns create_entry."""
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:empty_areas/areas_1": {
                    "flow_id": "rf-1",
                    "type": "form",
                    "step_id": "confirm",
                },
                "repair_submit:rf-1": {"type": "create_entry"},
            }
        )
        result = _run(drive_repair_flow(fake, "spook", "empty_areas/areas_1"))
        assert result.success is True
        assert result.abort_reason == ""
        assert result.error_message == ""
        # start + submit
        assert any("/api/repairs/issues/fix" in p for p, _ in fake.posted)
        assert any("/api/repairs/issues/fix/rf-1" in p for p, _ in fake.posted)

    def test_immediate_create_entry_on_start(self):
        """Flow that resolves immediately without a confirm step."""
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss1": {"flow_id": "rf-2", "type": "create_entry"},
            }
        )
        result = _run(drive_repair_flow(fake, "spook", "iss1"))
        assert result.success is True

    def test_abort_on_start(self):
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss2": {
                    "flow_id": "rf-3",
                    "type": "abort",
                    "reason": "already_fixed",
                },
            }
        )
        result = _run(drive_repair_flow(fake, "spook", "iss2"))
        assert result.success is False
        assert result.abort_reason == "already_fixed"


class TestDriveRepairFlowAbortPaths:
    def test_step_errors_causes_abort(self):
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss3": {
                    "flow_id": "rf-4",
                    "type": "form",
                    "step_id": "confirm",
                },
                "repair_submit:rf-4": {
                    "type": "form",
                    "step_id": "confirm",
                    "errors": {"base": "unknown_error"},
                },
            }
        )
        result = _run(drive_repair_flow(fake, "spook", "iss3"))
        assert result.success is False
        assert result.abort_reason == "step_errors"
        # abort_repair_flow should be called
        assert any("/api/repairs/issues/fix/rf-4" in p for p in fake.deleted)

    def test_unexpected_type_causes_failure(self):
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss4": {
                    "flow_id": "rf-5",
                    "type": "external",
                },
            }
        )
        result = _run(drive_repair_flow(fake, "spook", "iss4"))
        assert result.success is False
        assert "external" in result.error_message

    def test_exception_aborts_and_returns_failure(self, caplog):
        class _RaisingClient(FakeHARestClient):
            async def start_repair_flow(self, domain: str, issue_id: str) -> dict:
                raise RuntimeError("connection refused")

        with caplog.at_level(logging.WARNING, logger="config_flow_driver"):
            result = _run(drive_repair_flow(_RaisingClient(), "spook", "iss5"))
        assert result.success is False
        assert "connection refused" in result.error_message
        assert "connection refused" in caplog.text


class TestDriveRepairFlowMenuStep:
    """drive_repair_flow handles menu steps correctly (Spook registry cleanups)."""

    def test_menu_valid_choice_reaches_create_entry(self):
        """A valid menu_choice is submitted as next_step_id and flow completes."""
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:empty_areas/iss1": {
                    "flow_id": "rf-menu-1",
                    "type": "menu",
                    "step_id": "init",
                    "menu_options": ["remove", "manage", "ignore"],
                },
                "repair_submit:rf-menu-1": {"type": "create_entry"},
            }
        )
        result = _run(
            drive_repair_flow(fake, "spook", "empty_areas/iss1", menu_choice="remove")
        )
        assert result.success is True
        # Exactly one submission must have been made with the menu choice.
        menu_submits = [d for _, d in fake.posted if d == {"next_step_id": "remove"}]
        assert len(menu_submits) == 1

    def test_menu_no_choice_aborts_without_looping(self):
        """When menu_choice is None the flow is aborted immediately; {} is never submitted."""
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss_menu_none": {
                    "flow_id": "rf-menu-none",
                    "type": "menu",
                    "step_id": "init",
                    "menu_options": ["remove", "manage"],
                },
            }
        )
        result = _run(drive_repair_flow(fake, "spook", "iss_menu_none"))
        assert result.success is False
        assert result.abort_reason == "menu_unhandled"
        # Empty dict {} must NOT have been submitted — that is the looping bug.
        empty_submits = [d for _, d in fake.posted if d == {}]
        assert empty_submits == []
        # Flow should have been aborted via DELETE.
        assert any("rf-menu-none" in p for p in fake.deleted)

    def test_menu_invalid_choice_aborts(self):
        """A menu_choice not in menu_options aborts the flow."""
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss_bad_choice": {
                    "flow_id": "rf-menu-bad",
                    "type": "menu",
                    "step_id": "init",
                    "menu_options": ["remove", "manage"],
                },
            }
        )
        result = _run(
            drive_repair_flow(fake, "spook", "iss_bad_choice", menu_choice="delete")
        )
        assert result.success is False
        assert result.abort_reason == "menu_unhandled"
        assert "delete" in result.error_message
        assert any("rf-menu-bad" in p for p in fake.deleted)

    def test_menu_abort_changed_after_remove(self):
        """Flow aborts with 'changed' reason if the item was modified since the issue was raised."""
        fake = FakeHARestClient(
            flow_responses={
                "repair_start:spook:iss_changed": {
                    "flow_id": "rf-menu-changed",
                    "type": "menu",
                    "step_id": "init",
                    "menu_options": ["remove", "manage"],
                },
                "repair_submit:rf-menu-changed": {
                    "type": "abort",
                    "reason": "changed",
                },
            }
        )
        result = _run(
            drive_repair_flow(fake, "spook", "iss_changed", menu_choice="remove")
        )
        assert result.success is False
        assert result.abort_reason == "changed"


class TestIgnoreRepairIssueWS:
    """Test FakeHAWebSocketClient.ignore_repair_issue stubs."""

    def test_records_call(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient()
        _run(fake.ignore_repair_issue("spook", "empty_areas/areas_1", ignore=True))
        assert ("spook", "empty_areas/areas_1", True) in fake.ignored_repairs
        assert any("ignore_repair_issue" in c for c in fake.calls)

    def test_unignore_records_false(self):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient

        fake = FakeHAWebSocketClient()
        _run(fake.ignore_repair_issue("spook", "iss_x", ignore=False))
        assert ("spook", "iss_x", False) in fake.ignored_repairs
