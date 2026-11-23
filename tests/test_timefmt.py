"""Tests for utils/core/timefmt.py and the local-time template rule (#1278)."""

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from utils.core.timefmt import time_markup, to_utc_iso

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
