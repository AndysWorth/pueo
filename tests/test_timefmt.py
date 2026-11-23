"""Tests for utils/core/timefmt.py and the local-time template rule (#1278)."""

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from utils.core.timefmt import log_ts_epoch, time_markup, to_utc_iso

_TEMPLATES = Path(__file__).resolve().parent.parent / "web" / "templates"


class TestToUtcIso:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (1753123200, "2025-07-21T18:40:00Z"),
            (1753123200.75, "2025-07-21T18:40:00Z"),
            ("1753123200", "2025-07-21T18:40:00Z"),
            ("2025-07-21T18:40:00Z", "2025-07-21T18:40:00Z"),
            ("2025-07-21T18:40:00.123456Z", "2025-07-21T18:40:00Z"),
            ("2025-07-21T20:40:00+02:00", "2025-07-21T18:40:00Z"),
            ("2025-07-21 18:40:00", "2025-07-21T18:40:00Z"),
            ("2025-07-21 18:40", "2025-07-21T18:40:00Z"),
            ("2025-07-21", "2025-07-21T00:00:00Z"),
            ("  2025-07-21 18:40:00  ", "2025-07-21T18:40:00Z"),
            (datetime(2025, 7, 21, 18, 40), "2025-07-21T18:40:00Z"),
            (
                datetime(2025, 7, 21, 18, 40, tzinfo=timezone.utc),
                "2025-07-21T18:40:00Z",
            ),
        ],
    )
    def test_valid(self, value, expected):
        assert to_utc_iso(value) == expected

    @pytest.mark.parametrize(
        "value", [None, "", "   ", "garbage", 0, -5, True, 1e20, [1], "2025-13-40"]
    )
    def test_not_a_time(self, value):
        assert to_utc_iso(value) is None


class TestLogTsEpoch:
    _UTC = datetime(2026, 9, 3, 14, 22, 1, tzinfo=timezone.utc).timestamp()

    def test_z_suffix_is_utc(self):
        assert log_ts_epoch("2026-09-03 14:22:01Z") == self._UTC

    def test_offset_is_applied(self):
        assert log_ts_epoch("2026-09-03T19:22:01+05:00") == self._UTC
        assert log_ts_epoch("2026-09-03 09:22:01-0500") == self._UTC

    def test_nanosecond_fraction(self):
        assert log_ts_epoch("2026-09-03 14:22:01.123456789Z") == pytest.approx(
            self._UTC + 0.123456
        )

    def test_naive_is_server_local(self):
        assert (
            log_ts_epoch("2026-09-03 14:22:01.5")
            == datetime(2026, 9, 3, 14, 22, 1, 500000).timestamp()
        )

    def test_invalid_returns_none(self):
        assert log_ts_epoch("2026-13-45 99:99:99") is None
        assert log_ts_epoch("") is None


class TestTimeMarkup:
    def test_datetime_shape(self):
        assert time_markup(1753123200) == (
            '<time class="pueo-ts" datetime="2025-07-21T18:40:00Z" '
            'data-fmt="datetime">2025-07-21 18:40:00 UTC</time>'
        )

    @pytest.mark.parametrize(
        "style,fallback",
        [("date", "2025-07-21 UTC"), ("time", "18:40:00 UTC")],
    )
    def test_styles(self, style, fallback):
        out = time_markup(1753123200, style)
        assert f'data-fmt="{style}">{fallback}</time>' in out

    def test_unknown_style_falls_back_to_datetime(self):
        out = time_markup(1753123200, "bogus")
        assert 'data-fmt="datetime">2025-07-21 18:40:00 UTC<' in out

    @pytest.mark.parametrize("value", [None, "", 0])
    def test_empty(self, value):
        assert time_markup(value) == ""

    def test_unparseable_is_escaped(self):
        assert time_markup("<b>soon</b>") == "&lt;b&gt;soon&lt;/b&gt;"


class TestTemplatesUseLocaltime:
    """UI times go through ``| localtime`` / ``pueoFmt``, never server formatting."""

    def test_no_epoch_to_iso(self):
        hits = [
            p.name for p in _TEMPLATES.glob("*.html") if "epoch_to_iso" in p.read_text()
        ]
        assert hits == []

    def test_no_locale_js_formatters(self):
        pat = re.compile(r"toLocale(Date|Time)?String")
        hits = [p.name for p in _TEMPLATES.glob("*.html") if pat.search(p.read_text())]
        assert hits == []

    def test_no_sliced_or_literal_utc_times(self):
        """No ``*_at[:N]`` / ``last_seen[:N]`` slices and no literal " UTC" (#1278)."""
        slice_pat = re.compile(r"\b(\w+_at|last_seen)\[:\d+\]")
        utc_pat = re.compile(r"\}\}\s*UTC\b")
        hits = [
            p.name
            for p in _TEMPLATES.glob("*.html")
            if slice_pat.search(p.read_text()) or utc_pat.search(p.read_text())
        ]
        assert hits == []
