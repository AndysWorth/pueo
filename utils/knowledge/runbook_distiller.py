"""Post-session runbook distillation.

Turns a completed AgentLoop session into atomic runbook entries or a gap.

Learned knowledge lives in ``runbook_entries`` (ADR 051): one row per decision
rule, pitfall or procedure note, with provenance and success/failure counts.
Entries are added or reinforced, never rewritten, and the learned runbook's
``approach`` is rendered from the live ones (``render_learned_runbook``).

Two distillation lanes:
- **Procedural**: enqueued after any session that reached a conclusion (success,
  needs_action, monitoring, awaiting_approval).  The deterministic extractor
  records failed, corrected, repeated and empty tool calls as ``procedure``
  entries, with no LLM and no verification.
- **Verdict**: enqueued after explicit verification (success confirmed or failure +
  correction).  The LLM proposes decision rules and pitfalls beyond the seed;
  code gates (grounding, seed overlap, version scrub, known tools) decide which
  become entries.  Non-success sessions still write gaps immediately.
"""

from __future__ import annotations

import asyncio
import functools
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
    from utils.agent.tool_registry import AgentStep, ToolCall


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


class DeltaItem(BaseModel):
    """One decision rule or pitfall the session adds beyond the seed."""

    text: str = Field(description="One sentence, reusing words from the cited step")
    evidence_step: int = Field(
        description="Step number in the tool trace whose args or output shows this"
    )


class DistilledDelta(BaseModel):
    """What a verified session adds beyond its seed (ADR 051).

    Only new items — never a restated seed, a title or a tool list.  Empty lists
    are the expected answer when the session confirms the seed.
    """

    decision_rules: list[DeltaItem] = Field(
        default_factory=list,
        description="Decisive factors that determined the right action, not in the seed",
    )
    pitfalls: list[DeltaItem] = Field(
        default_factory=list,
        description="Things that misled or did not work, not in the seed",
    )
    instance_facts: list[InstanceFact] = Field(
        default_factory=list,
        description=(
            "Instance-specific facts not in the seed: device quirks, entity naming, "
            "known-wrong states, hardware.  Each must cite the trace step whose "
            "output shows it; facts not visible in a tool output are discarded."
        ),
    )


class ProceduralLesson(BaseModel):
    """One tool-use note from a session trace, citing the step that shows it."""

    lesson: str = Field(description="What failed, was repeated or returned nothing")
    evidence_step: int = Field(
        description="Step number in the tool trace whose args or output shows this"
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


# Tools whose output is log content: an empty log search is evidence, not a
# tool problem.
_LOG_READER_TOOLS = frozenset(
    {"read_logs", "search_log", "read_pueo_log", "summarize_log_window"}
)
# finish_* tools end the session; a lesson about them teaches nothing.
_TERMINAL_TOOL_PREFIX = "finish_"


# ---------------------------------------------------------------------------
# Deterministic procedure facts (#991, amends ADR 049)
# ---------------------------------------------------------------------------

# Errors about Pueo's own setup or a transient stall, not about how a tool behaves.
_ENVIRONMENT_ERROR_RE = re.compile(
    r"client not available|not configured|timed out|timeout", re.I
)
# Config entry ids (32-hex or ULID) and UUIDs vary per instance; normalise them
# so the same quirk dedupes across sessions.
_ID_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    r"|[0-9a-f]{32}|[0-7][0-9A-HJKMNP-TV-Z]{25})\b"
)
_HTTPX_STATUS_RE = re.compile(r"Client error '(\d{3}[^']*)'")
_HTTPX_NOISE_RE = re.compile(r"\s*for url\s*'[^']*'|\s*For more information.*$", re.S)
_URL_RE = re.compile(r"https?://\S+")
_EMPTY_RESULT_RE = re.compile(r"^\(?no\b", re.I)
_EMPTY_RESULT_MAX_CHARS = 200
_NOTE_ERROR_CHARS = 120
_NOTE_ARG_CHARS = 100
# Free-text args say why a call was made, not how; they never belong in a note.
_NOTE_SKIP_ARGS = frozenset({"reason"})
_QUIRK_PREFIX = "procedure_quirk:"

