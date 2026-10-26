"""Verifying sessions without waiting on the user (ADR 050).

Two paths close a pending ``session_outcomes`` row without a card click:

- **Prediction checks.** A device-availability or host-health session may end
  with a falsifiable ``prediction`` (``recovers_without_action``,
  ``recovers_after_reload`` or ``stays_offline`` within N minutes), stored in
  ``conclusion_json``.  The poll that watches the condition judges it with
  ``judge_prediction`` and marks the session ``success``/``failure`` by
  ``system``.  An unpredicted recovery still verifies nothing (ADR 046).
- **Daily digest.** Once a day, one card lists every open card's
  ``verification_question`` so the user can answer them in one place.  The rows
  post to the existing ``/right``, ``/wrong`` and ``/off`` handlers.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from utils.core.logging import get_logger

log = get_logger("session_verification")

PREDICTION_KINDS: frozenset[str] = frozenset(
    {"recovers_without_action", "recovers_after_reload", "stays_offline"}
)
# A prediction must be checkable within a week.
_MAX_WITHIN_MINUTES = 7 * 24 * 60

_KIND_TEXT = {
    "recovers_without_action": "recover on its own",
    "recovers_after_reload": "recover after an integration reload",
    "stays_offline": "stay offline",
}

# Successful calls to these tools mean Pueo acted during the session.
_ACTION_TOOLS = frozenset({"reload_integration", "call_service", "run_ha_command"})


def normalize_prediction(raw: object) -> Optional[dict]:
    """Return ``{"kind", "within_minutes"}`` for a valid prediction, else None."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "")).strip()
    if kind not in PREDICTION_KINDS:
        return None
    try:
        within = int(raw.get("within_minutes", 0))
    except (TypeError, ValueError):
        return None
    if within < 1:
        return None
    return {"kind": kind, "within_minutes": min(within, _MAX_WITHIN_MINUTES)}


def describe_prediction(prediction: dict) -> str:
    """Plain-English form, e.g. "recover on its own within 30 min"."""
    kind = _KIND_TEXT.get(prediction.get("kind", ""), prediction.get("kind", ""))
    return f"{kind} within {prediction.get('within_minutes')} min"


@dataclass(frozen=True)
class PredictionVerdict:
    verification: str  # "success" | "failure"
    correction: Optional[str] = None


def judge_prediction(
    prediction: dict,
    created_ts: float,
    now: float,
    recovered_at: Optional[float],
    actioned: bool,
    reloaded: bool,
) -> Optional[PredictionVerdict]:
    """Judge a prediction against what happened.  None = not decidable (yet).

    ``recovered_at`` is when the condition cleared, or None while it persists.
    ``actioned`` means Pueo or the user acted on the condition during the
    session; ``reloaded`` means an integration reload ran.
    """
    kind = prediction["kind"]
    within = int(prediction["within_minutes"])
    deadline = created_ts + within * 60
    said = f"Predicted it would {describe_prediction(prediction)}"

    if recovered_at is None:
        if now < deadline:
            return None
        if kind == "stays_offline":
            return PredictionVerdict("success")
        waited = int((now - created_ts) / 60)
        return PredictionVerdict(
            "failure", f"{said}; it was still not recovered after {waited} min."
        )

    took = int((recovered_at - created_ts) / 60)
    on_time = recovered_at <= deadline
    if kind == "stays_offline":
        if on_time:
            return PredictionVerdict(
                "failure", f"{said}; it recovered after {took} min."
            )
        return PredictionVerdict("success")
    if kind == "recovers_without_action":
        if actioned:
            return None  # someone acted, so the claim can't be checked
        if on_time:
            return PredictionVerdict("success")
        return PredictionVerdict(
            "failure", f"{said}; it took {took} min to recover on its own."
        )
    # recovers_after_reload
    if not reloaded:
        return PredictionVerdict(
            "failure", f"{said}; it recovered after {took} min without a reload."
        )
    if on_time:
        return PredictionVerdict("success")
    return PredictionVerdict(
        "failure", f"{said}; it took {took} min to recover after the reload."
    )


