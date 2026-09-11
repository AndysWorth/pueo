"""Dashboard entity health monitor — detects Lovelace cards referencing missing or unregistered entities."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Optional, TYPE_CHECKING

from interfaces import (
    HAWebSocketClientProtocol,
    LLMClientProtocol,
)
from utils.core.logging import get_logger
from utils.ha.lovelace_utils import EntityRef, _extract_entity_refs
from utils.hitl.notify import NotifierProtocol

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

log = get_logger("ha_lovelace_monitor")


async def _run_lovelace_investigation(
    suspicious: list[dict],
    ws_client: HAWebSocketClientProtocol,
    db_path: str,
    notifier: NotifierProtocol,
    llm_client: Optional[LLMClientProtocol] = None,
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
) -> None:
    """Run an AgentLoop to investigate unregistered Lovelace entities.

    The agent uses check_entity_status, get_config_entries_all, get_ha_components,
    and read_logs to classify each entity, then calls finish_lovelace_investigation
    with findings (or an empty list when all entities are benign).
    """
    if not suspicious:
        return

    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_lovelace_investigation_registry
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.llm.llm_factory import make_llm_client
    from utils.core.prompts import load_prompt

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = FakeAutonomyGate(auto_execute_result=True)

    class _NullSSH:
        async def read_file(self, path: str) -> str:
            return ""

        async def write_file(self, path: str, content: str) -> None:
            pass

        async def download_file(self, remote_path: str, local_path: str) -> None:
            pass

        async def run(self, command: str, check: bool = False) -> tuple[int, str, str]:
            return (0, "", "")

        async def stream_lines(self, command: str):  # type: ignore[return]
            pass

    executor = ToolExecutor(
        ha_ssh_client=_NullSSH(),  # type: ignore[arg-type]
        gate=gate,  # type: ignore[arg-type]
        notifier=notifier,
        ha_ws_client=ws_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
    )

    registry = build_lovelace_investigation_registry()
    system_prompt = load_prompt("agent_loop_lovelace").format(
        terminal_tool="finish_lovelace_investigation"
    )

    lines = []
    for e in suspicious:
        has_state = e.get("has_state", False)
        dash = e.get("dashboard", "Default")
        view = e.get("view", "")
        card_title = e.get("card_title") or ""
        location = f"{dash} → {view}"
        if card_title:
            location += f' → "{card_title}"'
        lines.append(
            f"- {e['entity_id']}: has_state={has_state}, location={location!r}"
        )
    initial_context = (
        "Suspicious Lovelace entities (in dashboards but not in the entity registry):\n"
        + "\n".join(lines)
    )

    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
        publish_activity_done,
    )

    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,
        tool_registry=registry,
        system_prompt=system_prompt,
        terminal_tool_name="finish_lovelace_investigation",
        trigger="lovelace_poll",
        activity_type="lovelace_investigation",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "lovelace_investigation", trigger="Lovelace entity change"
        ),
    )
    increment_active_agent()
    try:
        result = await loop.run(initial_context=initial_context)
        publish_activity_done("lovelace_investigation", result.outcome)
    except Exception as exc:
        log.warning("lovelace_investigation_failed", error=str(exc))
        publish_activity_done("lovelace_investigation", "failed")
    finally:
        decrement_active_agent()


async def poll_for_dashboard_entity_issues(
    ws_client: Optional[HAWebSocketClientProtocol] = None,
    notifier: Optional[NotifierProtocol] = None,
    db_path: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    llm_client: Optional[LLMClientProtocol] = None,
) -> None:
    """Polling loop — checks all Lovelace dashboards for missing or unregistered entity references."""
    import config as _cfg
    from utils.hitl.card_types import (
        CARD_TYPE_DASHBOARD_ENTITY,
        CARD_TYPE_HA_CONFIG_ISSUE,
        CARD_TYPE_UNREGISTERED_ENTITY,
    )
    from utils.ha.ha_ws_client import HAWebSocketClient
    from utils.hitl.hitl_tracker import mark_card_resolved
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
        state_ids: set[str] = set()
        if any(ref.entity_id not in registry_ids for ref in entity_refs):
            try:
                states_raw = await _ws.get_states()
                state_ids = {s["entity_id"] for s in states_raw if s.get("entity_id")}
            except Exception:
                state_ids = set()

        active_missing: set[str] = set()
        suspicious_unregistered: list[dict] = []

        for ref in entity_refs:
            if ref.entity_id in registry_ids:
                continue

            if ref.dashboard_url_path:
                dash_label = ref.dashboard_title or ref.dashboard_url_path
            else:
                dash_label = "Default"

            if ref.entity_id in state_ids:
                # Entity has live state but no entity registry entry.
                # Pass to AgentLoop for LLM-driven classification.
                suspicious_unregistered.append(
                    {
                        "entity_id": ref.entity_id,
                        "has_state": True,
                        "dashboard": dash_label,
                        "view": ref.view_title,
                        "card_title": ref.card_title,
                    }
                )
                continue

            # Entity is absent from both registry and states — route to AgentLoop.
            active_missing.add(ref.entity_id)
            suspicious_unregistered.append(
                {
                    "entity_id": ref.entity_id,
                    "has_state": False,
                    "dashboard": dash_label,
                    "view": ref.view_title,
                    "card_title": ref.card_title,
                }
            )

        # Delegate unregistered entity classification to the AgentLoop.
        if suspicious_unregistered:
            from utils.agent.work_queue import (
                PRIORITY_NORMAL,
                WorkItem,
                get_work_queue_or_none,
            )

            _suspicious = suspicious_unregistered
            _ws_ref = _ws
            _db_ref = _db_path
            _notifier_ref = _notifier
            _llm_ref = llm_client
            _wq = get_work_queue_or_none()
            if _wq is not None:
                await _wq.submit(
                    WorkItem(
                        priority=PRIORITY_NORMAL,
                        activity_type="lovelace_investigation",
                        description="Lovelace entity investigation",
                        dedup_key="lovelace_investigation",
                        suppress_while_running=frozenset(),
                        coro_factory=lambda: _run_lovelace_investigation(
                            suspicious=_suspicious,
                            ws_client=_ws_ref,
                            db_path=_db_ref,
                            notifier=_notifier_ref,
                            llm_client=_llm_ref,
                        ),
                    )
                )
            else:
                await _run_lovelace_investigation(
                    suspicious=_suspicious,
                    ws_client=_ws_ref,
                    db_path=_db_ref,
                    notifier=_notifier_ref,
                    llm_client=_llm_ref,
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

        # Reconcile ha_config_issue cards: resolve when all entities in the card
        # are now in the entity registry.
        with sqlite3.connect(_db_path) as conn:
            cfg_rows = conn.execute(
                "SELECT card_key FROM hitl_suppression"
                " WHERE card_type = ? AND resolved_at IS NULL",
                (CARD_TYPE_HA_CONFIG_ISSUE,),
            ).fetchall()
        for (pending_key,) in cfg_rows:
            suffix = pending_key.removeprefix("ha_config_issue:")
            entity_ids_in_key = [eid for eid in suffix.split(":") if eid]
            if all(eid in registry_ids for eid in entity_ids_in_key):
                with sqlite3.connect(_db_path) as conn:
                    mark_card_resolved(conn, pending_key)
                log.info("lovelace_config_issue_resolved", card_key=pending_key)

        # Reconcile legacy unregistered-entity cards: resolve when entity joins the registry.
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

        _lv_n = len(active_missing) + len(suspicious_unregistered)
        _lv_outcome = "No entity issues" if _lv_n == 0 else f"{_lv_n} entity issue(s)"
        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv_inst = _get_sv()
            if _sv_inst is not None:
                _sv_inst.touch("lovelace_poll", outcome=_lv_outcome)
        except Exception:  # nosec B110
            pass
        from utils.agent.supervisor import supervised_sleep as _sup_sleep_lv

        await _sup_sleep_lv("lovelace_poll", _interval * 60)