# Python exception patterns that indicate a Pueo code defect (not an HA/external error).
# These are stored as procedure_quirk rows with kind="pueo_defect" and never injected as
# procedure notes into prompts.
_PUEO_DEFECT_RE = re.compile(
    r"invalid literal for int\b"
    r"|(?:TypeError|KeyError|ValueError|AttributeError|IndexError|NameError"
    r"|ZeroDivisionError|RuntimeError):\s"
    r"|object is not subscriptable"
    r"|NoneType.*has no attribute",
    re.I,
)

# Normalise quirk detail so `App mosquitto…` and `App matter_server…` count as one.
_QUOTED_TOKEN_RE = re.compile(r"'[^']*?'")
_NUMBER_TOKEN_RE = re.compile(r"\b\d+\b")


def _normalise_quirk_detail(text: str) -> str:
    """Replace quoted strings → '<x>' and numbers → 'N' for stable dedup."""
    text = _QUOTED_TOKEN_RE.sub("'<x>'", text)
    text = _NUMBER_TOKEN_RE.sub("N", text)
    return " ".join(text.split())[:_NOTE_ERROR_CHARS]


def _normalise_error(text: str) -> str:
    text = _HTTPX_STATUS_RE.sub(r"HTTP \1", text)
    text = _HTTPX_NOISE_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _ID_RE.sub("<id>", text)
    return " ".join(text.split()).rstrip(" .:")[:_NOTE_ERROR_CHARS]


def _call_args(call: ToolCall) -> dict:
    return {k: v for k, v in call.arguments.items() if k not in _NOTE_SKIP_ARGS}


def _format_call(call: ToolCall) -> str:
    """Render a tool call compactly: ``tool(value)`` or ``tool(k=v, k2=v2)``."""
    args = _call_args(call)
    parts = [
        " ".join(_ID_RE.sub("<id>", str(v)).split())[:_NOTE_ARG_CHARS]
        for v in args.values()
    ]
    if len(args) > 1:
        parts = [f"{k}={p}" for k, p in zip(args, parts)]
    return f"{call.name}({', '.join(parts)})"


def _is_correction(failed: ToolCall, later: ToolCall) -> bool:
    """True when *later* retries *failed* with a corrected value for the same input.

    Same tool and arg names, and each changed value is related to the one it
    replaces (one contains the other's last word: ``humidy`` → ``humidifier.humidy``).
    A different command on the same tool is not a correction.
    """
    a, b = _call_args(failed), _call_args(later)
    if later.name != failed.name or a.keys() != b.keys() or a == b:
        return False
    for k in a:
        old, new = str(a[k]).lower(), str(b[k]).lower()
        if old == new:
            continue
        old_last = (old.split() or [old])[-1]
        new_last = (new.split() or [new])[-1]
        if not (old_last in new or new_last in old):
            return False
    return True


def _is_empty_result(step: AgentStep) -> bool:
    out = (step.tool_result.output or "").strip()
    if not out:
        return True
    return len(out) < _EMPTY_RESULT_MAX_CHARS and bool(_EMPTY_RESULT_RE.match(out))


def _step_problem(step: AgentStep) -> Optional[tuple[str, str]]:
    """Classify a step as ``("failed", error)`` or ``("empty", "")``, else None.

    Approval hand-offs, terminal tools, environment errors, and empty log
    searches (absence of a log line is evidence, not a tool problem) are not
    problems.
    """
    r = step.tool_result
    if r.awaiting_approval or step.tool_call.name.startswith(_TERMINAL_TOOL_PREFIX):
        return None
    if not r.success:
        err = r.error or r.output or ""
        if _ENVIRONMENT_ERROR_RE.search(err):
            return None
        return "failed", _normalise_error(err) or "(no error text)"
    if step.tool_call.name not in _LOG_READER_TOOLS and _is_empty_result(step):
        return "empty", ""
    return None


