"""Post-session runbook distillation.

Turns a completed AgentLoop session into a structured candidate runbook or gap.
Enqueued as a low-priority WorkItem after each session so the model never needs
to call save_runbook manually — learning becomes infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

from utils.core.context import truncate_to_budget
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol, LLMClientProtocol
    from utils.agent.tool_registry import AgentStep


def make_llm_client():  # pragma: no cover
    """Deferred import so the module loads without Ollama present."""
    from utils.llm.llm_factory import make_llm_client as _make

    return _make()


log = get_logger(__name__)

# Evidence-gathering tools — at least this many calls are required.
_EVIDENCE_TOOLS: frozenset[str] = frozenset(
    {
        "read_config",
        "read_logs",
        "read_file",
        "run_ha_command",
        "get_entity_history",
        "get_logbook",
        "render_ha_template",
        "search_integrations",
        "get_integration_diagnostics",
        "fetch_ha_docs",
        "search_ha_docs",
        "fetch_url",
        "investigate_device",
        "get_ha_profile",
        "get_spook_issues",
        "get_statistics",
        "get_recent_events",
        "get_area_layout",
        "get_automation_traces",
        "get_system_error_log",
        "get_ollama_status",
        "get_dashboard_entity_health",
        "check_entity_status",
        "search_log",
        "summarize_log_window",
        "read_pueo_log",
        "list_log_sources",
        "recall",
        "remember",
        "get_update_release_notes",
        "check_config_against_breaking_change",
        "get_pueo_command_catalog",
    }
)

_MIN_EVIDENCE_CALLS: int = 3

# Per-call initial-context budget for the distillation prompt.
_INITIAL_CONTEXT_TOKENS: int = 400

# Per-step tool output budget in the trace (characters, not tokens).
_STEP_OUTPUT_MAX_CHARS: int = 800


def _distillation_timeout_seconds(db_path: str, model: str) -> float:
    """Compute distillation LLM timeout using the same adaptive logic as the main loop."""
    import config as _cfg
    from utils.llm.llm_stats import expected_timeout_ms

    provider = "local"
    try:
        from utils.llm.llm_factory import _provider_name

        provider = _provider_name()
    except Exception:  # nosec B110
        pass

    return (
        expected_timeout_ms(
            db_path,
            model=model,
            provider=provider,
            factor=_cfg.AGENT_PER_CALL_TIMEOUT_FACTOR,
            min_ms=_cfg.AGENT_PER_CALL_MIN_TIMEOUT_SECONDS * 1000.0,
            max_ms=_cfg.AGENT_PER_CALL_MAX_TIMEOUT_SECONDS * 1000.0,
        )
        / 1000.0
    )


class DistilledRunbook(BaseModel):
    """Structured runbook produced by post-session distillation."""

    title: str = Field(description="Short descriptive title (max 80 chars)")
    when_to_use: str = Field(
        description="One sentence describing when this runbook applies"
    )
    recommended_tools: list[str] = Field(
        description="Ordered list of tool names called (evidence phase first)"
    )
    hypotheses: list[str] = Field(
        description="Hypotheses formed and confirmed or ruled out during the session"
    )
    fix_summary: str = Field(
        description="What worked or what conclusion was reached (empty string for gap)"
    )
    pitfalls: list[str] = Field(
        default_factory=list,
        description="Pitfalls, surprises, or things that did not work",
    )


def _count_evidence_calls(steps: list[AgentStep]) -> int:
    """Count evidence-gathering calls with successful, non-empty output."""
    return sum(
        1
        for s in steps
        if s.tool_call.name in _EVIDENCE_TOOLS
        and s.tool_result.success
        and s.tool_result.output.strip()
    )


def qualifies_for_distillation(outcome: str, steps: list[AgentStep]) -> bool:
    """Return True if this session warrants a candidate distillation LLM call."""
    return outcome == "success" and _count_evidence_calls(steps) >= _MIN_EVIDENCE_CALLS


def _format_step_trace(steps: list[AgentStep]) -> str:
    lines = []
    for s in steps:
        args_preview = json.dumps(s.tool_call.arguments)[:200]
        if not s.tool_result.success:
            out_preview = f"[FAILED] {s.tool_result.error or '(no error text)'}"
        elif not s.tool_result.output.strip():
            out_preview = "[EMPTY]"
        else:
            out_preview = s.tool_result.output[:_STEP_OUTPUT_MAX_CHARS]
        lines.append(
            f"  {s.step_number}. {s.tool_call.name}({args_preview}) → {out_preview}"
        )
    return "\n".join(lines) or "  (no steps)"


def _get_existing_by_signature(
    db_path: str, signature: str
) -> Optional[tuple[str, int, str, str]]:
    """Return (id, version, episode_refs_json, approach) for a live runbook."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, version, episode_refs, approach FROM agent_strategies"
                " WHERE signature = ? AND runbook_state NOT IN ('gap', 'discarded', 'resolved')"
                " ORDER BY version DESC LIMIT 1",
                (signature,),
            ).fetchone()
        if row:
            return (row[0], row[1] or 1, row[2] or "[]", row[3] or "")
    except Exception:  # nosec B110
        pass
    return None


