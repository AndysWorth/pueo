"""Tests for utils/ha/upgrade_advisor.py."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

from utils.ha.upgrade_advisor import (
    AdvisorReport,
    PostUpgradeReport,
    _DEFAULT_RISK_ENTITY,
    _DEFAULT_STATUS_ENTITY,
    _MAX_REPORT_CHARS,
    _MAX_REGRESSIONS,
    _extract_component_section,
    _parse_iso_timestamp,
    _resolve_entity_ids,
    read_advisor_report,
    read_post_upgrade_report,
    request_advisor_analysis,
)


# ---------------------------------------------------------------------------
# AdvisorReport dataclass
# ---------------------------------------------------------------------------


class TestAdvisorReport:
    def test_valid_construction(self):
        r = AdvisorReport(
            status="report_ready",
            risk="medium",
            breaking_change_count=3,
            report="Some report text.",
            available_version="2026.10.0",
        )
        assert r.status == "report_ready"
        assert r.risk == "medium"
        assert r.breaking_change_count == 3

    def test_json_round_trip(self):
        r = AdvisorReport(
            status="report_ready",
            risk="high",
            breaking_change_count=1,
            report="Report.",
            available_version="2026.11.0",
        )
        data = asdict(r)
        r2 = AdvisorReport(**data)
        assert r2 == r


# ---------------------------------------------------------------------------
# Fake REST client for testing
# ---------------------------------------------------------------------------


class _FakeRest:
    """Minimal REST fake that returns pre-scripted states."""

    def __init__(
        self,
        status_entity: dict | None = None,
        risk_entity: dict | None = None,
        status_raises: Exception | None = None,
        risk_raises: Exception | None = None,
        call_service_raises: Exception | None = None,
        # sequence of states returned by consecutive get_state calls for the status sensor
        status_sequence: list[dict] | None = None,
    ) -> None:
        self._status = status_entity
        self._risk = risk_entity
        self._status_raises = status_raises
        self._risk_raises = risk_raises
        self._call_service_raises = call_service_raises
        self._status_sequence = list(status_sequence) if status_sequence else None
        self._status_call_count = 0
        self.service_calls: list[tuple[str, str, dict]] = []

    async def get_state(self, entity_id: str) -> dict:
        # Support any resolved entity ID (default or custom)
        if "status" in entity_id:
            if self._status_raises is not None:
                raise self._status_raises
            if self._status_sequence is not None:
                idx = min(self._status_call_count, len(self._status_sequence) - 1)
                self._status_call_count += 1
                return self._status_sequence[idx]
            if self._status is None:
                raise RuntimeError("entity not found")
            return self._status
        if "risk" in entity_id:
            if self._risk_raises is not None:
                raise self._risk_raises
            if self._risk is None:
                raise RuntimeError("entity not found")
            return self._risk
        raise RuntimeError(f"Unknown entity: {entity_id}")

    async def call_service(self, domain: str, service: str, payload: dict) -> dict:
        if self._call_service_raises is not None:
            raise self._call_service_raises
        self.service_calls.append((domain, service, payload))
        return {}


class _FakeWs:
    """Minimal WS fake that returns a pre-scripted entity registry."""

    def __init__(
        self, entries: list[dict] | None = None, raises: Exception | None = None
    ):
        self._entries = entries or []
        self._raises = raises

    async def get_entity_registry(self) -> list[dict]:
        if self._raises is not None:
            raise self._raises
        return list(self._entries)


def _make_status(
    state: str = "report_ready",
    available_version: str = "2026.10.0",
    breaking_change_count: int = 2,
    report: str = "Some findings.",
) -> dict:
    return {
        "state": state,
        "attributes": {
            "available_version": available_version,
            "breaking_change_count": breaking_change_count,
            "report": report,
        },
    }


def _make_risk(state: str = "medium") -> dict:
    return {"state": state, "attributes": {}}


# ---------------------------------------------------------------------------
# read_advisor_report
# ---------------------------------------------------------------------------


class TestReadAdvisorReport:
    def test_happy_path_returns_report(self):
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.10.0"),
            risk_entity=_make_risk("medium"),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is not None
        assert result.status == "report_ready"
        assert result.risk == "medium"
        assert result.breaking_change_count == 2
        assert result.available_version == "2026.10.0"

    def test_not_installed_returns_none(self):
        """get_state raises → integration not installed → return None."""
        rest = _FakeRest(status_raises=RuntimeError("entity not found"))
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is None

    def test_state_not_report_ready_returns_none(self):
        rest = _FakeRest(
            status_entity=_make_status(state="analyzing"),
            risk_entity=_make_risk(),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is None

    def test_stale_version_returns_none(self):
        """Report for a different version than target → None."""
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.9.0"),
            risk_entity=_make_risk(),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is None

    def test_matching_version_is_accepted(self):
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.11.0"),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.11.0"))
        assert result is not None
        assert result.risk == "low"

    def test_risk_sensor_absent_uses_unknown(self):
        """Missing risk sensor → risk == 'unknown', report still returned."""
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.10.0"),
            risk_raises=RuntimeError("not found"),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is not None
        assert result.risk == "unknown"

    def test_long_report_is_truncated(self):
        long_report = "x" * (_MAX_REPORT_CHARS + 500)
        rest = _FakeRest(
            status_entity=_make_status(
                available_version="2026.10.0", report=long_report
            ),
            risk_entity=_make_risk(),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is not None
        assert len(result.report) <= _MAX_REPORT_CHARS + len("...[truncated]")
        assert result.report.endswith("...[truncated]")

    def test_breaking_change_count_non_int_defaults_to_zero(self):
        status = _make_status(available_version="2026.10.0")
        status["attributes"]["breaking_change_count"] = "not-a-number"
        rest = _FakeRest(status_entity=status, risk_entity=_make_risk())
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is not None
        assert result.breaking_change_count == 0

    def test_zero_breaking_changes_is_valid(self):
        rest = _FakeRest(
            status_entity=_make_status(
                available_version="2026.10.0", breaking_change_count=0
            ),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is not None
        assert result.breaking_change_count == 0


# ---------------------------------------------------------------------------
# PostUpgradeReport dataclass
# ---------------------------------------------------------------------------


class TestPostUpgradeReport:
    def test_valid_construction(self):
        r = PostUpgradeReport(
            post_upgrade_status="regressions",
            regressions=["automation.foo broke", "light.bar unavailable"],
        )
        assert r.post_upgrade_status == "regressions"
        assert len(r.regressions) == 2

    def test_default_empty_regressions(self):
        r = PostUpgradeReport(post_upgrade_status="ok")
        assert r.regressions == []

    def test_json_round_trip(self):
        r = PostUpgradeReport(
            post_upgrade_status="ok",
            regressions=["issue1"],
        )
        data = asdict(r)
        r2 = PostUpgradeReport(**data)
        assert r2 == r


# ---------------------------------------------------------------------------
# read_post_upgrade_report
# ---------------------------------------------------------------------------


def _make_post_status(
    post_upgrade_status: str = "ok",
    post_upgrade_regressions=None,
) -> dict:
    attrs: dict = {"post_upgrade_status": post_upgrade_status}
    if post_upgrade_regressions is not None:
        attrs["post_upgrade_regressions"] = post_upgrade_regressions
    return {"state": "report_ready", "attributes": attrs}


class TestReadPostUpgradeReport:
    def test_not_installed_returns_none(self):
        rest = _FakeRest(status_raises=RuntimeError("entity not found"))
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is None

    def test_no_post_upgrade_attributes_returns_none(self):
        """Sensor present but no post_upgrade_* attrs → return None."""
        rest = _FakeRest(status_entity={"state": "idle", "attributes": {}})
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is None

    def test_happy_path_ok_status(self):
        rest = _FakeRest(status_entity=_make_post_status("ok"))
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.post_upgrade_status == "ok"
        assert result.regressions == []

    def test_regressions_list(self):
        rest = _FakeRest(
            status_entity=_make_post_status(
                "regressions",
                post_upgrade_regressions=["automation.foo", "light.bar"],
            )
        )
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.regressions == ["automation.foo", "light.bar"]

    def test_regressions_string_coerced_to_list(self):
        rest = _FakeRest(
            status_entity=_make_post_status(
                "regressions",
                post_upgrade_regressions="automation.foo",
            )
        )
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.regressions == ["automation.foo"]

    def test_regressions_capped_at_max(self):
        many = [f"issue_{i}" for i in range(_MAX_REGRESSIONS + 5)]
        rest = _FakeRest(status_entity=_make_post_status("regressions", many))
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert len(result.regressions) == _MAX_REGRESSIONS

    def test_empty_string_status_with_regressions_returns_report(self):
        """post_upgrade_status can be empty if regressions list is present."""
        rest = _FakeRest(status_entity=_make_post_status("", ["automation.foo"]))
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.regressions == ["automation.foo"]

    def test_empty_regressions_string_not_added(self):
        """An empty-string regression is not added to the list."""
        rest = _FakeRest(
            status_entity=_make_post_status("ok", post_upgrade_regressions="")
        )
        result = asyncio.run(read_post_upgrade_report(rest))
        # post_upgrade_status="ok" → post_status is truthy → report is returned
        assert result is not None
        assert result.regressions == []


# ---------------------------------------------------------------------------
# _resolve_entity_ids
# ---------------------------------------------------------------------------


class TestResolveEntityIds:
    def test_resolves_from_registry(self):
        ws = _FakeWs(
            entries=[
                {
                    "platform": "upgrade_advisor",
                    "unique_id": "ha_advisor_status",
                    "entity_id": "sensor.my_advisor_status",
                },
                {
                    "platform": "upgrade_advisor",
                    "unique_id": "ha_advisor_risk_level",
                    "entity_id": "sensor.my_advisor_risk_level",
                },
            ]
        )
        status_id, risk_id = asyncio.run(_resolve_entity_ids(ws))
        assert status_id == "sensor.my_advisor_status"
        assert risk_id == "sensor.my_advisor_risk_level"

    def test_falls_back_to_defaults_when_registry_raises(self):
        ws = _FakeWs(raises=RuntimeError("ws unavailable"))
        status_id, risk_id = asyncio.run(_resolve_entity_ids(ws))
        assert status_id == _DEFAULT_STATUS_ENTITY
        assert risk_id == _DEFAULT_RISK_ENTITY

    def test_falls_back_to_defaults_when_entities_absent(self):
        ws = _FakeWs(
            entries=[{"platform": "other", "unique_id": "x", "entity_id": "sensor.x"}]
        )
        status_id, risk_id = asyncio.run(_resolve_entity_ids(ws))
        assert status_id == _DEFAULT_STATUS_ENTITY
        assert risk_id == _DEFAULT_RISK_ENTITY

    def test_partial_fallback_missing_risk(self):
        ws = _FakeWs(
            entries=[
                {
                    "platform": "upgrade_advisor",
                    "unique_id": "ha_advisor_status",
                    "entity_id": "sensor.custom_status",
                },
            ]
        )
        status_id, risk_id = asyncio.run(_resolve_entity_ids(ws))
        assert status_id == "sensor.custom_status"
        assert risk_id == _DEFAULT_RISK_ENTITY


# ---------------------------------------------------------------------------
# read_advisor_report — risk entity name fix
# ---------------------------------------------------------------------------


class TestReadAdvisorReportRiskEntityName:
    def test_default_risk_entity_uses_risk_level_suffix(self):
        """The default risk entity ID must end in _risk_level, not _risk."""
        assert _DEFAULT_RISK_ENTITY.endswith("_risk_level")
        assert "_risk_level" in _DEFAULT_RISK_ENTITY

    def test_reads_risk_from_resolved_entity(self):
        """When ws_client is provided, the resolved risk entity ID is used."""
        ws = _FakeWs(
            entries=[
                {
                    "platform": "upgrade_advisor",
                    "unique_id": "advisor_status",
                    "entity_id": "sensor.upgrade_advisor_status",
                },
                {
                    "platform": "upgrade_advisor",
                    "unique_id": "advisor_risk_level",
                    "entity_id": "sensor.upgrade_advisor_risk_level",
                },
            ]
        )
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.10.0"),
            risk_entity=_make_risk("high"),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0", ws_client=ws))
        assert result is not None
        assert result.risk == "high"

    def test_reads_risk_from_default_entity_when_no_ws(self):
        """Without ws_client, the default entity IDs are used (which include _risk_level)."""
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.10.0"),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is not None
        assert result.risk == "low"


# ---------------------------------------------------------------------------
# request_advisor_analysis
# ---------------------------------------------------------------------------


def _make_analyzing() -> dict:
    return {"state": "analyzing", "attributes": {}}


def _make_report_ready() -> dict:
    return {"state": "report_ready", "attributes": {}}


class TestRequestAdvisorAnalysis:
    def test_core_uses_analyze_version_service(self):
        rest = _FakeRest(
            status_sequence=[_make_analyzing(), _make_report_ready()],
        )
        asyncio.run(
            request_advisor_analysis(
                rest, "2026.10.0", "homeassistant", timeout_seconds=30
            )
        )
        assert len(rest.service_calls) == 1
        domain, service, payload = rest.service_calls[0]
        assert domain == "upgrade_advisor"
        assert service == "analyze_version"
        assert payload == {"version": "2026.10.0"}

    def test_core_alias_uses_analyze_version(self):
        rest = _FakeRest(status_sequence=[_make_report_ready()])
        asyncio.run(
            request_advisor_analysis(rest, "2026.10.0", "core", timeout_seconds=30)
        )
        assert rest.service_calls[0][1] == "analyze_version"

    def test_hacs_component_uses_analyze_service(self):
        rest = _FakeRest(status_sequence=[_make_report_ready()])
        asyncio.run(
            request_advisor_analysis(
                rest, "1.2.3", "my_custom_component", timeout_seconds=30
            )
        )
        assert rest.service_calls[0][1] == "analyze"
        assert rest.service_calls[0][2] == {}

    def test_supervisor_is_skipped(self):
        """Supervisor updates are not supported by the advisor."""
        rest = _FakeRest(status_entity=_make_analyzing())
        asyncio.run(
            request_advisor_analysis(
                rest, "2024.08.0", "supervisor", timeout_seconds=30
            )
        )
        assert rest.service_calls == []

    def test_os_is_skipped(self):
        rest = _FakeRest(status_entity=_make_analyzing())
        asyncio.run(request_advisor_analysis(rest, "14.1", "os", timeout_seconds=30))
        assert rest.service_calls == []

    def test_service_call_failure_is_silent(self):
        """A service call failure should not raise — proceed gracefully."""
        rest = _FakeRest(call_service_raises=RuntimeError("service unavailable"))
        # Should not raise
        asyncio.run(
            request_advisor_analysis(
                rest, "2026.10.0", "homeassistant", timeout_seconds=30
            )
        )

    def test_poll_returns_after_report_ready(self):
        """Polling stops as soon as the state transitions out of 'analyzing'."""
        rest = _FakeRest(
            status_sequence=[
                _make_analyzing(),
                _make_analyzing(),
                _make_report_ready(),
            ]
        )
        with patch("utils.ha.upgrade_advisor.asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(
                request_advisor_analysis(
                    rest, "2026.10.0", "homeassistant", timeout_seconds=60
                )
            )
        # Service call + 3 polls
        assert rest._status_call_count == 3

    def test_timeout_does_not_raise(self):
        """On timeout, the function returns without error."""
        rest = _FakeRest(
            status_sequence=[_make_analyzing()],  # never leaves analyzing
        )
        with patch("utils.ha.upgrade_advisor.asyncio.sleep", new_callable=AsyncMock):
            with patch("utils.ha.upgrade_advisor.asyncio.get_event_loop") as mock_loop:
                # Simulate instant timeout: deadline is already in the past
                mock_loop.return_value.time.side_effect = [0.0, 999.0]
                asyncio.run(
                    request_advisor_analysis(
                        rest, "2026.10.0", "homeassistant", timeout_seconds=1
                    )
                )
        # Should have called the service and not raised
        assert len(rest.service_calls) == 1

    def test_ws_client_used_for_entity_resolution(self):
        """When ws_client is provided, the resolved entity ID is used for polling."""
        ws = _FakeWs(
            entries=[
                {
                    "platform": "upgrade_advisor",
                    "unique_id": "custom_status",
                    "entity_id": "sensor.custom_advisor_status",
                },
            ]
        )
        rest = _FakeRest(status_sequence=[_make_report_ready()])
        asyncio.run(
            request_advisor_analysis(
                rest, "2026.10.0", "homeassistant", ws_client=ws, timeout_seconds=30
            )
        )
        # If resolution succeeded, the poll used the custom entity ID
        # (get_state called with "sensor.custom_advisor_status", which contains "status")
        assert rest._status_call_count >= 1

    def test_default_timeout_is_1800(self):
        """Default timeout must be 1800 s (stuck-only ceiling, ADR 022)."""
        import inspect

        sig = inspect.signature(request_advisor_analysis)
        assert sig.parameters["timeout_seconds"].default == 1800.0


# ---------------------------------------------------------------------------
# _parse_iso_timestamp
# ---------------------------------------------------------------------------


class TestParseIsoTimestamp:
    def test_parses_utc_string(self):
        ts = _parse_iso_timestamp("2026-10-16T12:00:00+00:00")
        assert ts is not None
        assert ts > 0.0

    def test_parses_naive_string_as_utc(self):
        ts_naive = _parse_iso_timestamp("2026-10-16T12:00:00")
        ts_utc = _parse_iso_timestamp("2026-10-16T12:00:00+00:00")
        assert ts_naive == ts_utc

    def test_returns_none_on_invalid(self):
        assert _parse_iso_timestamp("not-a-date") is None
        assert _parse_iso_timestamp("") is None


# ---------------------------------------------------------------------------
# _extract_component_section
# ---------------------------------------------------------------------------


class TestExtractComponentSection:
    def test_extracts_matching_section(self):
        report = "## homeassistant\nCore change.\n## my_component\nCustom change.\n## other\nOther."
        result = _extract_component_section(report, "my_component")
        assert "Custom change." in result
        assert "Core change." not in result
        assert "Other." not in result

    def test_returns_full_report_when_no_match(self):
        report = "## homeassistant\nCore change."
        result = _extract_component_section(report, "missing_component")
        assert result == report

    def test_case_insensitive_match(self):
        report = "## My_Component\nFound it."
        result = _extract_component_section(report, "my_component")
        assert "Found it." in result

    def test_subsection_stops_at_next_same_level_heading(self):
        report = "## comp_a\nSection A.\n## comp_b\nSection B."
        result = _extract_component_section(report, "comp_a")
        assert "Section A." in result
        assert "Section B." not in result

    def test_deeper_heading_not_treated_as_section_boundary(self):
        report = "## comp_a\n### details\nDetail text.\n## comp_b\nSection B."
        result = _extract_component_section(report, "comp_a")
        assert "Detail text." in result
        assert "Section B." not in result


# ---------------------------------------------------------------------------
# read_advisor_report — HACS component matching (Fix 3)
# ---------------------------------------------------------------------------


class TestReadAdvisorReportHacsMatching:
    """For non-Core component_type, version check is replaced by last_analysis freshness."""

    def _make_hacs_status(
        self,
        state: str = "report_ready",
        available_version: str = "2026.10.0",
        last_analysis: str = "",
        report: str = "## my_hacs_comp\nNo breaking changes.",
    ) -> dict:
        attrs: dict = {
            "available_version": available_version,
            "breaking_change_count": 0,
            "report": report,
        }
        if last_analysis:
            attrs["last_analysis"] = last_analysis
        return {"state": state, "attributes": attrs}

    def test_hacs_component_skips_version_check(self):
        """Non-Core: available_version mismatch is ignored."""
        rest = _FakeRest(
            status_entity=self._make_hacs_status(available_version="2026.10.0"),
            risk_entity=_make_risk("low"),
        )
        # target_version is a HACS component version, not Core
        result = asyncio.run(
            read_advisor_report(rest, "1.2.3", component_type="my_hacs_comp")
        )
        assert result is not None

    def test_hacs_fresh_last_analysis_accepted(self):
        """Non-Core: last_analysis after trigger_time → report accepted."""
        import time

        trigger = time.time() - 10  # triggered 10 s ago
        last_analysis = "2099-01-01T00:00:00+00:00"  # far future = fresh
        rest = _FakeRest(
            status_entity=self._make_hacs_status(last_analysis=last_analysis),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(
            read_advisor_report(
                rest,
                "1.2.3",
                component_type="my_hacs_comp",
                trigger_time=trigger,
            )
        )
        assert result is not None

    def test_hacs_stale_last_analysis_rejected(self):
        """Non-Core: last_analysis before trigger_time → report rejected (stale)."""
        trigger = 9999999999.0  # far future trigger
        last_analysis = "2000-01-01T00:00:00+00:00"  # clearly before trigger
        rest = _FakeRest(
            status_entity=self._make_hacs_status(last_analysis=last_analysis),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(
            read_advisor_report(
                rest,
                "1.2.3",
                component_type="my_hacs_comp",
                trigger_time=trigger,
            )
        )
        assert result is None

    def test_hacs_absent_last_analysis_accepted(self):
        """Non-Core: last_analysis attribute absent → gracefully accept the report."""
        trigger = 9999999999.0  # far future trigger, but no attr to check
        rest = _FakeRest(
            status_entity=self._make_hacs_status(last_analysis=""),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(
            read_advisor_report(
                rest,
                "1.2.3",
                component_type="my_hacs_comp",
                trigger_time=trigger,
            )
        )
        assert result is not None

    def test_hacs_no_trigger_time_accepted(self):
        """Non-Core: no trigger_time provided → accept any ready report."""
        rest = _FakeRest(
            status_entity=self._make_hacs_status(
                last_analysis="2000-01-01T00:00:00+00:00"
            ),
            risk_entity=_make_risk("medium"),
        )
        result = asyncio.run(
            read_advisor_report(rest, "1.2.3", component_type="my_hacs_comp")
        )
        assert result is not None

    def test_hacs_section_extracted_from_report(self):
        """Non-Core: the component's section is extracted from the full report."""
        report = (
            "## homeassistant\nCore changes.\n"
            "## my_hacs_comp\nNo breaking changes here.\n"
            "## other_comp\nSomething else."
        )
        rest = _FakeRest(
            status_entity=self._make_hacs_status(report=report),
            risk_entity=_make_risk("low"),
        )
        result = asyncio.run(
            read_advisor_report(rest, "1.2.3", component_type="my_hacs_comp")
        )
        assert result is not None
        assert "No breaking changes here." in result.report
        assert "Core changes." not in result.report

    def test_core_component_type_still_checks_version(self):
        """Core: available_version mismatch still rejects the report."""
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.9.0"),
            risk_entity=_make_risk(),
        )
        result = asyncio.run(
            read_advisor_report(rest, "2026.10.0", component_type="homeassistant")
        )
        assert result is None

    def test_empty_component_type_falls_back_to_core_logic(self):
        """Default empty component_type uses Core (available_version) check."""
        rest = _FakeRest(
            status_entity=_make_status(available_version="2026.9.0"),
            risk_entity=_make_risk(),
        )
        result = asyncio.run(read_advisor_report(rest, "2026.10.0"))
        assert result is None