def extract_procedure_facts(steps: list[AgentStep]) -> list[ProceduralLesson]:
    """Derive tool-use notes from a trace without an LLM.

    Records failed calls with their normalised error (or, when a later call
    retried the same input with a corrected value and succeeded, the corrected
    call), identical calls repeated within the session, and calls that returned
    nothing — in that order, since the note list is capped.  Pure.
    """
    failures: list[ProceduralLesson] = []
    repeats: list[ProceduralLesson] = []
    empties: list[ProceduralLesson] = []
    seen: set[str] = set()

    def _add(bucket: list[ProceduralLesson], text: str, step_number: int) -> None:
        if text not in seen:
            seen.add(text)
            bucket.append(ProceduralLesson(lesson=text, evidence_step=step_number))

    calls: dict[str, list[AgentStep]] = {}
    for i, s in enumerate(steps):
        key = s.tool_call.name + json.dumps(_call_args(s.tool_call), sort_keys=True)
        calls.setdefault(key, []).append(s)
        problem = _step_problem(s)
        if problem is None:
            continue
        kind, detail = problem
        shown = _format_call(s.tool_call)
        if kind == "empty":
            _add(empties, f"`{shown}` returned no results", s.step_number)
            continue
        # Pueo-internal errors (Python exceptions from Pueo's own code) are tracked
        # separately as pueo_defect quirks and never surfaced as procedure notes.
        if _PUEO_DEFECT_RE.search(detail):
            continue
        fix = next(
            (
                t
                for t in steps[i + 1 :]
                if _is_correction(s.tool_call, t.tool_call)
                and _step_problem(t) is None
                and t.tool_result.success
            ),
            None,
        )
        if fix is not None:
            _add(
                failures,
                f"`{shown}` failed ({detail}); `{_format_call(fix.tool_call)}` worked",
                s.step_number,
            )
        else:
            _add(failures, f"`{shown}` failed: {detail}", s.step_number)
    for same in calls.values():
        if len(same) >= 2:
            _add(
                repeats,
                f"`{_format_call(same[0].tool_call)}` was called {len(same)}× "
                "with identical arguments; reuse the first result",
                same[1].step_number,
            )
    return failures + repeats + empties


def _record_tool_quirks(db_path: str, signature: str, steps: list[AgentStep]) -> int:
    """Count each (tool, problem) once per session in ``user_prefs``.

    One row per quirk (``procedure_quirk:<hash>``) holds the count, the
    signatures it was seen under, an example call and the last-seen time, so the
    learning-health table can show which tools keep tripping agents up.
    Synchronous — call via asyncio.to_thread.  Returns rows written.
    """
    found: dict[str, dict] = {}
    for s in steps:
        problem = _step_problem(s)
        if problem is None:
            continue
        kind, detail = problem
        if kind == "failed" and _PUEO_DEFECT_RE.search(detail):
            kind = "pueo_defect"
        norm_detail = (
            _normalise_quirk_detail(detail) if kind == "pueo_defect" else detail
        )
        h = hashlib.sha256(
            f"{s.tool_call.name}|{kind}|{norm_detail}".encode()
        ).hexdigest()
        found.setdefault(
            _QUIRK_PREFIX + h[:12],
            {
                "tool": s.tool_call.name,
                "kind": kind,
                "detail": norm_detail,
                "example": _format_call(s.tool_call),
            },
        )
    if not found:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with sqlite3.connect(db_path) as conn:
            for key, quirk in found.items():
                row = conn.execute(
                    "SELECT value FROM user_prefs WHERE key=?", (key,)
                ).fetchone()
                prev = json.loads(row[0]) if row else {}
                sigs = list(prev.get("signatures", []))
                if signature and signature not in sigs:
                    sigs.append(signature)
                quirk.update(
                    count=int(prev.get("count", 0)) + 1, signatures=sigs, last_seen=now
                )
                conn.execute(
                    "INSERT OR REPLACE INTO user_prefs (key, value) VALUES (?, ?)",
                    (key, json.dumps(quirk)),
                )
            conn.commit()
    except (sqlite3.Error, ValueError, AttributeError) as exc:
        log.warning("tool_quirk_record_failed", signature=signature, error=str(exc))
        return 0
    return len(found)


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


def _fetch_seed_for_signature(db_path: str, signature: str) -> tuple[str, str]:
    """Return (seed_signature, seed_text) of the most specific seed for *signature*.

    Uses the same wildcard matching as _sig_specificity.  Returns ``("", "")``
    when no matching seed is found or the DB is unavailable.
    """
    if not db_path or not signature or ":" not in signature:
        return "", ""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title, approach, signature FROM agent_strategies"
                " WHERE signature IS NOT NULL AND runbook_state = 'seed'"
            ).fetchall()
    except Exception:  # nosec B110
        return "", ""

    best: Optional[tuple[int, sqlite3.Row]] = None
    for row in rows:
        stored_sig = row["signature"] or ""
        matches, spec = _sig_specificity(stored_sig, signature)
        if not matches:
            continue
        if best is None or spec > best[0]:
            best = (spec, row)

    if best is None:
        return "", ""
    _, row = best
    title = row["title"] or signature
    approach = row["approach"] or ""
    return row["signature"], f"# {title}\n\n{approach}".strip()