def session_actions(steps: list) -> tuple[bool, bool]:
    """Return (actioned, reloaded) from a session's serialized AgentSteps."""
    actioned = reloaded = False
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        name = (step.get("tool_call") or {}).get("name", "")
        ok = bool((step.get("tool_result") or {}).get("success"))
        if ok and name in _ACTION_TOOLS:
            actioned = True
            reloaded = reloaded or name == "reload_integration"
    return actioned, reloaded


def _iso_to_ts(iso: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PendingPrediction:
    episode_id: str
    subject_key: str
    card_id: str
    created_ts: float
    prediction: Optional[dict]  # None only from load_pending_for_subject
    actioned: bool
    reloaded: bool


def _load_pending(
    db_path: str, where: str, params: tuple, need_prediction: bool
) -> list[PendingPrediction]:
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT episode_id, subject_key, card_id, created_at,"
                " conclusion_json, distill_payload_json FROM session_outcomes"
                " WHERE (verification IS NULL OR verification = 'pending')"
                f"   AND {where}",  # nosec B608 — fixed fragments only
                params,
            ).fetchall()
    except sqlite3.Error as e:
        log.warning("pending_predictions_load_failed", error=str(e))
        return []
    out: list[PendingPrediction] = []
    for ep, subject_key, card_id, created_at, conclusion_json, payload_json in rows:
        try:
            conclusion = json.loads(conclusion_json or "{}") or {}
            payload = json.loads(payload_json or "{}") or {}
        except (TypeError, ValueError):
            continue
        prediction = normalize_prediction(conclusion.get("prediction"))
        created_ts = _iso_to_ts(created_at)
        if created_ts is None or (need_prediction and prediction is None):
            continue
        actioned, reloaded = session_actions(payload.get("steps") or [])
        out.append(
            PendingPrediction(
                episode_id=ep,
                subject_key=subject_key or "",
                card_id=card_id or "",
                created_ts=created_ts,
                prediction=prediction,
                actioned=actioned,
                reloaded=reloaded,
            )
        )
    return out


def load_pending_predictions(db_path: str) -> list[PendingPrediction]:
    """Return every pending session whose conclusion carries a valid prediction."""
    return _load_pending(
        db_path, "conclusion_json LIKE '%\"prediction\"%'", (), need_prediction=True
    )


def load_pending_for_subject(db_path: str, subject_key: str) -> list[PendingPrediction]:
    """Return every pending session for *subject_key*, with or without a prediction."""
    return _load_pending(db_path, "subject_key = ?", (subject_key,), False)


async def resolve_prediction(
    db_path: str,
    pending: PendingPrediction,
    verdict: PredictionVerdict,
    knowledge_store: Any = None,
    llm_client: Any = None,
) -> None:
    """Mark the session system-verified and enqueue verdict-lane distillation."""
    import asyncio

    import config as _cfg
    from agents.ha_agent_advanced import resolve_session_outcome_by_episode_id
    from utils.knowledge.runbook_distiller import (
        enqueue_distillation_after_verification,
    )

    await asyncio.to_thread(
        resolve_session_outcome_by_episode_id,
        pending.episode_id,
        verdict.verification,
        "system",
        verdict.correction,
        knowledge_store,
    )
    log.info(
        "prediction_verified",
        episode_id=pending.episode_id,
        subject_key=pending.subject_key,
        kind=(pending.prediction or {}).get("kind", ""),
        verification=verdict.verification,
    )
    if knowledge_store is None:
        return
    await enqueue_distillation_after_verification(
        episode_id=pending.episode_id,
        verification=verdict.verification,
        correction=verdict.correction,
        db_path=db_path,
        knowledge_store=knowledge_store,
        model=_cfg.OLLAMA_MODEL,
        llm_client=llm_client,
    )


# ------------------------------------------------------------------
# Daily question digest
# ------------------------------------------------------------------

_DIGEST_PREF_KEY = "verification_digest:last_date"

# Mirrors the ``is_actionable`` card types in web/templates/index.html.  Yes on
# an actionable card leaves it open for Approve/Reject; on any other card it
# also acknowledges it.
ACTIONABLE_CARD_TYPES: frozenset[str] = frozenset(
    {
        "repair",
        "update",
        "netalertx_heal",
        "netalertx_setup",
        "netalertx_uninstall",
        "netalertx_migrate",
        "netalertx_switch",
        "resource_action",
        "disk_recovery",
        "code_proposal",
        "open_pr",
        "ha_repair",
        "cloud_escalation",
        "dashboard_entity",
        "unregistered_entity",
        "config_entry_reload",
        "ha_command",
        "service_call",
        "automation_create",
        "companion_setup",
        "device_recurring",
    }
)


