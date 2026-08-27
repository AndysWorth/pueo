"""Dashboard entity health monitor — detects Lovelace cards referencing missing entities."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional

from interfaces import (
    HAWebSocketClientProtocol,
    LLMClientProtocol,
)
from utils.core.logging import get_logger
from utils.ha.lovelace_utils import EntityRef, _extract_entity_refs, _fuzzy_candidates
from utils.hitl.notify import NotifierProtocol

log = get_logger("ha_lovelace_monitor")


@dataclass
class DashboardEntityAnalysis:
    explanation: str
    likely_cause: str  # renamed | deleted | disabled | integration_removed
    action: str  # replace | remove | investigate
    proposed_entity_id: Optional[str] = None

    @classmethod
    def model_json_schema(cls) -> dict:
        return {
            "type": "object",
            "properties": {
                "explanation": {"type": "string"},
                "likely_cause": {
                    "type": "string",
                    "enum": ["renamed", "deleted", "disabled", "integration_removed"],
                },
                "action": {
                    "type": "string",
                    "enum": ["replace", "remove", "investigate"],
                },
                "proposed_entity_id": {"type": ["string", "null"]},
            },
            "required": ["explanation", "likely_cause", "action", "proposed_entity_id"],
        }

    @classmethod
    def model_validate_json(cls, raw: str) -> "DashboardEntityAnalysis":
        import json

        d = json.loads(raw)
        return cls(
            explanation=d.get("explanation", ""),
            likely_cause=d.get("likely_cause", "deleted"),
            action=d.get("action", "investigate"),
            proposed_entity_id=d.get("proposed_entity_id") or None,
        )


async def _analyze_missing_entity(
    ref: EntityRef,
    registry: list[dict],
    llm_client: Optional[LLMClientProtocol],
) -> DashboardEntityAnalysis:
    """Single-shot LLM call to explain a missing entity and recommend an action."""
    from utils.llm.llm_factory import _default_model_for_provider, make_llm_client
    from utils.core.prompts import load_prompt

    client: LLMClientProtocol = llm_client or make_llm_client()  # pragma: no cover

    registry_ids = {e.get("entity_id", "") for e in registry if e.get("entity_id")}
    candidates = _fuzzy_candidates(ref.entity_id, registry_ids)

    domain = ref.entity_id.split(".")[0] if "." in ref.entity_id else ""
    domain_entities = sorted(e for e in registry_ids if e.startswith(f"{domain}."))[:20]

    prompt_text = load_prompt(
        "triage_dashboard_entity",
        entity_id=ref.entity_id,
        view_title=ref.view_title,
        card_index=str(ref.card_index),
        candidates="\n".join(candidates) if candidates else "(none)",
        domain_entities="\n".join(domain_entities) if domain_entities else "(none)",
    )

    messages = [
        {"role": "user", "content": prompt_text},
    ]

    try:
        response = await client.chat(
            model=_default_model_for_provider(),
            messages=messages,
            options={"temperature": 0.0},
            format=DashboardEntityAnalysis.model_json_schema(),
        )
        raw = response["message"]["content"]
        return DashboardEntityAnalysis.model_validate_json(raw)
    except Exception as exc:
        log.warning("lovelace_analysis_failed", entity_id=ref.entity_id, error=str(exc))
        return DashboardEntityAnalysis(
            explanation=f"Entity {ref.entity_id!r} not found in the HA entity registry.",
            likely_cause="deleted",
            action="investigate",
            proposed_entity_id=None,
        )


async def poll_for_dashboard_entity_issues(
    ws_client: Optional[HAWebSocketClientProtocol] = None,
    notifier: Optional[NotifierProtocol] = None,
    db_path: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    llm_client: Optional[LLMClientProtocol] = None,
) -> None:
    """Polling loop — checks all Lovelace dashboards for missing entity references."""
    import config as _cfg
    from utils.hitl.card_types import CARD_TYPE_DASHBOARD_ENTITY
    from utils.ha.ha_ws_client import HAWebSocketClient
    from utils.hitl.hitl_tracker import (
        mark_card_resolved,
        mark_card_sent,
        should_send_card,
        stable_nid,
    )
    from utils.hitl.notify import get_notifier

    _db_path: str = db_path or _cfg.DB_PATH
    _interval: int = (
        interval_minutes
        if interval_minutes is not None
        else _cfg.HA_LOVELACE_CHECK_INTERVAL_MINUTES
    )
    _ws: HAWebSocketClientProtocol = ws_client or HAWebSocketClient(  # pragma: no cover
        _cfg.HA_HOST, _cfg.HA_API_PORT, _cfg.HA_API_TOKEN
    )
    _notifier: NotifierProtocol = notifier or get_notifier(  # pragma: no cover
        _cfg.NOTIFIER, _cfg.NOTIFY_URL, _cfg.NOTIFY_WATCH_DIR
    )

    while True:
        try:
            # Enumerate all named dashboards; the default (url_path=None) is always tried.
            try:
                named = await _ws.get_lovelace_dashboards()
            except Exception as exc:
                log.warning("lovelace_dashboard_list_failed", error=str(exc))
                named = []

            url_paths: list[Optional[str]] = [None] + [  # type: ignore[assignment]
                d.get("url_path") for d in named if d.get("url_path")
            ]

            # Merge entity refs across all dashboards, deduplicating by entity_id.
            merged_refs: dict[str, EntityRef] = {}
            for url_path in url_paths:
                try:
                    cfg = await _ws.get_lovelace_config(url_path)
                    for ref in _extract_entity_refs(cfg):
                        if ref.entity_id not in merged_refs:
                            merged_refs[ref.entity_id] = ref
                except Exception as exc:  # nosec B110
                    log.warning(
                        "lovelace_dashboard_config_failed",
                        url_path=url_path,
                        error=str(exc),
                    )

            entity_refs = list(merged_refs.values())
            registry = await _ws.get_entity_registry()

        except Exception as exc:
            log.warning("lovelace_poll_failed", error=str(exc))
            await asyncio.sleep(_interval * 60)
            continue

        registry_ids = {e.get("entity_id", "") for e in registry if e.get("entity_id")}
        active_missing: set[str] = set()

        for ref in entity_refs:
            if ref.entity_id in registry_ids:
                continue
            active_missing.add(ref.entity_id)
            card_key = f"dashboard_entity:{ref.entity_id}"

            with sqlite3.connect(_db_path) as conn:
                if not should_send_card(conn, card_key):
                    continue

            analysis = await _analyze_missing_entity(ref, registry, llm_client)

            description = (
                f"Dashboard entity missing: {ref.entity_id} "
                f"(view: {ref.view_title}, card {ref.card_index})"
            )
            title = f"Missing dashboard entity: {ref.entity_id}"
            body_parts = [
                description,
                f"Cause: {analysis.likely_cause}",
                f"Action: {analysis.action}",
                analysis.explanation,
            ]
            if analysis.proposed_entity_id:
                body_parts.append(
                    f"Proposed replacement: {analysis.proposed_entity_id}"
                )

            payload: dict = {
                "notification_id": stable_nid(card_key),
                "card_type": CARD_TYPE_DASHBOARD_ENTITY,
                "suppression_key": card_key,
                "entity_id": ref.entity_id,
                "view_title": ref.view_title,
                "card_index": ref.card_index,
                "path": ref.path,
                "action": analysis.action,
                "likely_cause": analysis.likely_cause,
                "explanation": analysis.explanation,
                "proposed_entity_id": analysis.proposed_entity_id,
                "title": title,
                "body": "\n".join(body_parts),
            }

            with sqlite3.connect(_db_path) as conn:
                mark_card_sent(conn, card_key, CARD_TYPE_DASHBOARD_ENTITY, description)
            await _notifier.send(
                subject=title,
                body="\n".join(body_parts),
                payload=payload,
            )
            log.info(
                "lovelace_entity_card_sent",
                entity_id=ref.entity_id,
                action=analysis.action,
                likely_cause=analysis.likely_cause,
            )

        # Reconcile: mark resolved any card whose entity has since reappeared.
        with sqlite3.connect(_db_path) as conn:
            pending_rows = conn.execute(
                "SELECT card_key FROM hitl_suppression"
                " WHERE card_type = ? AND resolved_at IS NULL",
                (CARD_TYPE_DASHBOARD_ENTITY,),
            ).fetchall()
        for (pending_key,) in pending_rows:
            pending_entity_id = pending_key.removeprefix("dashboard_entity:")
            if pending_entity_id not in active_missing:
                with sqlite3.connect(_db_path) as conn:
                    mark_card_resolved(conn, pending_key)
                log.info("lovelace_entity_resolved", entity_id=pending_entity_id)

        await asyncio.sleep(_interval * 60)