def _fetch_seed_text_for_signature(db_path: str, signature: str) -> str:
    """Return the text of the most specific matching seed for *signature*, or ''."""
    return _fetch_seed_for_signature(db_path, signature)[1]


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


def _build_prompt(
    *,
    signature: str,
    trigger: str,
    outcome: str,
    initial_context: str,
    episode_stub: Optional[dict],
    step_trace: str,
    seed_text: str = "",
    known_entries: Optional[list[str]] = None,
    verification: Optional[str] = None,
    correction: Optional[str] = None,
) -> str:
    """Build the verdict-lane prompt: ask for the delta beyond the seed (ADR 051)."""
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

    outcome_line = outcome
    if verification:
        outcome_line = f"{outcome} (verified: {verification})"

    parts_prompt = [
        "You are reviewing a verified Home Assistant diagnostic session for what it "
        "teaches beyond the existing runbook.",
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
    if correction:
        parts_prompt += [
            "",
            f"User correction (ground truth — the correct action was):\n{correction}",
        ]
    if seed_text.strip():
        parts_prompt += ["", "Seed runbook for this symptom:", seed_text]
    if known_entries:
        parts_prompt += [
            "",
            "Already learned for this signature:",
            *(f"- {e}" for e in known_entries),
        ]
    parts_prompt += [
        "",
        "Tool trace (FAILED = tool error, EMPTY = no output returned):",
        step_trace,
        "",
        "List only what this session adds beyond the seed and the already-learned "
        "items:",
        "- decision_rules: what determined the right action, one sentence each, "
        "stated directly as 'When <evidence>, <conclusion or action>'.",
        "- pitfalls: what misled the session or did not work, one sentence each.",
        "",
        "Rules:",
        "- Never restate the seed or an already-learned item.  Empty lists are the "
        "right answer when the session simply confirms the seed.",
        "- Each item cites evidence_step, the trace step that shows it, and reuses "
        "that step's own words.",
        "- Write for this kind of symptom, never for one version or release: no "
        "version numbers.",
        "- Name only tools that appear in the trace.",
        "",
        "Separately, list instance_facts: facts about this specific Home Assistant "
        "instance (hardware, device quirks, entity naming, known-wrong states) that a "
        "future session on this symptom should know. Every fact must be visible in "
        "a tool output above; set evidence_step to that step's number and reuse the "
        "output's own words. Never include facts that come only from the seed, the "
        "initial context, or your own inference.",
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
      for the delta beyond the seed and write the gated items as runbook entries.
      Set verification="success"/"failure" to trigger the LLM path.  Unverified
      success sessions are skipped.  Grounded instance_facts go to agent_memory
      even when no entry is written, and session_outcomes.distilled_at is
      stamped on every terminal path.
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
        # Every terminal path — entries written, nothing new, LLM or write
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
    """LLM distillation of a verified session (ADR 051).

    Facts go to agent_memory.  Decision rules and pitfalls that pass the gates
    become ``runbook_entries`` rows (or reinforce one), and the learned runbook
    is re-rendered.  A failure verification first counts against the entries
    the session was shown.
    """
    # Resolve any existing gap for this signature first.
    await asyncio.to_thread(resolve_gap, db_path, signature, episode_id)

    retired = 0
    if verification == "failure":
        shown_before = await asyncio.to_thread(_session_created_at, db_path, episode_id)
        try:
            retired = await asyncio.to_thread(
                _record_entry_failures, db_path, signature, shown_before
            )
        except sqlite3.Error as exc:
            log.warning(
                "distillation_failure_count_failed", signature=signature, error=str(exc)
            )
        if retired:
            log.info("distillation_entries_retired", signature=signature, count=retired)

    existing = await asyncio.to_thread(_load_entries, db_path, signature)
    seed_sig, db_seed_text = await asyncio.to_thread(
        _fetch_seed_for_signature, db_path, signature
    )
    seed_text = seed_text or db_seed_text

    prompt = _build_prompt(
        signature=signature,
        trigger=trigger,
        outcome=outcome,
        initial_context=initial_context,
        episode_stub=episode_stub,
        step_trace=_format_step_trace(steps),
        seed_text=seed_text,
        known_entries=[e["text"] for e in existing if e["kind"] in _VERDICT_KINDS],
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

    delta: Optional[DistilledDelta] = None
    try:
        response = await asyncio.wait_for(
            _client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options=_options,
                format=DistilledDelta.model_json_schema(),
                think=_think,
            ),
            timeout=timeout_secs,
        )
        content = response["message"]["content"]
        delta = DistilledDelta.model_validate_json(content)
    except Exception as exc:
        log.warning(
            "distillation_llm_failed",
            signature=signature,
            error=str(exc),
            error_type=type(exc).__name__,
            timeout_secs=round(timeout_secs, 1),
        )

    kept: list[tuple[str, str, Optional[str]]] = []
    if delta is not None:
        facts = _grounded_facts(delta.instance_facts, steps)
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
                    "distillation_facts_write_failed",
                    signature=signature,
                    error=str(exc),
                )

        evidence = _trace_evidence(steps, correction)
        known_tools = _known_tool_names()
        for kind, items in (
            ("decision_rule", delta.decision_rules),
            ("pitfall", delta.pitfalls),
        ):
            for item in items:
                text = " ".join(item.text.split())
                reason = _gate_item(
                    text,
                    evidence=evidence,
                    seed_text=seed_text,
                    wildcard_seed=not seed_sig or "*" in seed_sig,
                    known_tools=known_tools,
                )
                if reason is not None:
                    log.info(
                        "distillation_entry_rejected",
                        signature=signature,
                        kind=kind,
                        reason=reason,
                        text=text[:200],
                    )
                    continue
                kept.append((kind, text, _first_tool(text, known_tools)))

    if kept:
        try:
            results = await asyncio.to_thread(
                _write_entries, db_path, signature, episode_id, kept
            )
        except sqlite3.Error as exc:
            log.warning(
                "distillation_entry_write_failed", signature=signature, error=str(exc)
            )
            results = []
        for (kind, _, _), (action, text) in zip(kept, results):
            log.info(
                "distillation_entry_written",
                signature=signature,
                kind=kind,
                action=action,
                text=text[:200],
            )
    elif delta is not None:
        log.info(
            "distillation_skipped",
            reason="no_new_knowledge",
            signature=signature,
            episode_id=episode_id,
        )

    if kept or retired:
        try:
            await asyncio.to_thread(
                render_learned_runbook, db_path, signature, knowledge_store
            )
        except sqlite3.Error as exc:
            log.warning(
                "learned_runbook_render_failed", signature=signature, error=str(exc)
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
# Atomic runbook entries (ADR 051)
# ---------------------------------------------------------------------------

_VERDICT_KINDS = ("decision_rule", "pitfall")
# Share of an item's content tokens already present in the seed (or an existing
# entry) above which the item is a restatement, not new knowledge.
_OVERLAP_THRESHOLD = 0.6
_RENDER_MAX_PER_KIND = 8
_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")
# snake_case words not part of a dotted id (sensor.noaa_tides_2) or a longer word.
_TOOL_TOKEN_RE = re.compile(r"(?<![\w.])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?!\w|\.\w)")
_LEARNED_TITLE_PREFIX = "Learned: "


# Words that frame an item ("the decisive factor was…, which the session
# treated as…") without carrying its content; they would dilute the overlap.
_OVERLAP_FILLER = frozenset(
    "decisive factor session treated rather showing shows showed issue because "
    "likely initially concluded conclude".split()
)


def _content_tokens(text: str) -> set[str]:
    return {
        low
        for t in _FACT_TOKEN_RE.findall(text)
        if len(t) >= 3
        and (low := t.lower()) not in _FACT_STOPWORDS
        and low not in _OVERLAP_FILLER
    }


def _overlaps(text: str, corpus: str, threshold: float = _OVERLAP_THRESHOLD) -> bool:
    """True when at least *threshold* of *text*'s content tokens appear in *corpus*.

    Case-insensitive; stopwords and tokens under 3 chars are ignored.  Text with
    no content tokens never overlaps.  Pure.
    """
    tokens = _content_tokens(text)
    if not tokens:
        return False
    covered = tokens & _content_tokens(corpus)
    return len(covered) >= threshold * len(tokens)


@functools.lru_cache(maxsize=1)
def _known_tool_names() -> frozenset[str]:
    """Names of every tool an automated or chat session can call."""
    from utils.agent.tool_registry import (
        build_chat_tool_registry,
        build_ha_tool_registry,
        build_netalertx_tool_registry,
    )

    names: set[str] = set()
    for build in (
        build_ha_tool_registry,
        build_chat_tool_registry,
        build_netalertx_tool_registry,
    ):
        names.update(build().names())
    return frozenset(names)


def _unknown_tools(text: str, known: frozenset[str]) -> list[str]:
    """Return tool-like names in *text* that are not registered tools.  Pure.

    A snake_case word counts as a tool name when a call follows it (``x(``) or it
    starts with a verb some registered tool starts with (``get_``, ``read_``…).
    Slugs such as ``core_mosquitto`` and dotted entity ids are not tool names.
    """
    prefixes = {n.split("_", 1)[0] for n in known}
    unknown: list[str] = []
    for m in _TOOL_TOKEN_RE.finditer(text):
        tok = m.group(1)
        if tok in known:
            continue
        if text[m.end() : m.end() + 1] == "(" or tok.split("_", 1)[0] in prefixes:
            unknown.append(tok)
    return unknown


def _first_tool(text: str, known: frozenset[str]) -> Optional[str]:
    for m in _TOOL_TOKEN_RE.finditer(text):
        if m.group(1) in known:
            return m.group(1)
    return None


def _trace_evidence(steps: list[AgentStep], correction: Optional[str]) -> str:
    """Every step's call, output and error, plus the user's correction."""
    parts = [
        f"{s.tool_call.name}({json.dumps(s.tool_call.arguments)})\n"
        f"{s.tool_result.output or ''}\n{s.tool_result.error or ''}"
        for s in steps
    ]
    if correction:
        parts.append(correction)
    return "\n".join(parts)


def _gate_item(
    text: str,
    *,
    evidence: str,
    seed_text: str,
    wildcard_seed: bool,
    known_tools: frozenset[str],
) -> Optional[str]:
    """Return why *text* must not become an entry, or None when it passes.  Pure.

    Gates, in order: grounded in the trace or correction; not a restatement of
    the seed; no version string when the seed is a wildcard (or absent); every
    tool it names is registered.
    """
    if not _fact_is_grounded(text, evidence):
        return "ungrounded"
    if seed_text and _overlaps(text, seed_text):
        return "seed_overlap"
    if wildcard_seed and _VERSION_RE.search(text):
        return "version_pinned"
    if _unknown_tools(text, known_tools):
        return "unknown_tool"
    return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_entries(db_path: str, signature: str) -> list[dict]:
    """Return the live (not retired) entries for *signature*.  Synchronous."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, kind, text, tool, episode_refs, success_count,"
                " failure_count, created_at FROM runbook_entries"
                " WHERE signature = ? AND retired_at IS NULL"
                " ORDER BY created_at, id",
                (signature,),
            ).fetchall()
    except sqlite3.Error:
        return []
    entries = []
    for r in rows:
        e = dict(r)
        try:
            e["episode_refs"] = json.loads(e["episode_refs"] or "[]")
        except ValueError:
            e["episode_refs"] = []
        entries.append(e)
    return entries


def _same_entry(kind: str, text: str, entry: dict) -> bool:
    """Procedure notes are deterministic, so they match exactly; verdict items
    match on token overlap."""
    if entry["kind"] != kind:
        return False
    if kind == "procedure":
        return " ".join(text.lower().split()) == " ".join(entry["text"].lower().split())
    return _overlaps(text, entry["text"])


def _write_entries(
    db_path: str,
    signature: str,
    episode_id: str,
    items: list[tuple[str, str, Optional[str]]],
) -> list[tuple[str, str]]:
    """Insert each (kind, text, tool) item, or reinforce a near-duplicate entry.

    Reinforcing adds one to ``success_count`` and appends *episode_id* to the
    entry's refs, once per episode.  Returns ``(action, text)`` pairs, where
    action is ``inserted``, ``reinforced`` or ``unchanged``.  Synchronous.
    """
    entries = _load_entries(db_path, signature)
    now = _now_iso()
    results: list[tuple[str, str]] = []
    with sqlite3.connect(db_path) as conn:
        for kind, text, tool in items:
            match = next((e for e in entries if _same_entry(kind, text, e)), None)
            if match is None:
                entry = {
                    "id": str(uuid.uuid4()),
                    "kind": kind,
                    "text": text,
                    "tool": tool,
                    "episode_refs": [episode_id] if episode_id else [],
                    "success_count": 1,
                    "failure_count": 0,
                    "created_at": now,
                }
                conn.execute(
                    "INSERT INTO runbook_entries (id, signature, kind, text, tool,"
                    " episode_refs, success_count, failure_count, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                    (
                        entry["id"],
                        signature,
                        kind,
                        text,
                        tool,
                        json.dumps(entry["episode_refs"]),
                        now,
                        now,
                    ),
                )
                entries.append(entry)
                results.append(("inserted", text))
            elif episode_id and episode_id not in match["episode_refs"]:
                match["episode_refs"].append(episode_id)
                match["success_count"] += 1
                conn.execute(
                    "UPDATE runbook_entries SET success_count = ?, episode_refs = ?,"
                    " updated_at = ? WHERE id = ?",
                    (
                        match["success_count"],
                        json.dumps(match["episode_refs"]),
                        now,
                        match["id"],
                    ),
                )
                results.append(("reinforced", match["text"]))
            else:
                results.append(("unchanged", match["text"]))
        conn.commit()
    return results


def _session_created_at(db_path: str, episode_id: str) -> str:
    """When the session ran (its session_outcomes row), or now when unknown."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT created_at FROM session_outcomes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.Error:  # nosec B110
        pass
    return _now_iso()


def _record_entry_failures(db_path: str, signature: str, shown_before: str) -> int:
    """Count a failed verification against the verdict entries the session saw.

    Entries created at or before *shown_before* (the session's start) were in
    its pre-injected runbook.  An entry is retired once its failures reach its
    successes plus one.  Returns the number of entries retired.  Synchronous.
    """
    now = _now_iso()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE runbook_entries SET failure_count = failure_count + 1,"
            " updated_at = ? WHERE signature = ? AND kind IN (?, ?)"
            " AND retired_at IS NULL AND created_at <= ?",
            (now, signature, *_VERDICT_KINDS, shown_before),
        )
        cur = conn.execute(
            "UPDATE runbook_entries SET retired_at = ?, updated_at = ?"
            " WHERE signature = ? AND retired_at IS NULL"
            " AND failure_count >= success_count + 1",
            (now, now, signature),
        )
        conn.commit()
        return cur.rowcount


def _render_approach(entries: list[dict]) -> str:
    """Render live entries as runbook sections, best-supported first.  Pure.

    ``## Procedure notes`` comes last so ``_split_procedure_notes`` separates it.
    Empty sections are omitted.
    """
    sections = []
    for kind, header, cap in (
        ("decision_rule", "## Decision rules", _RENDER_MAX_PER_KIND),
        ("pitfall", "## Pitfalls", _RENDER_MAX_PER_KIND),
        ("procedure", _PROCEDURE_NOTES_HEADER, _PROCEDURE_MAX_NOTES),
    ):
        items = sorted(
            (e for e in entries if e["kind"] == kind),
            key=lambda e: (e["failure_count"] - e["success_count"], e["created_at"]),
        )[:cap]
        if items:
            sections.append(header + "\n" + "\n".join(f"- {e['text']}" for e in items))
    return "\n\n".join(sections)


def render_learned_runbook(
    db_path: str,
    signature: str,
    knowledge_store: KnowledgeStoreClientProtocol,
) -> Optional[str]:
    """Rebuild the learned runbook for *signature* from its live entries.

    Updates (or inserts) the signature's candidate/validated ``agent_strategies``
    row, keeping its state, and syncs it to Chroma.  When no live entries remain
    the row is discarded and removed from Chroma.  Pre-inject, kb_contributor and
    ``/runbooks`` read ``approach`` as before.  Returns the strategy id, or None
    when nothing is rendered.  Synchronous — call via asyncio.to_thread.
    """
    from utils.knowledge.strategy_seeder import sync_strategy_to_chroma

    entries = _load_entries(db_path, signature)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM agent_strategies WHERE signature = ?"
            " AND runbook_state IN ('candidate', 'validated')"
            " ORDER BY version DESC LIMIT 1",
            (signature,),
        ).fetchone()
        if not entries:
            if row is None:
                return None
            conn.execute(
                "UPDATE agent_strategies SET runbook_state = 'discarded' WHERE id = ?",
                (row[0],),
            )
            conn.commit()
        else:
            approach = _render_approach(entries)
            refs: list[str] = []
            for e in entries:
                refs.extend(r for r in e["episode_refs"] if r not in refs)
            title = _LEARNED_TITLE_PREFIX + signature
            if row is not None:
                strategy_id = row[0]
                conn.execute(
                    "UPDATE agent_strategies SET title = ?, approach = ?,"
                    " episode_refs = ?, version = COALESCE(version, 1) + 1"
                    " WHERE id = ?",
                    (title, approach, json.dumps(refs), strategy_id),
                )
            else:
                strategy_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO agent_strategies"
                    " (id, title, trigger_pattern, approach, runbook_state,"
                    "  created_at, signature, version, episode_refs)"
                    " VALUES (?, ?, ?, ?, 'candidate', datetime('now'), ?, 1, ?)",
                    (
                        strategy_id,
                        title,
                        signature,
                        approach,
                        signature,
                        json.dumps(refs),
                    ),
                )
            conn.commit()
    if not entries:
        try:
            knowledge_store.delete_ids("strategies", [row[0]])
        except Exception as exc:  # nosec B110
            log.warning(
                "learned_runbook_chroma_failed", signature=signature, error=str(exc)
            )
        log.info("learned_runbook_emptied", signature=signature, strategy_id=row[0])
        return None
    if not sync_strategy_to_chroma(strategy_id, db_path, knowledge_store):
        log.warning("learned_runbook_chroma_failed", signature=signature)
    log.info(
        "learned_runbook_rendered",
        signature=signature,
        strategy_id=strategy_id,
        entries=len(entries),
    )
    return strategy_id


