"""Normalize stored timestamps to a UTC ISO instant for local-time UI rendering.

The browser formats every absolute time in the viewer's timezone. The server
only emits a machine-readable UTC instant (``<time datetime="...Z">``), so the
display never depends on the Pueo host's (or Docker container's) timezone.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone


def to_utc_iso(value: object) -> str | None:
    """Return *value* as ``YYYY-MM-DDTHH:MM:SSZ``, or ``None`` if it is not a time.

    Accepts an epoch (int/float, or a numeric string), an ISO string with ``Z``
    or an offset, or a naive ``YYYY-MM-DD[ HH:MM[:SS]]`` string. Naive strings
    are UTC: every naive DB writer uses SQLite ``datetime('now')`` or
    ``datetime.utcnow()``.
    """
    if value is None or isinstance(value, bool):
        return None
    dt: datetime | None = None
    if isinstance(value, (int, float)):
        dt = _from_epoch(float(value))
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, str):
        dt = _from_string(value.strip())
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_epoch(ts: float) -> datetime | None:
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _from_string(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return _from_epoch(float(s))
    except ValueError:
        pass
    try:
        # 3.11+ fromisoformat parses Z, offsets, fractions, a space separator
        # and date-only strings.
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_STYLES = {"datetime": "%Y-%m-%d %H:%M:%S", "date": "%Y-%m-%d", "time": "%H:%M:%S"}


def time_markup(value: object, style: str = "datetime") -> str:
    """Return a ``<time class="pueo-ts">`` element for *value*, as HTML text.

    The element's text is a UTC fallback (``... UTC``) that the browser helper
    ``pueoLocalizeTimes`` (``web/static/pueo_time.js``) rewrites to local time.
    Empty input returns ``""``; input that is not a time returns it escaped.
    """
    if value is None or value == "" or value == 0:
        return ""
    iso = to_utc_iso(value)
    if iso is None:
        return html.escape(str(value))
    if style not in _STYLES:
        style = "datetime"
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    fallback = f"{dt.strftime(_STYLES[style])} UTC"
    return (
        f'<time class="pueo-ts" datetime="{iso}" data-fmt="{style}">'
        f"{fallback}</time>"
    )
