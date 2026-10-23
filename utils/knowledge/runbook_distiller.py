"""Post-session runbook distillation.

Turns a completed AgentLoop session into a structured candidate runbook or gap.

Two distillation lanes:
- **Procedural**: enqueued after any session that reached a conclusion (success,
  needs_action, monitoring, awaiting_approval).  Learns tool-use lessons from the
  trace without requiring user verification.  Lessons are merged into the candidate
  runbook's ``## Procedure notes`` section.
- **Verdict**: enqueued after explicit verification (success confirmed or failure +
  correction).  Learns decision rules and fix summaries.  Non-success sessions still
  write gaps immediately.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
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

# Total token budget for tool outputs in the trace, split evenly across steps.
_TRACE_OUTPUT_TOKENS: int = 4000
# Floor per step so long sessions still show something from every step.
_STEP_OUTPUT_MIN_TOKENS: int = 100


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


class InstanceFact(BaseModel):
    """One instance-specific fact, citing the trace step whose output shows it."""

    fact: str = Field(description="The fact, stated using words from the tool output")
    evidence_step: int = Field(
        description="Step number in the tool trace whose output contains this fact"
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
    instance_facts: list[InstanceFact] = Field(
        default_factory=list,
        description=(
            "Instance-specific facts not in the seed: device quirks, entity naming, "
            "known-wrong states, hardware.  Each must cite the trace step whose "
            "output shows it; facts not visible in a tool output are discarded."
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
            "True when the session adds no runbook knowledge beyond the seed: "
            "no novel decision rule, no pitfall not already listed. "
            "instance_facts are still saved when this is true."
        ),
    )


class ProceduralLesson(BaseModel):
    """One tool-use lesson from a session trace, citing the step that shows it."""

    lesson: str = Field(
        description=(
            "What worked, failed, or was redundant — stated using words from the "
            "cited step's output.  No verdict or root-cause claim."
        )
    )
    evidence_step: int = Field(
        description="Step number in the tool trace whose args or output shows this lesson"
    )


class ProceduralLessons(BaseModel):
    """Tool-use lessons distilled from a session trace (no verdicts)."""

    lessons: list[ProceduralLesson] = Field(
        default_factory=list,
        description="Up to 4 lessons about which tools worked, failed, or were redundant",
    )


_PROCEDURE_NOTES_HEADER = "## Procedure notes"
_PROCEDURE_MAX_NOTES = 8


def _format_step_trace(steps: list[AgentStep]) -> str:
    per_step = max(_STEP_OUTPUT_MIN_TOKENS, _TRACE_OUTPUT_TOKENS // max(1, len(steps)))
    lines = []
    for s in steps:
        args_preview = json.dumps(s.tool_call.arguments)[:200]
        if not s.tool_result.success:
            out_preview = f"[FAILED] {s.tool_result.error or '(no error text)'}"
        elif not s.tool_result.output.strip():
            out_preview = "[EMPTY]"
        else:
            out_preview = truncate_to_budget(
                s.tool_result.output, per_step, strategy="smart"
            )
        lines.append(
            f"  {s.step_number}. {s.tool_call.name}({args_preview}) → {out_preview}"
        )
    return "\n".join(lines) or "  (no steps)"


_FACT_TOKEN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.:/-]*[A-Za-z0-9])?")
_FACT_STOPWORDS = frozenset(
    "the a an and or but of to in on at for with from by is are was were be been "
    "this that these those it its as not no has have had which who when where "
    "than then there their they them into onto via per".split()
)


def _fact_is_grounded(fact: str, evidence: str) -> bool:
    """Cheap grounding check: does the cited step output support *fact*?

    Every distinctive token (contains a digit or ``._:/-``, or is a capitalized
    word after the first) must appear in the evidence.  Content words (4+ chars,
    not stopwords) must also be at least half present, except that a single
    content word is waived when distinctive tokens already matched.
    Case-insensitive.  Rejects facts the model carried over from seed text or
    invented outright.
    """
    ev = evidence.lower()
    if not fact.strip() or not ev.strip():
        return False
    tokens = _FACT_TOKEN_RE.findall(fact)
    distinctive: list[str] = []
    content: list[str] = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in _FACT_STOPWORDS:
            continue
        if (
            any(c.isdigit() for c in tok)
            or any(c in "._:/-" for c in tok)
            or (i > 0 and tok[0].isupper())
        ):
            distinctive.append(low)
        elif len(tok) >= 4:
            content.append(low)
    if not distinctive and not content:
        return False
    if any(t not in ev for t in distinctive):
        return False
    if distinctive and len(content) < 2:
        return True
    hits = sum(1 for t in content if t in ev)
    return hits * 2 >= len(content)


def _grounded_facts(
    facts: list[InstanceFact], steps: list[AgentStep]
) -> list[InstanceFact]:
    """Return facts whose cited step exists and whose text passes the grounding check."""
    evidence_by_step: dict[int, str] = {}
    for s in steps:
        evidence_by_step[s.step_number] = (
            f"{s.tool_result.output or ''}\n{s.tool_result.error or ''}"
        )
    kept: list[InstanceFact] = []
    for f in facts:
        evidence = evidence_by_step.get(f.evidence_step)
        if evidence is not None and _fact_is_grounded(f.fact, evidence):
            kept.append(f)
        else:
            log.info(
                "distillation_fact_rejected",
                fact=f.fact[:200],
                evidence_step=f.evidence_step,
                reason="no_such_step" if evidence is None else "ungrounded",
            )
    return kept


def _grounded_lessons(
    lessons: list[ProceduralLesson], steps: list[AgentStep]
) -> list[ProceduralLesson]:
    """Filter lessons to those whose cited step exists and whose text is grounded."""
    evidence_by_step: dict[int, str] = {}
    for s in steps:
        evidence_by_step[s.step_number] = (
            f"{s.tool_call.name}({json.dumps(s.tool_call.arguments)[:200]})\n"
            f"{s.tool_result.output or ''}\n{s.tool_result.error or ''}"
        )
    kept: list[ProceduralLesson] = []
    for lesson in lessons:
        evidence = evidence_by_step.get(lesson.evidence_step)
        if evidence is not None and _fact_is_grounded(lesson.lesson, evidence):
            kept.append(lesson)
        else:
            log.info(
                "procedure_lesson_rejected",
                lesson=lesson.lesson[:200],
                evidence_step=lesson.evidence_step,
                reason="no_such_step" if evidence is None else "ungrounded",
            )
    return kept


def _split_procedure_notes(approach: str) -> tuple[str, str]:
    """Split approach text into (main_body, procedure_notes).

    ``procedure_notes`` is the content of the ``## Procedure notes`` section (without the
    header).  ``main_body`` is everything else, with the section removed.  Returns
    ``(approach, "")`` when the section is absent.
    """
    if _PROCEDURE_NOTES_HEADER not in approach:
        return approach, ""
    idx = approach.index(_PROCEDURE_NOTES_HEADER)
    before = approach[:idx].rstrip()
    rest = approach[idx + len(_PROCEDURE_NOTES_HEADER) :]
    # Find the next section (## …) in the remainder.
    next_sec = rest.find("\n## ")
    if next_sec >= 0:
        notes = rest[:next_sec].strip()
        after = rest[next_sec:]
    else:
        notes = rest.strip()
        after = ""
    main = before + (after if after else "")
    return main, notes


def _merge_notes_text(existing_notes: str, new_lessons: list[ProceduralLesson]) -> str:
    """Merge new lessons into existing procedure-note lines, dedup, cap at _PROCEDURE_MAX_NOTES.

    Each note is stored as a ``- <lesson>`` bullet.  Deduplication normalises whitespace
    and lowercases before comparing.  Returns the merged bullet-list string (no header).
    """
    existing_bullets: list[str] = []
    for line in existing_notes.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            existing_bullets.append(stripped[2:])
        elif stripped:
            existing_bullets.append(stripped)

    seen: set[str] = {" ".join(b.lower().split()) for b in existing_bullets}
    merged = list(existing_bullets)
    for lesson in new_lessons:
        norm = " ".join(lesson.lesson.lower().split())
        if norm not in seen:
            seen.add(norm)
            merged.append(lesson.lesson)
    return "\n".join(f"- {b}" for b in merged[:_PROCEDURE_MAX_NOTES])


def _build_procedure_prompt(
    *,
    signature: str,
    outcome: str,
    step_trace: str,
) -> str:
    """Build the prompt for procedural-lesson distillation.

    Asks for tool-use lessons only — no root-cause verdict or fix summary.
    """
    return "\n".join(
        [
            "You are reviewing a Home Assistant diagnostic session for tool-use lessons.",
            "",
            f"Session signature: {signature}",
            f"Outcome: {outcome}",
            "",
            "Tool trace (FAILED = tool error, EMPTY = no output returned):",
            step_trace,
            "",
            "List up to 4 lessons about tool mechanics only:",
            "- Which tool call returned the key evidence (state which step)",
            "- Which calls failed or returned empty output (cite the FAILED/EMPTY step)",
            "- Which calls were redundant (called twice with the same arguments)",
            "- The correct command shape or URL when a call failed with a wrong input",
            "",
            "Rules:",
            "- Do NOT state a root cause, fix, or verdict.",
            "- Each lesson must cite a step number and reuse words from that step's "
            "output, args, or error message.",
            "- Skip lessons that add nothing beyond 'tool X was called' — only note "
            "failures, redundancies, or surprising successes.",
        ]
    )


def _write_instance_facts(
    db_path: str, signature: str, episode_id: str, facts: list[InstanceFact]
) -> int:
    """Upsert grounded facts into agent_memory scoped to *signature*.

    The key is derived from signature + normalised fact text, so a fact restated
    by a later session replaces its earlier row instead of duplicating it.
    Synchronous — call via asyncio.to_thread.  Returns the number of rows written.
    """
    if not facts or not signature:
        return 0
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        for f in facts:
            norm = " ".join(f.fact.lower().split())
            key = (
                "fact_"
                + hashlib.sha256(f"{signature}|{norm}".encode()).hexdigest()[:12]
            )
            conn.execute(
                "DELETE FROM agent_memory WHERE key = ? AND signature = ?",
                (key, signature),
            )
            conn.execute(
                "INSERT INTO agent_memory (key, content, source, ts, signature)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, f.fact, f"distilled:{episode_id}", now, signature),
            )
        conn.commit()
    return len(facts)


def _mark_distilled(db_path: str, episode_id: str) -> None:
    """Stamp session_outcomes.distilled_at so the session is never re-distilled."""
    if not db_path or not episode_id:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE session_outcomes SET distilled_at = ? WHERE episode_id = ?",
                (now, episode_id),
            )
            conn.commit()
    except Exception as exc:  # nosec B110
        log.warning("distillation_mark_failed", episode_id=episode_id, error=str(exc))


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
    Returns None when the row is missing, already cleared, already distilled, or the
    DB is unavailable.
    """
    if not db_path or not episode_id:
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT distill_payload_json, signature FROM session_outcomes"
                " WHERE episode_id = ? AND distilled_at IS NULL",
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
        "\n".join(f"- {f.fact}" for f in runbook.instance_facts) or "None noted."
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
    if seed_section:
        task = (
            "Write a runbook scoped to this exact symptom signature. "
            "Focus on the delta from the seed: the decisive decision rule and any "
            "pitfalls not already in the seed. "
            "If the session adds no decision rule or pitfall beyond the seed, set "
            "no_new_knowledge=true."
        )
    else:
        task = (
            "No seed runbook exists for this signature. Write a signature-level "
            "runbook — the method and decision rule for this kind of symptom, not "
            "for one specific version, release, or device — only if the decision "
            "rule generalizes beyond this one session. Otherwise set "
            "no_new_knowledge=true."
        )
    parts_prompt += [
        "",
        "Tool trace (FAILED = tool error, EMPTY = no output returned):",
        step_trace,
        "",
        task,
        "",
        "Separately, list instance_facts: facts about this specific Home Assistant "
        "instance (hardware, device quirks, entity naming, known-wrong states) that a "
        "future session on this symptom should know. Every fact must be visible in "
        "a tool output above; set evidence_step to that step's number and reuse the "
        "output's own words. Never include facts that come only from the seed, the "
        "initial context, or your own inference. instance_facts are saved even when "
        "no_new_knowledge=true.",
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
      the LLM path.  Unverified success sessions are skipped.  Grounded
      instance_facts go to agent_memory even when no_new_knowledge is set, and
      session_outcomes.distilled_at is stamped on every terminal path.
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

    try:
        await _distill_verified(
            trigger=trigger,
            signature=signature,
            steps=steps,
            outcome=outcome,
            episode_id=episode_id,
            db_path=db_path,
            knowledge_store=knowledge_store,
            model=model,
            initial_context=initial_context,
            episode_stub=episode_stub,
            llm_client=llm_client,
            verification=verification,
            correction=correction,
            seed_text=seed_text,
        )
    finally:
        # Every terminal path — inserted, merged, no_new_knowledge, LLM or write
        # failure — counts as distilled so startup recovery never repeats it.
        await asyncio.to_thread(_mark_distilled, db_path, episode_id)


async def _distill_verified(
    *,
    trigger: str,
    signature: str,
    steps: list[AgentStep],
    outcome: str,
    episode_id: str,
    db_path: str,
    knowledge_store: KnowledgeStoreClientProtocol,
    model: str,
    initial_context: str,
    episode_stub: Optional[dict],
    llm_client: Optional[LLMClientProtocol],
    verification: Optional[str],
    correction: Optional[str],
    seed_text: str,
) -> None:
    """LLM distillation of a verified session: facts → agent_memory, runbook → strategies."""
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

    facts = _grounded_facts(runbook.instance_facts, steps)
    runbook.instance_facts = facts
    if facts:
        try:
            written = await asyncio.to_thread(
                _write_instance_facts, db_path, signature, episode_id, facts
            )
            log.info(
                "distillation_facts_written",
                signature=signature,
                episode_id=episode_id,
                count=written,
            )
        except Exception as exc:  # nosec B110
            log.warning(
                "distillation_facts_write_failed", signature=signature, error=str(exc)
            )

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


# ---------------------------------------------------------------------------
# Procedural distillation lane (ADR 049)
# ---------------------------------------------------------------------------


def _persist_procedure_notes(
    db_path: str,
    signature: str,
    lessons: list[ProceduralLesson],
    knowledge_store: "KnowledgeStoreClientProtocol",
) -> bool:
    """Merge lessons into the candidate runbook for *signature*, or create one.

    If no live candidate/validated/seed runbook exists, inserts a procedure-only
    candidate (``fix_summary`` / ``decision_rule`` empty).  Deduplicates notes and
    caps at ``_PROCEDURE_MAX_NOTES``.  Returns True on success.
    Synchronous — call via asyncio.to_thread.
    """
    existing = _get_existing_by_signature(db_path, signature)
    notes_text = _merge_notes_text("", lessons)  # start from scratch; merge below

    if existing:
        strategy_id, current_version, episode_refs_json, existing_approach = existing
        main_approach, old_notes = _split_procedure_notes(existing_approach)
        notes_text = _merge_notes_text(old_notes, lessons)
        new_approach = (
            main_approach.rstrip() + f"\n\n{_PROCEDURE_NOTES_HEADER}\n{notes_text}"
        )
        new_version = current_version + 1
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE agent_strategies SET version=?, approach=? WHERE id=?",
                    (new_version, new_approach, strategy_id),
                )
                conn.commit()
        except Exception as exc:  # nosec B110
            log.warning(
                "procedure_notes_update_failed", signature=signature, error=str(exc)
            )
            return False
        try:
            row_title = ""
            with sqlite3.connect(db_path) as conn:
                t = conn.execute(
                    "SELECT title FROM agent_strategies WHERE id=?", (strategy_id,)
                ).fetchone()
                if t:
                    row_title = t[0] or signature
            text = f"# {row_title}\n\nSignature: {signature}\n\n{new_approach}"
            meta: dict = {
                "source": "agent_learned",
                "title": row_title,
                "trigger_pattern": signature,
                "strategy_id": strategy_id,
                "signature": signature,
                "runbook_type": "candidate",
            }
            knowledge_store.upsert("strategies", [strategy_id], [text], [meta])
        except Exception as exc:  # nosec B110
            log.warning(
                "procedure_notes_chroma_failed", signature=signature, error=str(exc)
            )
        log.info(
            "procedure_notes_merged",
            signature=signature,
            strategy_id=strategy_id,
            note_count=notes_text.count("\n- ")
            + (1 if notes_text.startswith("- ") else 0),
        )
    else:
        # No live runbook: create a procedure-only candidate.
        strategy_id = str(uuid.uuid4())
        title = f"Procedure notes: {signature}"
        approach = (
            "## When to use\n(Procedure-only: verdict not yet learned)\n\n"
            f"## Fix summary\n(pending verification)\n\n"
            f"{_PROCEDURE_NOTES_HEADER}\n{notes_text}"
        )
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO agent_strategies"
                    " (id, title, trigger_pattern, approach, runbook_state, created_at,"
                    "  signature, version, episode_refs)"
                    " VALUES (?, ?, ?, ?, 'candidate', datetime('now'), ?, 1, '[]')",
                    (strategy_id, title, signature, approach, signature),
                )
                conn.commit()
        except Exception as exc:  # nosec B110
            log.warning(
                "procedure_candidate_insert_failed",
                signature=signature,
                error=str(exc),
            )
            return False
        try:
            text = f"# {title}\n\nSignature: {signature}\n\n{approach}"
            meta = {
                "source": "agent_learned",
                "title": title,
                "trigger_pattern": signature,
                "strategy_id": strategy_id,
                "signature": signature,
                "runbook_type": "candidate",
            }
            knowledge_store.upsert("strategies", [strategy_id], [text], [meta])
        except Exception as exc:  # nosec B110
            log.warning(
                "procedure_candidate_chroma_failed", signature=signature, error=str(exc)
            )
        log.info(
            "procedure_candidate_created",
            signature=signature,
            strategy_id=strategy_id,
        )
    return True