def _merge_in_sqlite(
    db_path: str,
    strategy_id: str,
    new_version: int,
    episode_refs_json: str,
    approach: str,
    title: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE agent_strategies"
            " SET version=?, episode_refs=?, approach=?, title=? WHERE id=?",
            (new_version, episode_refs_json, approach, title, strategy_id),
        )
        conn.commit()


def _insert_in_sqlite(
    db_path: str,
    strategy_id: str,
    title: str,
    signature: str,
    trigger_pattern: str,
    approach: str,
    episode_id: str,
) -> None:
    episode_refs_json = json.dumps([episode_id]) if episode_id else "[]"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agent_strategies"
            " (id, title, trigger_pattern, approach, runbook_state, created_at,"
            "  signature, version, episode_refs)"
            " VALUES (?, ?, ?, ?, 'candidate', datetime('now'), ?, 1, ?)",
            (
                strategy_id,
                title,
                trigger_pattern,
                approach,
                signature,
                episode_refs_json,
            ),
        )
        conn.commit()


def write_gap(
    db_path: str,
    knowledge_store: KnowledgeStoreClientProtocol,
    signature: str,
    trigger: str,
    steps: list[AgentStep],
    outcome: str,
    episode_id: str,
) -> None:
    """Write or merge a gap for this signature. Synchronous — call via asyncio.to_thread."""
    tool_names = sorted({s.tool_call.name for s in steps})
    queries_tried = [
        s.tool_call.arguments.get("query", "")
        for s in steps
        if s.tool_call.name == "query_knowledge"
    ]
    approach = (
        f"Outcome: {outcome}\n"
        f"Queries tried: {'; '.join(q for q in queries_tried if q) or 'none'}\n"
        f"Tools used: {', '.join(tool_names) or 'none'}"
    )
    title = f"Gap: {trigger} ({outcome})"
    gap_id = str(uuid.uuid4())
    text = f"# {title}\n\nSignature: {signature}\n\nTrigger: {trigger}\n\n{approach}"
    meta: dict = {
        "source": "agent_learned",
        "title": title,
        "trigger_pattern": trigger,
        "strategy_id": gap_id,
        "signature": signature,
        "runbook_type": "gap",
    }
    try:
        knowledge_store.upsert("knowledge_gaps", [gap_id], [text], [meta])
    except Exception as exc:  # nosec B110
        log.warning("gap_write_chroma_failed", signature=signature, error=str(exc))

    episode_refs_json = json.dumps([episode_id]) if episode_id else "[]"
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at,"
                "  signature, version, episode_refs)"
                " VALUES (?, ?, ?, ?, 'gap', datetime('now'), ?, 1, ?)",
                (
                    gap_id,
                    title,
                    trigger,
                    approach,
                    signature,
                    episode_refs_json,
                ),
            )
            conn.commit()
    except Exception as exc:  # nosec B110
        log.warning("gap_write_sqlite_failed", signature=signature, error=str(exc))


