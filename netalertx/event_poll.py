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
from typing import TYPE_CHECKING, Optional

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.event_router import NetAlertXEventRouter
    from interfaces import SSHClientProtocol
    from utils.agent.autonomy import AutonomyGate
    from utils.hitl.notify import NotifierProtocol
    from utils.knowledge.knowledge_store import ChromaKnowledgeStore

log = get_logger("netalertx.event_poll")

_OUTAGE_CARD_KEY = "netalertx_unreachable"

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

    On the very first run (HWM == 0.0), seeds the mark to the newest event
    timestamp without routing anything, to prevent flooding cards for all
    historical events.

    Returns the number of events routed.
    """
    if not api_client.has_token:
        log.debug("netalertx_event_poll_no_token")
        return 0
    hwm = await asyncio.to_thread(_read_hwm, db_path)
    events = await api_client.get_events()

    # First-run seeding: set the HWM to the newest event and route nothing.
    if hwm == 0.0 and events:
        newest = max((_parse_event_ts(e) for e in events), default=0.0)
        if newest > 0.0:
            await asyncio.to_thread(_write_hwm, db_path, newest)
            log.info("netalertx_event_poll_first_run_seeded", newest_ts=newest)
        return 0

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


async def _send_outage_card(
    db_path: str,
    notifier: "NotifierProtocol",
    error_summary: str,
) -> None:
    """Send a deduped netalertx_unreachable card via the notifier."""
    import sqlite3

    from utils.hitl.card_types import CARD_TYPE_NETALERTX_HEAL
    from utils.hitl.hitl_tracker import mark_card_sent, should_send_card, stable_nid

    nid = stable_nid(_OUTAGE_CARD_KEY)
    subject = "Pueo: NetAlertX has been unreachable"
    body = f"NetAlertX API has not responded for multiple consecutive polls.\n\nLast error: {error_summary[:300]}"
    try:
        with sqlite3.connect(db_path) as conn:
            if should_send_card(conn, _OUTAGE_CARD_KEY):
                await notifier.send(
                    subject=subject,
                    body=body,
                    payload={
                        "notification_id": nid,
                        "card_type": CARD_TYPE_NETALERTX_HEAL,
                        "severity": "HIGH",
                        "episode_id": None,
                    },
                )
                mark_card_sent(
                    conn, _OUTAGE_CARD_KEY, CARD_TYPE_NETALERTX_HEAL, subject
                )
                log.info("netalertx_outage_card_sent", nid=nid)
    except Exception as exc:  # nosec B110
        log.error("netalertx_outage_card_failed", error=str(exc))


async def _resolve_outage_card(db_path: str) -> None:
    """Auto-resolve the outage card when the API comes back."""
    import sqlite3

    from utils.hitl.hitl_tracker import mark_card_resolved, stable_nid

    nid = stable_nid(_OUTAGE_CARD_KEY)
    try:
        with sqlite3.connect(db_path) as conn:
            mark_card_resolved(conn, _OUTAGE_CARD_KEY)
        log.info("netalertx_outage_card_resolved", nid=nid)
    except Exception as exc:  # nosec B110
        log.debug("netalertx_outage_card_resolve_failed", error=str(exc))


async def _submit_outage_diagnosis(
    ha_ssh_client: "SSHClientProtocol",
    llm_client: "object",
    knowledge_store: "Optional[ChromaKnowledgeStore]",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    slug: str,
) -> None:
    """Submit a one-off outage diagnosis WorkItem — deduped so only one runs at a time."""
    from utils.agent.work_queue import (
        PRIORITY_LOW,
        WorkItem,
        get_work_queue_or_none,
    )

    _wq = get_work_queue_or_none()
    if _wq is None:
        return

    _ha = ha_ssh_client
    _ks = knowledge_store
    _g = gate
    _n = notifier
    _lc = llm_client
    _s = slug

    async def _outage_diag_coro(
        ha: "SSHClientProtocol" = _ha,
        ks: "Optional[ChromaKnowledgeStore]" = _ks,
        g: "AutonomyGate" = _g,
        n: "NotifierProtocol" = _n,
        lc: "object" = _lc,
        s: str = _s,
    ) -> None:
        from netalertx.one_shot_diagnose import _diagnose_unreachable

        await _diagnose_unreachable(
            Exception("API unreachable — detected by event poll"),
            ha,
            s,
            lc,  # type: ignore[arg-type]
            ks,
            g,
            n,
        )

    await _wq.submit(
        WorkItem(
            priority=PRIORITY_LOW,
            activity_type="netalertx_outage_diagnosis",
            description="NetAlertX outage diagnosis",
            dedup_key="netalertx_outage_diag",
            suppress_while_running=frozenset(),
            coro_factory=_outage_diag_coro,
        )
    )
    log.info("netalertx_outage_diagnosis_submitted")


async def run_event_poll(
    api_client: "NetAlertXAPIClient",
    router: "NetAlertXEventRouter",
    db_path: str,
    interval_minutes: int,
    failure_threshold: int = 3,
    notifier: "Optional[NotifierProtocol]" = None,
    ha_ssh_client: "Optional[SSHClientProtocol]" = None,
    gate: "Optional[AutonomyGate]" = None,
    knowledge_store: "Optional[ChromaKnowledgeStore]" = None,
    slug: str = "",
) -> None:
    """Continuous poll loop — fetch /events on each interval until cancelled.

    When *failure_threshold* consecutive polls fail, sends a deduped outage card
    (requires *notifier* and *db_path*) and submits an availability diagnosis WorkItem
    (requires *ha_ssh_client*, *gate*, and *knowledge_store*).  The outage card
    auto-resolves when the API recovers.
    """
    _consecutive_failures = 0
    _outage_card_sent = False

    while True:
        routed = 0
        poll_error: Optional[Exception] = None
        try:
            routed = await poll_once(api_client, router, db_path)
            # Successful poll: reset failure counter and auto-resolve any outage card.
            if _consecutive_failures > 0:
                _consecutive_failures = 0
            if _outage_card_sent:
                _outage_card_sent = False
                await _resolve_outage_card(db_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            poll_error = exc
            _consecutive_failures += 1
            log.warning(
                "netalertx_event_poll_error",
                error=str(exc),
                consecutive_failures=_consecutive_failures,
            )

        if poll_error is not None and _consecutive_failures >= failure_threshold:
            if notifier is not None and not _outage_card_sent:
                await _send_outage_card(db_path, notifier, str(poll_error))
                _outage_card_sent = True
                if ha_ssh_client is not None and gate is not None:
                    from utils.llm.llm_factory import make_llm_client as _mlc

                    try:
                        _llm = _mlc()
                    except Exception:  # nosec B110
                        _llm = None
                    await _submit_outage_diagnosis(
                        ha_ssh_client, _llm, knowledge_store, gate, notifier, slug
                    )

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