# ---------------------------------------------------------------------------
# Procedural distillation lane (ADR 049, amended by ADR 051)
# ---------------------------------------------------------------------------


async def distill_procedure(
    signature: str,
    steps: list[AgentStep],
    outcome: str,
    episode_id: str,
    db_path: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
) -> None:
    """Record the session's tool-use notes as ``procedure`` entries (ADR 049/051).

    Runs without waiting for verification — it captures HOW evidence was
    gathered, never WHAT was concluded.  Notes come only from the deterministic
    extractor; there is no LLM call.  No ``distilled_at`` stamp (that belongs
    to the verdict lane so recovery never skips it).
    """
    if not signature or not steps or not db_path:
        return

    notes = extract_procedure_facts(steps)
    await asyncio.to_thread(_record_tool_quirks, db_path, signature, steps)
    if not notes:
        log.info(
            "procedure_distillation_no_lessons",
            signature=signature,
            episode_id=episode_id,
        )
        return

    tool_by_step = {s.step_number: s.tool_call.name for s in steps}
    items: list[tuple[str, str, Optional[str]]] = [
        ("procedure", n.lesson, tool_by_step.get(n.evidence_step)) for n in notes
    ]
    try:
        results = await asyncio.to_thread(
            _write_entries, db_path, signature, episode_id, items
        )
        await asyncio.to_thread(
            render_learned_runbook, db_path, signature, knowledge_store
        )
    except sqlite3.Error as exc:
        log.warning(
            "procedure_distillation_persist_failed",
            signature=signature,
            error=str(exc),
        )
        return
    log.info(
        "procedure_distillation_done",
        signature=signature,
        episode_id=episode_id,
        outcome=outcome,
        notes=len(notes),
        inserted=sum(1 for a, _ in results if a == "inserted"),
    )


async def enqueue_procedural_distillation(
    episode_id: str,
    db_path: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
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
    _outcome = outcome

    async def _run_procedure() -> None:
        await distill_procedure(
            signature=_sig,
            steps=steps,
            outcome=_outcome,
            episode_id=_ep,
            db_path=_db,
            knowledge_store=_store,
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
