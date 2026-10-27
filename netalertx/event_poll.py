"""NetAlertX REST event poll — periodic fetch of /events with high-water mark.

Reads /events since the last successful poll (stored as an epoch float in
user_prefs key ``netalertx:event_hwm``), routes new-device events to
NetAlertXEventRouter, and updates the mark.  Device-online/offline transitions
are logged as context only; device-offline *alerting* lives in ADR 047 with
NetAlertX as presence evidence (ADR 048), not here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.event_router import NetAlertXEventRouter

log = get_logger("netalertx.event_poll")

_HWM_PREF_KEY = "netalertx:event_hwm"


def _read_hwm(db_path: str) -> float:
    """Return high-water mark as epoch float, or 0.0 if not yet set."""
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?",
                (_HWM_PREF_KEY,),
            ).fetchone()
            return float(row[0]) if row else 0.0
    except Exception:  # nosec B110 — table may not exist yet or value corrupt
        return 0.0


def _write_hwm(db_path: str, ts: float) -> None:
    """Persist the new high-water mark epoch to user_prefs."""
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_HWM_PREF_KEY, str(ts)),
            )
            conn.commit()
    except Exception as exc:  # nosec B110
        log.warning("netalertx_event_hwm_write_failed", error=str(exc))


def _parse_event_ts(event: dict) -> float:
    """Parse eveDateTime to epoch float; return 0.0 if absent or unparseable."""
    raw = event.get("eveDateTime", "")
    if not raw:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return 0.0


async def poll_once(
    api_client: "NetAlertXAPIClient",
    router: "NetAlertXEventRouter",
    db_path: str,
) -> int:
    """Fetch events newer than the stored high-water mark and route them.

    Returns the number of events routed.
    """
    if not api_client.has_token:
        log.debug("netalertx_event_poll_no_token")
        return 0
    hwm = await asyncio.to_thread(_read_hwm, db_path)
    events = await api_client.get_events()

    new_hwm = hwm
    routed = 0
    for event in events:
        ts = _parse_event_ts(event)
        if ts <= hwm:
            continue
        if ts > new_hwm:
            new_hwm = ts
        await router.route_rest_event(event)
        routed += 1

    if new_hwm > hwm:
        await asyncio.to_thread(_write_hwm, db_path, new_hwm)

    if routed:
        log.info("netalertx_event_poll_complete", routed=routed)
    else:
        log.debug("netalertx_event_poll_no_new")

    return routed


async def run_event_poll(
    api_client: "NetAlertXAPIClient",
    router: "NetAlertXEventRouter",
    db_path: str,
    interval_minutes: int,
) -> None:
    """Continuous poll loop — fetch /events on each interval until cancelled."""
    while True:
        routed = 0
        try:
            routed = await poll_once(api_client, router, db_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("netalertx_event_poll_error", error=str(exc))
        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv = _get_sv()
            if _sv is not None:
                _sv.touch(
                    "netalertx_event_poll",
                    outcome=f"{routed} event(s) routed" if routed else "No new events",
                )
        except Exception:  # nosec B110
            pass
        await asyncio.sleep(interval_minutes * 60)