def resolve_gap(db_path: str, signature: str, episode_id: str) -> None:
    """Mark any open gap with this signature as resolved. Synchronous."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE agent_strategies"
                " SET runbook_state='resolved', resolved_at=datetime('now'), resolved_by=?"
                " WHERE signature=? AND runbook_state='gap'",
                (episode_id, signature),
            )
            conn.commit()
    except Exception:  # nosec B110
        pass


def _build_approach(runbook: DistilledRunbook) -> str:
    hypotheses_block = "\n".join(f"- {h}" for h in runbook.hypotheses) or "None noted."
    pitfalls_block = "\n".join(f"- {p}" for p in runbook.pitfalls) or "None noted."
    return (
        f"## When to use\n{runbook.when_to_use}\n\n"
        f"## Recommended tools\n{', '.join(runbook.recommended_tools)}\n\n"
        f"## Hypotheses\n{hypotheses_block}\n\n"
        f"## Fix summary\n{runbook.fix_summary or '(none)'}\n\n"
        f"## Pitfalls\n{pitfalls_block}"
    )


def _build_prompt(
    *,
    signature: str,
    trigger: str,
    outcome: str,
    initial_context: str,
    episode_stub: Optional[dict],
    existing_approach: str,
    step_trace: str,
) -> str:
    ctx_trimmed = truncate_to_budget(
        initial_context, _INITIAL_CONTEXT_TOKENS, strategy="head"
    )

    terminal_section = ""
    if episode_stub:
        parts = []
        if episode_stub.get("summary"):
            parts.append(f"Summary: {episode_stub['summary']}")
        for key in (
            "recommendation",
            "action_taken",
            "root_cause",
            "human_explanation",
        ):
            val = episode_stub.get(key)
            if val:
                parts.append(f"{key.replace('_', ' ').title()}: {val}")
        if parts:
            terminal_section = "Terminal tool conclusions:\n" + "\n".join(parts)

    existing_section = ""
    if existing_approach.strip():
        existing_section = (
            "Existing runbook for this signature (refine, don't replace):\n"
            + existing_approach
        )

    parts_prompt = [
        "You are distilling a Home Assistant diagnostic session into a reusable runbook.",
        "",
        f"Session signature: {signature}",
        f"Trigger: {trigger}",
        f"Outcome: {outcome}",
        "",
        "Initial context:",
        ctx_trimmed,
    ]
    if terminal_section:
        parts_prompt += ["", terminal_section]
    if existing_section:
        parts_prompt += ["", existing_section]
    parts_prompt += [
        "",
        "Tool trace (FAILED = tool error, EMPTY = no output returned):",
        step_trace,
        "",
        "Write a concise, reusable runbook at the most general level that still captures "
        "the diagnostic approach. Use the specific trigger as an example, not as the scope. "
        "A runbook titled 'Diagnosing transient cloud integration connectivity errors' is "
        "reusable across all polling integrations; one scoped to a single component only "
        "helps that one case.",
    ]
    return "\n".join(parts_prompt)


async def distill_and_persist(
    trigger: str,
    signature: str,
    steps: list[AgentStep],
    outcome: str,
    episode_id: str,
    db_path: str,
    knowledge_store: KnowledgeStoreClientProtocol,
    model: str,
    initial_context: str = "",
    episode_stub: Optional[dict] = None,
    llm_client: Optional[LLMClientProtocol] = None,
) -> None:
    """Orchestrate post-session distillation.

    On success with enough evidence: make a structured LLM call, merge or insert
    the candidate into strategies, resolve any open gap for this signature.
    On non-success or thin evidence: write a gap.
    """
    if not qualifies_for_distillation(outcome, steps):
        if outcome != "success":
            await asyncio.to_thread(
                write_gap,
                db_path,
                knowledge_store,
                signature,
                trigger,
                steps,
                outcome,
                episode_id,
            )
            log.info(
                "distillation_gap_written",
                signature=signature,
                outcome=outcome,
            )
        else:
            log.debug(
                "distillation_skipped",
                signature=signature,
                outcome=outcome,
                evidence_calls=_count_evidence_calls(steps),
            )
        return

    # Resolve any existing gap for this signature first.
    await asyncio.to_thread(resolve_gap, db_path, signature, episode_id)

    # Check for an existing candidate to merge into (also fetches approach text).
    existing = await asyncio.to_thread(_get_existing_by_signature, db_path, signature)
    existing_approach = existing[3] if existing else ""

    step_trace = _format_step_trace(steps)
    prompt = _build_prompt(
        signature=signature,
        trigger=trigger,
        outcome=outcome,
        initial_context=initial_context,
        episode_stub=episode_stub,
        existing_approach=existing_approach,
        step_trace=step_trace,
    )

    timeout_secs = await asyncio.to_thread(
        _distillation_timeout_seconds, db_path, model
    )

    # Derive structured-output call options (one-shot: temp=0, num_predict=1024).
    from utils.llm.model_options import one_shot_options

    _options = await asyncio.to_thread(one_shot_options, model)

    _client = llm_client if llm_client is not None else make_llm_client()

    try:
        response = await asyncio.wait_for(
            _client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options=_options,
                format=DistilledRunbook.model_json_schema(),
            ),
            timeout=timeout_secs,
        )
        content = response["message"]["content"]
        runbook = DistilledRunbook.model_validate_json(content)
    except Exception as exc:
        log.warning(
            "distillation_llm_failed",
            signature=signature,
            error=str(exc),
            error_type=type(exc).__name__,
            timeout_secs=round(timeout_secs, 1),
        )
        return

    approach = _build_approach(runbook)
    text = f"# {runbook.title}\n\nSignature: {signature}\n\nTrigger: {trigger}\n\n{approach}"
    meta: dict = {
        "source": "agent_learned",
        "title": runbook.title,
        "trigger_pattern": trigger,
        "strategy_id": "",  # filled below
        "signature": signature,
        "runbook_type": "candidate",
    }

    if existing:
        strategy_id, current_version, episode_refs_json, _ = existing
        try:
            refs: list = json.loads(episode_refs_json)
        except Exception:  # nosec B110
            refs = []
        if episode_id and episode_id not in refs:
            refs.append(episode_id)
        new_version = current_version + 1
        try:
            await asyncio.to_thread(
                _merge_in_sqlite,
                db_path,
                strategy_id,
                new_version,
                json.dumps(refs),
                approach,
                runbook.title,
            )
        except Exception as exc:  # nosec B110
            log.warning(
                "distillation_merge_sqlite_failed", signature=signature, error=str(exc)
            )
            return
        meta["strategy_id"] = strategy_id
        try:
            await asyncio.to_thread(
                knowledge_store.upsert, "strategies", [strategy_id], [text], [meta]
            )
        except Exception as exc:  # nosec B110
            log.warning(
                "distillation_merge_chroma_failed", signature=signature, error=str(exc)
            )
        log.info(
            "distillation_merged",
            signature=signature,
            version=new_version,
            strategy_id=strategy_id,
        )
    else:
        strategy_id = str(uuid.uuid4())
        try:
            await asyncio.to_thread(
                _insert_in_sqlite,
                db_path,
                strategy_id,
                runbook.title,
                signature,
                trigger,
                approach,
                episode_id,
            )
        except Exception as exc:  # nosec B110
            log.warning(
                "distillation_insert_sqlite_failed",
                signature=signature,
                error=str(exc),
            )
            return
        meta["strategy_id"] = strategy_id
        try:
            await asyncio.to_thread(
                knowledge_store.upsert, "strategies", [strategy_id], [text], [meta]
            )
        except Exception as exc:  # nosec B110
            log.warning(
                "distillation_insert_chroma_failed",
                signature=signature,
                error=str(exc),
            )
        log.info(
            "distillation_inserted",
            signature=signature,
            strategy_id=strategy_id,
        )
