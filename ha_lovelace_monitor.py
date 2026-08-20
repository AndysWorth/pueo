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
from utils.logging import get_logger
from utils.notify import NotifierProtocol

log = get_logger("ha_lovelace_monitor")


@dataclass
class EntityRef:
    entity_id: str
    view_title: str
    card_index: int
    path: str


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


def _extract_entity_refs(lovelace_cfg: dict) -> list[EntityRef]:
    """Walk the Lovelace card tree and return all entity_id references."""
    refs: dict[str, EntityRef] = {}  # deduplicate by entity_id

    def _add(entity_id: str, view_title: str, card_index: int, path: str) -> None:
        if not entity_id or not isinstance(entity_id, str):
            return
        if entity_id not in refs:
            refs[entity_id] = EntityRef(
                entity_id=entity_id,
                view_title=view_title,
                card_index=card_index,
                path=path,
            )

    def _walk_entity(val: object, view_title: str, card_index: int, path: str) -> None:
        if isinstance(val, str):
            _add(val, view_title, card_index, path)
        elif isinstance(val, dict):
            # Handle both "entity" and "entity_id" keys used by different card types.
            _add(
                val.get("entity") or val.get("entity_id") or "",
                view_title,
                card_index,
                path,
            )

    def _walk_card(
        card: dict, view_title: str, card_index: int, depth: int = 0
    ) -> None:
        if not isinstance(card, dict):
            return
        prefix = f"card[{card_index}]"
        if depth > 0:
            prefix += f".nested[{depth}]"

        # Handle both "entity" and "entity_id" keys used by different card types.
        entity = card.get("entity") or card.get("entity_id") or ""
        if entity:
            _add(entity, view_title, card_index, prefix + ".entity")

        entities = card.get("entities", [])
        if isinstance(entities, list):
            for ent in entities:
                _walk_entity(ent, view_title, card_index, prefix + ".entities")

        for nested in card.get("cards", []):
            _walk_card(nested, view_title, card_index, depth + 1)

    for view in lovelace_cfg.get("views", []):
        if not isinstance(view, dict):
            continue
        view_title: str = str(view.get("title") or view.get("path") or "unnamed")

        for badge in view.get("badges", []):
            _walk_entity(badge, view_title, -1, "badges")

        # Traditional cards layout.
        for idx, card in enumerate(view.get("cards", [])):
            _walk_card(card, view_title, idx)

        # Sections layout (HA 2024+ dashboard type: sections).
        for sec_idx, section in enumerate(view.get("sections", [])):
            if not isinstance(section, dict):
                continue
            for card_idx, card in enumerate(section.get("cards", [])):
                _walk_card(card, view_title, card_idx)

    return list(refs.values())


def _fuzzy_candidates(
    entity_id: str, registry_ids: set[str], max_results: int = 5
) -> list[str]:
    """Return registry entity IDs closest to the missing one by simple edit distance."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    suffix = entity_id.split(".", 1)[1] if "." in entity_id else entity_id

    same_domain = [r for r in registry_ids if r.startswith(f"{domain}.")]

    def _dist(a: str, b: str) -> int:
        # Simple character-level Levenshtein distance, capped at 50 to avoid slow paths.
        la, lb = len(a), len(b)
        if abs(la - lb) > 20:
            return 999
        prev = list(range(lb + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                curr.append(
                    min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1))
                )
            prev = curr
        return prev[lb]

    ranked = sorted(same_domain, key=lambda r: _dist(suffix, r.split(".", 1)[-1]))
    return ranked[:max_results]


async def _analyze_missing_entity(
    ref: EntityRef,
    registry: list[dict],
    llm_client: Optional[LLMClientProtocol],
) -> DashboardEntityAnalysis:
    """Single-shot LLM call to explain a missing entity and recommend an action."""
    from utils.llm_factory import _default_model_for_provider, make_llm_client
    from utils.prompts import load_prompt

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
    from utils.card_types import CARD_TYPE_DASHBOARD_ENTITY
    from utils.ha_ws_client import HAWebSocketClient
    from utils.hitl_tracker import (
        mark_card_resolved,
        mark_card_sent,
        should_send_card,
        stable_nid,
    )
    from utils.notify import get_notifier

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
            except Exception:
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
                except Exception:
                    pass  # dashboard may not exist (e.g. no default dashboard)

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
