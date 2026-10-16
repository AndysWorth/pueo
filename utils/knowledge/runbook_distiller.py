"""Post-session runbook distillation.

Turns a completed AgentLoop session into a structured candidate runbook or gap.
Enqueued after verification (success confirmed or failure + correction) so the model
never distills unverified conclusions.  Non-success sessions still write gaps
immediately after the session ends.
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

# Per-call initial-context budget for the distillation prompt.
_INITIAL_CONTEXT_TOKENS: int = 400

# Per-step tool output budget in the trace (characters, not tokens).
_STEP_OUTPUT_MAX_CHARS: int = 800


def _sig_specificity(pattern: str, query: str) -> tuple[bool, int]:
    """Return (matches, specificity) for a stored pattern against a query signature.

    Both are colon-delimited strings.  A stored part of ``*`` is a wildcard that
    matches any single query part.  Specificity = number of exact (non-wildcard)
    parts that matched; higher is more specific.  Part counts must match exactly.

    Defined here (not in agent_loop) so runbook_distiller can use it for seed lookup
    without creating a circular import.
    """
    pp = pattern.split(":")
    qp = query.split(":")
    if len(pp) != len(qp):
        return False, 0
    score = 0
    for p, q in zip(pp, qp):
        if p == "*":
            continue
        if p != q:
            return False, 0
        score += 1
    return True, score


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
    instance_facts: list[str] = Field(
        default_factory=list,
        description=(
            "Instance-specific facts not in the seed: device quirks, entity naming, "
            "known-wrong states, user preferences relevant to this symptom"
        ),
    )
    decision_rule: str = Field(
        default="",
        description=(
            "One sentence rule-of-thumb for future agents: "
            "the decisive factor that determined the right action"
        ),
    )
    no_new_knowledge: bool = Field(
        default=False,
        description=(
            "True when the session adds nothing beyond the seed: "
            "no instance facts, no novel decision rule, no pitfall not already listed. "
            "Record usage only; do not write a runbook."
        ),
    )


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


def _fetch_seed_text_for_signature(db_path: str, signature: str) -> str:
    """Return the approach text of the most specific matching seed for *signature*.

    Uses the same wildcard matching as _sig_specificity.  Returns empty string when
    no matching seed is found or the DB is unavailable.
    """
    if not db_path or not signature or ":" not in signature:
        return ""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title, approach, signature FROM agent_strategies"
                " WHERE signature IS NOT NULL AND runbook_state = 'seed'"
            ).fetchall()
    except Exception:  # nosec B110
        return ""

    best: Optional[tuple[int, sqlite3.Row]] = None
    for row in rows:
        stored_sig = row["signature"] or ""
        matches, spec = _sig_specificity(stored_sig, signature)
        if not matches:
            continue
        if best is None or spec > best[0]:
            best = (spec, row)

    if best is None:
        return ""
    _, row = best
    title = row["title"] or signature
    approach = row["approach"] or ""
    return f"# {title}\n\n{approach}".strip()


def _read_distillation_payload(db_path: str, episode_id: str) -> Optional[dict]:
    """Read the stashed distill_payload_json and signature from session_outcomes.

    Returns a dict with keys: steps, initial_context, episode_stub, signature.
    Returns None when the row is missing, already cleared, or the DB is unavailable.
    """
    if not db_path or not episode_id:
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT distill_payload_json, signature FROM session_outcomes"
                " WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        payload: dict = json.loads(row[0])
        payload["signature"] = row[1] or payload.get("signature", "")
        return payload
    except Exception:  # nosec B110
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
    instance_facts_block = (
        "\n".join(f"- {f}" for f in runbook.instance_facts) or "None noted."
    )
    decision_rule_block = runbook.decision_rule or "(none)"
    return (
        f"## When to use\n{runbook.when_to_use}\n\n"
        f"## Recommended tools\n{', '.join(runbook.recommended_tools)}\n\n"
        f"## Hypotheses\n{hypotheses_block}\n\n"
        f"## Fix summary\n{runbook.fix_summary or '(none)'}\n\n"
        f"## Instance facts\n{instance_facts_block}\n\n"
        f"## Decision rule\n{decision_rule_block}\n\n"
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
    seed_text: str = "",
    verification: Optional[str] = None,
    correction: Optional[str] = None,
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

    seed_section = ""
    if seed_text.strip():
        seed_section = (
            "Seed runbook for this symptom (baseline knowledge for this exact signature):\n"
            + seed_text
        )

    correction_section = ""
    if correction:
        correction_section = (
            f"User correction (ground truth — the correct action was):\n{correction}"
        )

    outcome_line = outcome
    if verification:
        outcome_line = f"{outcome} (verified: {verification})"

    parts_prompt = [
        "You are distilling a Home Assistant diagnostic session into a reusable runbook.",
        "",
        f"Session signature: {signature}",
        f"Trigger: {trigger}",
        f"Outcome: {outcome_line}",
        "",
        "Initial context:",
        ctx_trimmed,
    ]
    if terminal_section:
        parts_prompt += ["", terminal_section]
    if correction_section:
        parts_prompt += ["", correction_section]
    if seed_section:
        parts_prompt += ["", seed_section]
    if existing_section:
        parts_prompt += ["", existing_section]
    parts_prompt += [
        "",
        "Tool trace (FAILED = tool error, EMPTY = no output returned):",
        step_trace,
        "",
        "Write a runbook scoped to this exact symptom signature. "
        "Focus on the delta from the seed: instance-specific facts (device quirks, "
        "entity naming, known-wrong states), the decisive decision rule, and any pitfalls "
        "not already in the seed. "
        "If this session adds nothing beyond the seed, set no_new_knowledge=true.",
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
    verification: Optional[str] = None,
    correction: Optional[str] = None,
    seed_text: str = "",
) -> None:
    """Orchestrate post-session distillation.

    Two call paths:
    - Non-success with no verification: write a gap (called immediately after session ends).
    - Success or failure-with-correction after verification: make a structured LLM call
      and merge or insert the candidate.  Set verification="success"/"failure" to trigger
      the LLM path.  Unverified success sessions are skipped.
    """
    if outcome != "success" and verification is None:
        # Immediate gap for non-success sessions (no verification needed).
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
        return

    if verification is None:
        # Success session but no verification yet — defer.
        log.info(
            "distillation_skipped",
            reason="pending_verification",
            signature=signature,
            episode_id=episode_id,
        )
        return

    if verification == "failure" and not correction:
        log.info(
            "distillation_skipped",
            reason="failure_no_correction",
            signature=signature,
            episode_id=episode_id,
        )
        return

    if verification not in ("success", "failure"):
        log.info(
            "distillation_skipped",
            reason=f"unrecognised_verification_{verification}",
            signature=signature,
            episode_id=episode_id,
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
        seed_text=seed_text,
        verification=verification,
        correction=correction,
    )

    timeout_secs = await asyncio.to_thread(
        _distillation_timeout_seconds, db_path, model
    )

    # Derive structured-output call options (one-shot: temp=0, num_predict=1024).
    from utils.llm.model_options import one_shot_options, one_shot_think

    _options = await asyncio.to_thread(one_shot_options, model)
    _think = await asyncio.to_thread(one_shot_think, model)

    _client = llm_client if llm_client is not None else make_llm_client()

    try:
        response = await asyncio.wait_for(
            _client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options=_options,
                format=DistilledRunbook.model_json_schema(),
                think=_think,
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

    if runbook.no_new_knowledge:
        log.info(
            "distillation_skipped",
            reason="no_new_knowledge",
            signature=signature,
            episode_id=episode_id,
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


async def enqueue_distillation_after_verification(
    episode_id: str,
    verification: str,
    correction: Optional[str],
    db_path: str,
    knowledge_store: KnowledgeStoreClientProtocol,
    model: str,
    llm_client: Optional[LLMClientProtocol] = None,
) -> bool:
    """Read the stashed session payload and enqueue a distillation WorkItem.

    Called after resolve_session_outcome[_by_episode_id] completes.
    Returns True when a WorkItem was submitted.
    No-op (returns False) when: no work queue, failure without correction,
    payload cleared/missing, or blank signature.
    """
    from utils.agent.work_queue import PRIORITY_LOW, WorkItem, get_work_queue_or_none

    if verification not in ("success", "failure"):
        return False
    if verification == "failure" and not correction:
        return False

    queue = get_work_queue_or_none()
    if queue is None:
        return False

    payload = await asyncio.to_thread(_read_distillation_payload, db_path, episode_id)
    if not payload:
        log.info(
            "distillation_payload_missing",
            episode_id=episode_id,
            reason="cleared_or_absent",
        )
        return False

    sig = payload.get("signature", "")
    if not sig:
        return False

    steps_raw = payload.get("steps", [])
    initial_ctx = payload.get("initial_context", "")
    episode_stub = payload.get("episode_stub")

    from utils.agent.tool_registry import AgentStep

    steps: list[AgentStep] = []
    for s in steps_raw:
        try:
            steps.append(AgentStep.model_validate(s))
        except Exception:  # nosec B110
            pass

    seed_text = await asyncio.to_thread(_fetch_seed_text_for_signature, db_path, sig)

    _correction = correction
    _verification = verification
    _ep = episode_id
    _db = db_path
    _store = knowledge_store
    _model = model
    _llm = llm_client

    async def _run_distill() -> None:
        await distill_and_persist(
            trigger=sig,
            signature=sig,
            steps=steps,
            outcome="success" if _verification == "success" else "failure",
            episode_id=_ep,
            db_path=_db,
            knowledge_store=_store,
            model=_model,
            initial_context=initial_ctx,
            episode_stub=episode_stub,
            llm_client=_llm,
            seed_text=seed_text,
            verification=_verification,
            correction=_correction,
        )

    submitted = await queue.submit(
        WorkItem(
            priority=PRIORITY_LOW,
            activity_type="runbook_distillation",
            description=f"Distill {sig} ({_verification})",
            dedup_key=f"distill_after_verify_{_ep}",
            suppress_while_running=frozenset(),
            coro_factory=_run_distill,
        )
    )
    if submitted:
        log.info(
            "distillation_enqueued_after_verification",
            signature=sig,
            episode_id=_ep,
            verification=_verification,
        )
    return bool(submitted)