async def distill_procedure(
    signature: str,
    steps: list[AgentStep],
    outcome: str,
    episode_id: str,
    db_path: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    model: str,
    llm_client: Optional["LLMClientProtocol"] = None,
) -> None:
    """Distil tool-use lessons from the session trace and merge into the runbook.

    Called without waiting for user verification — only captures HOW to gather
    evidence, never WHAT conclusion to draw.  No ``distilled_at`` stamp (that
    belongs to verdict distillation so recovery never skips the verdict lane).
    """
    if not signature or not steps or not db_path:
        return

    step_trace = _format_step_trace(steps)
    prompt = _build_procedure_prompt(
        signature=signature,
        outcome=outcome,
        step_trace=step_trace,
    )

    timeout_secs = await asyncio.to_thread(
        _distillation_timeout_seconds, db_path, model
    )

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
                format=ProceduralLessons.model_json_schema(),
                think=_think,
            ),
            timeout=timeout_secs,
        )
        content = response["message"]["content"]
        result = ProceduralLessons.model_validate_json(content)
    except Exception as exc:
        log.warning(
            "procedure_distillation_llm_failed",
            signature=signature,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    lessons = _grounded_lessons(result.lessons, steps)
    if not lessons:
        log.info(
            "procedure_distillation_no_lessons",
            signature=signature,
            episode_id=episode_id,
        )
        return

    try:
        ok = await asyncio.to_thread(
            _persist_procedure_notes, db_path, signature, lessons, knowledge_store
        )
        if ok:
            log.info(
                "procedure_distillation_done",
                signature=signature,
                episode_id=episode_id,
                lessons=len(lessons),
            )
    except Exception as exc:  # nosec B110
        log.warning(
            "procedure_distillation_persist_failed",
            signature=signature,
            error=str(exc),
        )


async def enqueue_procedural_distillation(
    episode_id: str,
    db_path: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    model: str,
    llm_client: Optional["LLMClientProtocol"] = None,
) -> bool:
    """Read the stashed session payload and enqueue a procedural distillation WorkItem.

    Called from AgentLoop._enqueue_distillation for outcomes that reached a conclusion.
    Returns True when a WorkItem was submitted.  No-op when: no queue, payload missing,
    or blank signature.
    """
    from utils.agent.work_queue import PRIORITY_LOW, WorkItem, get_work_queue_or_none

    queue = get_work_queue_or_none()
    if queue is None:
        return False

    payload = await asyncio.to_thread(_read_distillation_payload, db_path, episode_id)
    if not payload:
        return False

    sig = payload.get("signature", "")
    if not sig:
        return False

    steps_raw = payload.get("steps", [])
    episode_stub = payload.get("episode_stub")
    outcome = (
        (episode_stub or {}).get("outcome", "success") if episode_stub else "success"
    )

    from utils.agent.tool_registry import AgentStep

    steps: list[AgentStep] = []
    for s in steps_raw:
        try:
            steps.append(AgentStep.model_validate(s))
        except Exception:  # nosec B110
            pass

    _sig = sig
    _ep = episode_id
    _db = db_path
    _store = knowledge_store
    _model = model
    _llm = llm_client
    _outcome = outcome

    async def _run_procedure() -> None:
        await distill_procedure(
            signature=_sig,
            steps=steps,
            outcome=_outcome,
            episode_id=_ep,
            db_path=_db,
            knowledge_store=_store,
            model=_model,
            llm_client=_llm,
        )

    submitted = await queue.submit(
        WorkItem(
            priority=PRIORITY_LOW,
            activity_type="runbook_distillation",
            description=f"Procedure notes {sig}",
            dedup_key=f"procedure_{sig}_{episode_id}",
            suppress_while_running=frozenset(),
            coro_factory=_run_procedure,
        )
    )
    if submitted:
        log.info(
            "procedural_distillation_enqueued",
            signature=sig,
            episode_id=episode_id,
        )
    return bool(submitted)