# ---------------------------------------------------------------------------
# read_post_upgrade_report — freshness check (Fix 4)
# ---------------------------------------------------------------------------


class TestReadPostUpgradeReportFreshness:
    def _make_post_status_with_check(
        self,
        post_upgrade_status: str = "ok",
        last_post_upgrade_check: str = "",
    ) -> dict:
        attrs: dict = {"post_upgrade_status": post_upgrade_status}
        if last_post_upgrade_check:
            attrs["last_post_upgrade_check"] = last_post_upgrade_check
        return {"state": "report_ready", "attributes": attrs}

    def test_fresh_report_accepted(self):
        """last_post_upgrade_check after update_completed_at → report returned."""
        completed = 1000.0
        last_check = "2099-01-01T00:00:00+00:00"  # far future = fresh
        rest = _FakeRest(
            status_entity=self._make_post_status_with_check(
                last_post_upgrade_check=last_check
            )
        )
        result = asyncio.run(
            read_post_upgrade_report(rest, update_completed_at=completed)
        )
        assert result is not None
        assert result.post_upgrade_status == "ok"
        assert result.last_post_upgrade_check == last_check

    def test_stale_report_rejected(self):
        """last_post_upgrade_check before update_completed_at → None."""
        completed = 9999999999.0
        last_check = "2000-01-01T00:00:00+00:00"  # clearly before completed
        rest = _FakeRest(
            status_entity=self._make_post_status_with_check(
                last_post_upgrade_check=last_check
            )
        )
        result = asyncio.run(
            read_post_upgrade_report(rest, update_completed_at=completed)
        )
        assert result is None

    def test_absent_last_check_attribute_accepted(self):
        """last_post_upgrade_check absent → gracefully return report."""
        completed = 9999999999.0  # far future; no attr to compare
        rest = _FakeRest(
            status_entity=self._make_post_status_with_check()  # no last_check attr
        )
        result = asyncio.run(
            read_post_upgrade_report(rest, update_completed_at=completed)
        )
        assert result is not None

    def test_no_update_completed_at_skips_check(self):
        """No update_completed_at → freshness check skipped, report returned."""
        last_check = "2000-01-01T00:00:00+00:00"
        rest = _FakeRest(
            status_entity=self._make_post_status_with_check(
                last_post_upgrade_check=last_check
            )
        )
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.last_post_upgrade_check == last_check

    def test_last_post_upgrade_check_stored_in_result(self):
        """last_post_upgrade_check is populated on the returned dataclass."""
        last_check = "2026-10-16T12:00:00+00:00"
        rest = _FakeRest(
            status_entity=self._make_post_status_with_check(
                last_post_upgrade_check=last_check
            )
        )
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.last_post_upgrade_check == last_check

    def test_last_post_upgrade_check_none_when_absent(self):
        """When the attribute is absent, last_post_upgrade_check is None on the result."""
        rest = _FakeRest(status_entity=_make_post_status("ok"))
        result = asyncio.run(read_post_upgrade_report(rest))
        assert result is not None
        assert result.last_post_upgrade_check is None


# ---------------------------------------------------------------------------
# PostUpgradeReport — updated dataclass
# ---------------------------------------------------------------------------


class TestPostUpgradeReportUpdated:
    def test_last_post_upgrade_check_field_exists(self):
        r = PostUpgradeReport(post_upgrade_status="ok")
        assert hasattr(r, "last_post_upgrade_check")
        assert r.last_post_upgrade_check is None

    def test_json_round_trip_with_last_check(self):
        r = PostUpgradeReport(
            post_upgrade_status="ok",
            regressions=["a"],
            last_post_upgrade_check="2026-10-16T00:00:00+00:00",
        )
        data = asdict(r)
        r2 = PostUpgradeReport(**data)
        assert r2 == r
