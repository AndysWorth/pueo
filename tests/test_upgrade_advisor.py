"""Tests for utils/ha/upgrade_advisor.py."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest

from utils.ha.upgrade_advisor import (
    AdvisorReport,
    _MAX_REPORT_CHARS,
    read_advisor_report,
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
    ) -> None:
        self._status = status_entity
        self._risk = risk_entity
        self._status_raises = status_raises
        self._risk_raises = risk_raises

    async def get_state(self, entity_id: str) -> dict:
        if entity_id == "sensor.upgrade_advisor_status":
            if self._status_raises is not None:
                raise self._status_raises
            if self._status is None:
                raise RuntimeError("entity not found")
            return self._status
        if entity_id == "sensor.upgrade_advisor_risk":
            if self._risk_raises is not None:
                raise self._risk_raises
            if self._risk is None:
                raise RuntimeError("entity not found")
            return self._risk
        raise RuntimeError(f"Unknown entity: {entity_id}")


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
