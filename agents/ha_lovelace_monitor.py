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
    from utils.hitl.card_types import (
        CARD_TYPE_DASHBOARD_ENTITY,
        CARD_TYPE_UNREGISTERED_ENTITY,
    )
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

            dash_titles = {
                d.get("url_path"): d.get("title", d.get("url_path", ""))
                for d in named
                if d.get("url_path")
            }

            url_paths: list[Optional[str]] = [None] + [  # type: ignore[assignment]
                d.get("url_path") for d in named if d.get("url_path")
            ]

            # Merge entity refs across all dashboards, deduplicating by entity_id.
            merged_refs: dict[str, EntityRef] = {}
            for url_path in url_paths:
                try:
                    lovelace_cfg = await _ws.get_lovelace_config(url_path)
                    dash_title = dash_titles.get(url_path, "") if url_path else ""
                    for ref in _extract_entity_refs(
                        lovelace_cfg,
                        dashboard_url_path=url_path,
                        dashboard_title=dash_title,
                    ):
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

        # Fetch hass.states once if any refs fall outside the registry.
        # Entities defined in YAML without unique_id appear in hass.states but not
        # the entity registry — they are valid, not missing.
        state_ids: set[str] = set()
        if any(ref.entity_id not in registry_ids for ref in entity_refs):
            try:
                states_raw = await _ws.get_states()
                state_ids = {s["entity_id"] for s in states_raw if s.get("entity_id")}
            except Exception:
                state_ids = set()

        active_missing: set[str] = set()
        active_unregistered: set[str] = set()

        for ref in entity_refs:
            if ref.entity_id in registry_ids:
                continue

            # Compute dash_url once; used by both card paths below.
            base = f"http://{_cfg.HA_HOST}:8123"
            if ref.dashboard_url_path:
                dash_url = f"{base}/{ref.dashboard_url_path}"
                dash_label = ref.dashboard_title or ref.dashboard_url_path
            else:
                dash_url = f"{base}/lovelace"
                dash_label = "Default"
            if ref.view_path:
                dash_url += f"/{ref.view_path}"

            if ref.entity_id in state_ids:
                # Entity has state but no unique_id — propose adding one.
                proposed_unique_id = ref.entity_id.replace(".", "_")
                card_key = f"unregistered_entity:{ref.entity_id}"
                active_unregistered.add(ref.entity_id)

                with sqlite3.connect(_db_path) as conn:
                    if not should_send_card(conn, card_key):
                        continue

                location_str = f"{dash_label} → {ref.view_title}"
                if ref.card_title:
                    location_str += f' → "{ref.card_title}"'
                yaml_hint = f"  unique_id: {proposed_unique_id}"
                description = (
                    f"Entity {ref.entity_id!r} has no unique_id — "
                    f"it cannot be renamed, disabled, or area-assigned in HA.\n"
                    f"Location: {location_str}\n"
                    f"Proposed unique_id: {proposed_unique_id}"
                )
                title = f"Unregistered entity: {ref.entity_id}"
                body_parts = [
                    description,
                    "",
                    "Add this to the entity's YAML definition:",
                    yaml_hint,
                ]
                unreg_payload: dict = {
                    "notification_id": stable_nid(card_key),
                    "card_type": CARD_TYPE_UNREGISTERED_ENTITY,
                    "suppression_key": card_key,
                    "entity_id": ref.entity_id,
                    "proposed_unique_id": proposed_unique_id,
                    "yaml_hint": yaml_hint,
                    "view_title": ref.view_title,
                    "card_title": ref.card_title,
                    "dashboard_url_path": ref.dashboard_url_path,
                    "dashboard_title": ref.dashboard_title,
                    "dash_url": dash_url,
                    "title": title,
                    "body": "\n".join(body_parts),
                }
                with sqlite3.connect(_db_path) as conn:
                    mark_card_sent(
                        conn, card_key, CARD_TYPE_UNREGISTERED_ENTITY, description
                    )
                await _notifier.send(
                    subject=title,
                    body="\n".join(body_parts),
                    payload=unreg_payload,
                )
                log.info(
                    "lovelace_unregistered_entity_card_sent", entity_id=ref.entity_id
                )
                continue

            active_missing.add(ref.entity_id)
            card_key = f"dashboard_entity:{ref.entity_id}"

            with sqlite3.connect(_db_path) as conn:
                if not should_send_card(conn, card_key):
                    continue

            analysis = await _analyze_missing_entity(ref, registry, llm_client)

            card_label = (
                f'"{ref.card_title}"' if ref.card_title else f"card {ref.card_index}"
            )
            location = f"{dash_label} → {ref.view_title} → {card_label}"
            description = (
                f"Dashboard entity missing: {ref.entity_id}\n"
                f"Dashboard: {location}\n"
                f"Navigate to: {dash_url}\n"
                f"(view: {ref.view_title}, card {ref.card_index}, field: {ref.path})"
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
                "dashboard_url_path": ref.dashboard_url_path,
                "dashboard_title": ref.dashboard_title,
                "card_title": ref.card_title,
                "dash_url": dash_url,
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

        # Reconcile missing-entity cards.
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

        # Reconcile unregistered-entity cards: resolve when entity gains a unique_id.
        with sqlite3.connect(_db_path) as conn:
            unreg_rows = conn.execute(
                "SELECT card_key FROM hitl_suppression"
                " WHERE card_type = ? AND resolved_at IS NULL",
                (CARD_TYPE_UNREGISTERED_ENTITY,),
            ).fetchall()
        for (pending_key,) in unreg_rows:
            pending_entity_id = pending_key.removeprefix("unregistered_entity:")
            if pending_entity_id in registry_ids:
                with sqlite3.connect(_db_path) as conn:
                    mark_card_resolved(conn, pending_key)
                log.info(
                    "lovelace_unregistered_entity_resolved",
                    entity_id=pending_entity_id,
                )

        _lv_n = len(active_missing) + len(active_unregistered)
        _lv_outcome = "No entity issues" if _lv_n == 0 else f"{_lv_n} entity issue(s)"
        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv_inst = _get_sv()
            if _sv_inst is not None:
                _sv_inst.touch("lovelace_poll", outcome=_lv_outcome)
        except Exception:  # nosec B110
            pass
        await asyncio.sleep(_interval * 60)