def card_is_actionable(payload: dict) -> bool:
    """Same rule as the queue template's ``is_actionable``."""
    ct = payload.get("card_type", "")
    return bool(
        ct in ACTIONABLE_CARD_TYPES
        or (ct == "host_health" and not payload.get("informational"))
        or (ct == "device_offline" and payload.get("has_fix"))
        or payload.get("pending_fix_yaml")
    )


def digest_due(now: float, hour: int, last_date: Optional[str]) -> Optional[str]:
    """Return today's local date when the digest should go out now, else None."""
    if hour < 0:
        return None
    local = time.localtime(now)
    today = time.strftime("%Y-%m-%d", local)
    if local.tm_hour < hour or last_date == today:
        return None
    return today


def collect_digest_items(watch_dir: Path, db_path: str) -> list[dict]:
    """Open cards with a verification question whose session is still pending."""
    try:
        with sqlite3.connect(db_path) as conn:
            pending = {
                r[0]
                for r in conn.execute(
                    "SELECT episode_id FROM session_outcomes"
                    " WHERE verification IS NULL OR verification = 'pending'"
                ).fetchall()
            }
    except sqlite3.Error as e:
        log.warning("digest_pending_load_failed", error=str(e))
        return []
    items: list[dict] = []
    if not watch_dir.is_dir():
        return items
    for json_file in watch_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text())
        except Exception:  # nosec B112
            continue
        nid = str(data.get("notification_id") or json_file.stem)
        if any(
            (watch_dir / f"{nid}{s}").exists()
            for s in (".approved", ".rejected", ".resolved", ".deferred")
        ):
            continue
        payload = data.get("payload") or {}
        question = str(payload.get("verification_question", "")).strip()
        if not question or payload.get("episode_id") not in pending:
            continue
        items.append(
            {
                "nid": nid,
                "subject": str(data.get("subject", "")),
                "question": question,
                "card_type": payload.get("card_type", ""),
                "acknowledge": not card_is_actionable(payload),
                "can_turn_off": payload.get("card_type") == "device_offline"
                and bool(payload.get("devices")),
                "sent_at": int(data.get("sent_at", 0) or 0),
            }
        )
    items.sort(key=lambda i: i["sent_at"])
    return items


def _load_last_digest_date(db_path: str) -> Optional[str]:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?", (_DIGEST_PREF_KEY,)
            ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def _save_last_digest_date(db_path: str, date: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_DIGEST_PREF_KEY, date),
        )
        conn.commit()


async def maybe_send_verification_digest(
    notifier: Any,
    db_path: str,
    watch_dir: str,
    hour: int,
    now: Optional[float] = None,
) -> bool:
    """Send today's digest card once, at or after *hour* local time.

    Returns True when a card was sent.  A day with no open questions sends
    nothing but still counts as done.
    """
    import asyncio

    from utils.hitl.card_types import CARD_TYPE_VERIFICATION_DIGEST
    from utils.hitl.hitl_tracker import stable_nid

    now = time.time() if now is None else now
    last = await asyncio.to_thread(_load_last_digest_date, db_path)
    today = digest_due(now, hour, last)
    if today is None:
        return False
    items = await asyncio.to_thread(collect_digest_items, Path(watch_dir), db_path)
    await asyncio.to_thread(_save_last_digest_date, db_path, today)
    if not items:
        log.info("verification_digest_empty", date=today)
        return False
    n = len(items)
    await notifier.send(
        subject=f"Pueo: {n} question{'s' if n != 1 else ''} to confirm",
        body=(
            "Your answers tell Pueo which conclusions were right."
            " Only confirmed sessions teach it anything."
        ),
        payload={
            "notification_id": stable_nid(f"verification_digest:{today}"),
            "card_type": CARD_TYPE_VERIFICATION_DIGEST,
            "type": "verification_digest",
            "date": today,
            "items": items,
            "informational": True,
        },
    )
    log.info("verification_digest_sent", date=today, items=n)
    return True
