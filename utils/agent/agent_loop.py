"""Agent loop controller — item 44.

AgentLoop drives iterative tool-calling with Ollama's tools API.
The model investigates and acts until it calls finish_repair, exhausts its
budget, or the wall-clock timeout fires.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

from config import (
    AGENT_LLM_LATENCY_LOOKBACK,
    AGENT_LLM_LATENCY_PERCENTILE,
    AGENT_MAX_EXTENSION_CALLS,
    AGENT_MAX_TOOL_CALLS,
    AGENT_MAX_TOOL_RESULT_TOKENS,
    AGENT_MAX_TOTAL_CALLS,
    AGENT_PER_CALL_MAX_TIMEOUT_SECONDS,
    AGENT_PER_CALL_MIN_TIMEOUT_SECONDS,
    AGENT_PER_CALL_TIMEOUT_FACTOR,
    LLM_PROVIDER,
    RAG_MIN_SCORE,
)
from utils.ha.context_sanitizer import sanitize_update_context
from utils.llm.model_options import ModelCallOptions, derive_call_options
from utils.core.logging import get_logger
from utils.core.prompts import load_agent_prompt, load_prompt
from utils.agent.tool_registry import AgentLoopResult, AgentStep, ToolCall, ToolResult
from utils.agent.tool_result_guardrail import ToolResultGuardrail
from utils.debug.capture import ToolCallRecord

_UNSET = object()  # sentinel for provider-aware model default

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol, LLMClientProtocol
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import ToolRegistry

log = get_logger("agent_loop")

_TRIGGER_LABELS: dict[str, str] = {
    "ha_log": "Error in HA logs",
    "ha_config": "HA config check",
    "lovelace_poll": "Lovelace entity change",
    "impact_analysis": "Update impact analysis",
    "config_analysis": "Config analysis",
    "investigation": "User-requested investigation",
    "health_diagnosis": "NetAlertX health check",
    "netalertx": "NetAlertX device event",
    "installer_diagnosis": "NetAlertX installer",
    "gap_detection": "Capability gap detected",
    "manual": "Chat session",
    "escalated": "Cloud escalation",
}


class LimitReviewDecision(BaseModel):
    reason_limit_hit: str = Field(description="Why the limit was reached")
    can_resolve_with_more: bool = Field(
        description="Would more budget/time lead to a solution?"
    )
    additional_calls_requested: int = Field(
        default=0, description="How many more tool calls needed (0 if giving up)"
    )
    summary_if_giving_up: str = Field(
        default="", description="What was learned, if cannot resolve"
    )


def _format_step_trace(steps: list["AgentStep"]) -> str:
    """One line per step, truncated to 80 chars each."""
    lines = []
    for s in steps:
        status = "OK" if s.tool_result.success else "ERR"
        detail = (s.tool_result.output or s.tool_result.error or "")[:60]
        lines.append(f"  {s.step_number}. {s.tool_call.name} → {status}: {detail}")
    return "\n".join(lines) if lines else "  (no tool calls)"


# Tools exempt from the repeat guard: side-effect tools and terminal tools.
_REPEAT_GUARD_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {"discard_result", "request_escalation", "remember"}
)

# Per-chunk token budget for pre-injected knowledge (semantic chunks, top_k=3 → ~900 total).
_PRE_INJECT_CHUNK_TOKENS = 300
# Per-runbook budget for exact-signature matches (seed + learned). Each gets this budget
# so neither is truncated on common runbook lengths (~800–1200 tokens uncut).
_PRE_INJECT_RUNBOOK_TOKENS = 1200


def _format_knowledge_block(chunks: list[Any]) -> str:
    """Render pre-injected knowledge chunks in rank order.

    Each chunk body is head-truncated to its own budget, so the header and the
    highest-ranked chunk are never dropped by a later, lower-ranked one.
    """
    from utils.agent.tool_executor import ToolExecutor as _TE
    from utils.core.context import truncate_to_budget

    parts = ["Knowledge context:"]
    for chunk in chunks:
        col = getattr(chunk, "collection", "")
        cid = getattr(chunk, "chunk_id", "")
        label = _TE._knowledge_authority_label(col, getattr(chunk, "metadata", {}), cid)
        source = getattr(chunk, "source", "")
        # Exact-signature runbooks (seed + learned) get a larger budget so they're not cut.
        is_sig_match = getattr(chunk, "metadata", {}).get("exact_match") or getattr(
            chunk, "metadata", {}
        ).get("sig_match")
        budget = (
            _PRE_INJECT_RUNBOOK_TOKENS if is_sig_match else _PRE_INJECT_CHUNK_TOKENS
        )
        body = truncate_to_budget(chunk.text, budget, "head")
        if len(body) < len(chunk.text):
            body += " …[truncated]"
        parts.append(f"\n{label} [{col} | {source}]\n{body}")
    return "\n".join(parts)


from utils.knowledge.runbook_distiller import _sig_specificity  # noqa: E402

import re as _re  # noqa: E402 — used only by _clean_seed_approach

_FRONT_MATTER_STRIP_RE = _re.compile(r"^---\n.*?\n---\s*\n?", _re.DOTALL)
_LEADING_H1_RE = _re.compile(r"^#[^\n]*\n*")


def _clean_seed_approach(approach: str, title: str) -> str:
    """Strip YAML front matter and a duplicate leading H1 from a seed approach text.

    Seeds are stored verbatim (including front matter) so that ``only_changed``
    diffing works in strategy_seeder.  Strip the front matter here so it does not
    appear in the injected chunk, and strip a leading ``# <title>`` that would
    duplicate the H1 the caller prepends.
    """
    text = _FRONT_MATTER_STRIP_RE.sub("", approach, count=1).lstrip("\n")
    m = _LEADING_H1_RE.match(text)
    if m:
        h1 = text[: m.end()].strip().lstrip("#").strip()
        if h1 == title:
            text = text[m.end() :]
    return text


def _fetch_signature_chunks(db_path: str, signature: str) -> "list[Any]":
    """Return up to two KnowledgeChunks for *signature*: best seed + best learned runbook.

    Wildcard matching: stored patterns like ``repair:*:restart_required`` match a query
    of ``repair:hacs:restart_required``.  Specificity = number of non-wildcard parts that
    matched; most specific wins.  Within equal specificity, state priority is
    seed > validated > candidate.

    Returns a list (0–2 items).  Each chunk carries ``metadata["sig_match"]=True`` and
    ``metadata["exact_match"]=True`` so ``_knowledge_authority_label`` emits
    ``[RUNBOOK FOR THIS EXACT SYMPTOM]``.
    """
    import sqlite3

    from utils.knowledge.knowledge_store import KnowledgeChunk

    if not db_path or not signature or ":" not in signature:
        return []

    _STATE_ORDER = {"seed": 0, "validated": 1, "candidate": 2}

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, approach, runbook_state, signature"
                " FROM agent_strategies"
                " WHERE signature IS NOT NULL"
                "   AND runbook_state IN ('seed', 'validated', 'candidate')"
            ).fetchall()
    except Exception:  # nosec B110
        return []

    # Score each row; keep best seed and best learned runbook separately.
    best_seed: Optional[tuple[int, int, sqlite3.Row]] = (
        None  # (specificity, state, row)
    )
    best_learned: Optional[tuple[int, int, sqlite3.Row]] = None

    for row in rows:
        stored_sig = row["signature"] or ""
        matches, spec = _sig_specificity(stored_sig, signature)
        if not matches:
            continue
        state_order = _STATE_ORDER.get(row["runbook_state"] or "", 99)
        if row["runbook_state"] == "seed":
            if best_seed is None or (spec, -state_order) > (
                best_seed[0],
                -best_seed[1],
            ):
                best_seed = (spec, state_order, row)
        else:
            if best_learned is None or (spec, -state_order) > (
                best_learned[0],
                -best_learned[1],
            ):
                best_learned = (spec, state_order, row)

    from utils.knowledge.runbook_distiller import _split_procedure_notes

    chunks: list[Any] = []
    for entry in (best_seed, best_learned):
        if entry is None:
            continue
        _, _, row = entry
        title = row["title"] or signature
        approach = row["approach"] or ""
        state = row["runbook_state"] or "candidate"

        # Seeds are stored verbatim (front matter + H1 + body); strip the front
        # matter and any leading duplicate H1 before injecting.
        if row["runbook_state"] == "seed":
            approach = _clean_seed_approach(approach, title)

        # Split procedure notes out so they get their own [PROCEDURE NOTES] label.
        main_approach, proc_notes = _split_procedure_notes(approach)
        text = f"# {title}\n\n{main_approach}".strip()
        if main_approach.strip():
            chunks.append(
                KnowledgeChunk(
                    text=text,
                    source=f"sig:{row['id']}",
                    collection="strategies",
                    score=1.0,
                    metadata={
                        "exact_match": True,
                        "sig_match": True,
                        "runbook_type": state,
                    },
                    authority_score=1.0,
                    chunk_id=row["id"],
                )
            )
        if proc_notes:
            chunks.append(
                KnowledgeChunk(
                    text=proc_notes,
                    source=f"proc:{row['id']}",
                    collection="strategies",
                    score=1.0,
                    metadata={
                        "procedure_note": True,
                        "sig_match": True,
                    },
                    authority_score=0.9,
                    chunk_id=f"proc:{row['id']}",
                )
            )
    return chunks


def _fetch_memory_chunks(db_path: str, signature: str) -> "list[Any]":
    """Return agent_memory rows whose signature matches as [INSTANCE FACT] chunks.

    Uses the same wildcard matching as _fetch_signature_chunks.  Returns at most 5
    rows, ordered newest first.  Added in V40 (agent_memory.signature column).
    Falls back gracefully if the column does not exist yet.
    """
    import sqlite3

    from utils.knowledge.knowledge_store import KnowledgeChunk

    if not db_path or not signature or ":" not in signature:
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT key, content, source, ts, signature"
                    " FROM agent_memory"
                    " WHERE signature IS NOT NULL"
                    " ORDER BY ts DESC LIMIT 50"
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # column not yet added (pre-V40)
    except Exception:  # nosec B110
        return []

    chunks: list[Any] = []
    for row in rows:
        stored_sig = row["signature"] or ""
        matches, _ = _sig_specificity(stored_sig, signature)
        if not matches:
            continue
        key = row["key"] or ""
        content = row["content"] or ""
        text = f"[{key}] {content}".strip()
        chunks.append(
            KnowledgeChunk(
                text=text,
                source=f"memory:{key}",
                collection="agent_memory",
                score=1.0,
                metadata={"instance_fact": True},
                authority_score=0.9,
                chunk_id=f"mem:{key}",
            )
        )
        if len(chunks) >= 5:
            break
    return chunks


_PRIOR_SESSION_WINDOW_DAYS = 14
_PRIOR_SESSION_LIMIT = 3
_PRIOR_SESSION_TEXT_CHARS = 300


def _fetch_prior_sessions(
    db_path: str, subject_keys: list[str], now: Optional[float] = None
) -> str:
    """Return a [PREVIOUS INVESTIGATION] block for recent sessions on the same subject.

    Lists up to _PRIOR_SESSION_LIMIT session_outcomes rows from the last
    _PRIOR_SESSION_WINDOW_DAYS, newest first, with what each concluded and
    whether anyone confirmed it.  Returns "" when there are none.
    """
    import json
    import sqlite3

    keys = [k for k in subject_keys if k]
    if not db_path or not keys:
        return ""
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime((now or time.time()) - _PRIOR_SESSION_WINDOW_DAYS * 86400),
    )
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT created_at, conclusion_json, verification, correction"
                " FROM session_outcomes"
                f" WHERE subject_key IN ({','.join('?' * len(keys))})"  # nosec B608
                "   AND created_at >= ?"
                " ORDER BY created_at DESC LIMIT ?",
                (*keys, cutoff, _PRIOR_SESSION_LIMIT),
            ).fetchall()
    except Exception:  # nosec B110
        return ""
    lines: list[str] = []
    for created_at, conclusion_json, verification, correction in rows:
        try:
            conclusion = json.loads(conclusion_json or "{}") or {}
        except (TypeError, ValueError):
            conclusion = {}
        text = (
            conclusion.get("root_cause") or conclusion.get("summary") or ""
        ).strip() or "(no conclusion recorded)"
        if len(text) > _PRIOR_SESSION_TEXT_CHARS:
            text = text[:_PRIOR_SESSION_TEXT_CHARS] + "…"
        when = str(created_at or "")[:16].replace("T", " ") + " UTC"
        line = f"[PREVIOUS INVESTIGATION] {when}: {text} (verification: {verification or 'pending'})"
        if correction:
            line += f" — user correction: {str(correction)[:200]}"
        lines.append(line)
    if not lines:
        return ""
    return (
        "Earlier sessions on this same subject. pending/unverified means nobody"
        " confirmed the conclusion; failure means it was wrong. Re-check, don't"
        " repeat it:\n" + "\n".join(lines)
    )


def _parse_content_as_tool_call(
    content: str, known_names: set[str]
) -> list[dict] | None:
    """Fallback for models that put tool calls in content instead of tool_calls.

    Some models (e.g. qwen2.5-coder:7b) respond with a JSON object like
    {"name": "read_config", "arguments": {}} in the content field rather than
    a proper tool_calls list.  If the content parses as a single tool call
    whose name is in the known registry, synthesise a tool_calls entry so the
    loop can dispatch it normally.  Returns None for ordinary text content.
    """
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    name = parsed.get("name", "")
    args = parsed.get("arguments")
    if name in known_names and isinstance(args, dict):
        return [{"function": {"name": name, "arguments": args}}]
    return None


_UNSET_PROMPT = object()  # sentinel for auto-loaded, terminal-tool-aware default

# Primary text field for each terminal tool; used in nudge messages so the model
# knows which field to pass its answer into.
_TERMINAL_TOOL_PRIMARY_FIELD: dict[str, str] = {
    "finish_repair_issue": "human_explanation",
    "finish_notification_investigation": "human_explanation",
    "finish_update_analysis": "recommendation",
}


def _derive_loop_call_options(model_name: str) -> ModelCallOptions:
    """Derive ModelCallOptions for an agent loop session.

    Called in a thread (via asyncio.to_thread) to avoid blocking the event loop
    during the subprocess calls in detect_local_hardware() and list_ollama_models().
    """
    import config as _cfg
    from utils.disk.hardware import detect_local_hardware, list_ollama_models
    from utils.agent.work_queue import get_work_queue_or_none

    if _cfg.LLM_PROVIDER == "openai_compat":
        ctx = _cfg.OPENAI_COMPAT_NUM_CTX
        return ModelCallOptions(
            think=None,
            num_ctx=ctx,
            temperature=0.0,
            keep_alive="5m",  # unused by OpenAI-compat client; harmless placeholder
        )

    hw = detect_local_hardware()
    models = list_ollama_models()
    model_info_map = {m.name: m for m in models}
    info = model_info_map.get(model_name)

    if info is not None:
        available_ram_gb = max(0.0, hw.ram_gb - info.size_gb)
        has_thinking = info.has_thinking
        recommended_temperature = info.recommended_temperature
        context_length = info.context_length
    else:
        available_ram_gb = hw.ram_gb * 0.35  # conservative fallback
        has_thinking = False
        recommended_temperature = None
        context_length = 0

    keep_alive_cfg = _cfg.OLLAMA_KEEP_ALIVE
    supervisor_mode = get_work_queue_or_none() is not None
    idle_unload_minutes = _cfg.OLLAMA_IDLE_UNLOAD_MINUTES
    if keep_alive_cfg != "auto":
        # Explicit override — translate into supervisor_mode equivalent
        supervisor_mode = keep_alive_cfg == "-1"
        idle_unload_minutes = 0  # explicit keep_alive wins; disable idle unload

    return derive_call_options(
        has_thinking=has_thinking,
        recommended_temperature=recommended_temperature,
        context_length=context_length,
        available_ram_gb=available_ram_gb,
        debug_level=_cfg.DEBUG_LEVEL,
        think_mode_cfg=_cfg.OLLAMA_THINK_MODE,
        num_ctx_override=_cfg.OLLAMA_NUM_CTX,
        supervisor_mode=supervisor_mode,
        idle_unload_minutes=idle_unload_minutes,
        one_shot=False,
    )


class AgentLoop:
    """Iterative tool-calling loop with budget and wall-clock timeout.

    Parameters
    ----------
    llm_client:
        Any object implementing LLMClientProtocol (OllamaClient in production,
        FakeToolCallingLLMClient in tests).
    tool_executor:
        ToolExecutor instance pre-loaded with SSH clients / gate / notifier.
    tool_registry:
        ToolRegistry containing the tools to expose to the model.
    model:
        Ollama model name (defaults to OLLAMA_MODEL from config).
    system_prompt:
        Override the default system prompt.
    max_tool_calls:
        Hard cap on tool invocations per run (defaults to AGENT_MAX_TOOL_CALLS).
    max_wall_seconds:
        Wall-clock timeout in seconds (defaults to AGENT_MAX_WALL_SECONDS).
    """

    def __init__(
        self,
        llm_client: "LLMClientProtocol",
        tool_executor: "ToolExecutor",
        tool_registry: "ToolRegistry",
        model: str | object = _UNSET,
        system_prompt: str | object = _UNSET_PROMPT,
        max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
        max_wall_seconds: float = 0.0,  # deprecated — ignored; per-call timeout used
        terminal_tool_name: str = "finish_repair",
        step_callback: Optional[Callable[["AgentStep"], None]] = None,
        pre_step_callback: Optional[Callable[["ToolCall"], Awaitable[None]]] = None,
        timeline_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
        trigger: str = "manual",
        activity_type: str = "",
        db_path: Optional[str] = None,
        escalated: bool = False,
        knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
        context_inject_callback: Optional[Callable[[str], None]] = None,
        on_llm_call_start: Optional[Callable[[str, str], None]] = None,
        on_llm_call_done: Optional[Callable[[str, float], None]] = None,
        capture_llm: bool = False,
    ) -> None:
        if model is _UNSET:
            from utils.llm.llm_factory import _default_model_for_provider

            model = _default_model_for_provider()
        if system_prompt is _UNSET_PROMPT:
            system_prompt = load_agent_prompt("config_repair", terminal_tool_name)
        self._llm = llm_client
        self._executor = tool_executor
        self._registry = tool_registry
        self._model: str = model  # type: ignore[assignment]
        self._system_prompt = system_prompt  # type: ignore[assignment]
        self._max_tool_calls = max_tool_calls
        self._terminal_tool_name = terminal_tool_name
        self._step_callback = step_callback
        self._pre_step_callback = pre_step_callback
        self._timeline_callback = timeline_callback
        self._trigger = trigger
        self._activity_type = activity_type
        self._db_path = db_path
        self._escalated = escalated
        self._knowledge_store = knowledge_store
        self._context_inject_callback = context_inject_callback
        self._on_llm_call_start = on_llm_call_start
        self._on_llm_call_done = on_llm_call_done
        self._capture_llm = capture_llm
        self._llm_captures: list = []
        self._tool_call_records: list[ToolCallRecord] = []
        self._seen_calls: dict[tuple[str, str], tuple[int, str]] = {}
        self._absolute_max = AGENT_MAX_TOTAL_CALLS
        self._messages: Optional[list] = None  # set during run(), cleared after
        self._episode_id: str = ""  # set at start of run()
        self._signature: str = ""  # set at start of run() from the signature= argument
        self._subject_key: str = (
            ""  # set at start of run() from the subject_key= argument
        )
        self._raw_initial_context: str = (
            ""  # pre-injection context, set at start of run()
        )
        self._top_retrieved_signature: str = (
            ""  # first strategy chunk sig from pre-inject
        )
        self._stuck_reason: str = (
            ""  # "host_sleep" when timeout was caused by Mac sleep
        )

    def inject_context(self, message: str) -> None:
        """Append a user message to the live conversation during a running loop.

        Safe to call from any coroutine in the same asyncio event loop while
        run() is executing.  The loop picks it up on the next LLM call.
        Has no effect if no loop is currently running.
        """
        if self._messages is not None:
            self._messages.append({"role": "user", "content": message})
            log.info("agent_loop_context_injected", length=len(message))

    def _build_episode(
        self,
        steps: list[AgentStep],
        episode_stub: Optional[dict],
        start_wall: float,
    ) -> dict[str, Any]:
        """Extract structured episode data from tool call history."""
        _DIAGNOSTIC_TOOLS = {
            "read_config",
            "read_logs",
            "read_file",
            "check_entity_status",
            "get_ha_components",
            "get_config_entries_all",
            "search_log",
            "run_ha_command",
            "query_netalertx",
            "get_device_info",
            "get_disk_usage",
            "read_pueo_log",
        }
        symptoms: list[str] = []
        fix_applied: Optional[str] = None
        verification_result = True

        for step in steps:
            name = step.tool_call.name
            if name in _DIAGNOSTIC_TOOLS and step.tool_result.success:
                out = (step.tool_result.output or "").strip()
                if out:
                    symptoms.append(out[:200])
            elif name == "apply_fix":
                fix_applied = step.tool_call.arguments.get("yaml_content")
            elif name == "verify_fix":
                verification_result = step.tool_result.success

        stub = episode_stub or {}
        summary = (
            stub.get("summary")
            or stub.get("human_explanation")
            or stub.get("recommendation")
            or ""
        )
        hypothesis_chain = [summary] if summary else []

        return {
            "symptoms": symptoms,
            "hypothesis_chain": hypothesis_chain,
            "fix_applied": fix_applied,
            "verification_result": verification_result,
            "duration_seconds": round(time.monotonic() - start_wall, 2),
        }

    def _record_episode(
        self,
        steps: list[AgentStep],
        episode_stub: Optional[dict],
        start_wall: float,
    ) -> str:
        """Serialize a RepairEpisode to SQLite and return its ID."""
        from utils.repair.repair_episode import RepairEpisode, serialize_episode

        data = self._build_episode(steps, episode_stub, start_wall)
        episode = RepairEpisode(
            id=self._episode_id,
            trigger=self._trigger,
            symptoms=data["symptoms"],
            tool_sequence=[step.tool_call for step in steps],
            tool_result_summaries=[
                (step.tool_result.output or step.tool_result.error or "")[:500]
                for step in steps
            ],
            hypothesis_chain=data["hypothesis_chain"],
            fix_applied=data["fix_applied"],
            verification_result=data["verification_result"],
            model_used=self._model,
            escalated=self._escalated,
            duration_seconds=data["duration_seconds"],
            initial_context=self._raw_initial_context,
            activity_type=self._activity_type,
        )
        if self._db_path is None:
            return (
                episode.id
            )  # unreachable: _record_episode only called when db_path set
        serialize_episode(self._db_path, episode)
        log.info(
            "repair_episode_recorded",
            episode_id=episode.id,
            trigger=self._trigger,
            fix_applied=episode.fix_applied is not None,
        )
        return episode.id

    def _pre_inject_ha_profile(self) -> str:
        """Return a compact HA profile block, or '' if not available.

        Best-effort — any failure returns ''.
        """
        try:
            if self._executor is None:
                return ""
            summary = self._executor.get_ha_profile_summary()
            if summary == "HA environment profile not yet available.":
                return ""
            return summary
        except Exception as exc:
            log.warning("ha_profile_injection_failed", error=str(exc))
            return ""

    async def _pre_inject_knowledge(
        self, initial_context: str, query_text: str = ""
    ) -> str:
        """Query the knowledge store and return the formatted knowledge block.

        Returns the block string, or '' when no chunks are found or on error.
        Semantic chunks are head-truncated to _PRE_INJECT_CHUNK_TOKENS in rank order.
        Exact-signature runbooks (seed + best learned) use _PRE_INJECT_RUNBOOK_TOKENS
        each so they are never cut.
        The query_text parameter should be the original user question so
        ChromaDB searches user intent rather than the injected HA profile.
        Sets executor._query_knowledge_had_results when chunks are found so that
        sessions benefiting from pre-injected context don't trigger the auto-gap.

        When self._signature is set (with a colon), _fetch_signature_chunks returns up to
        two runbooks (best seed + best learned) for the symptom using wildcard matching.
        Both are prepended before semantic results and labelled
        [RUNBOOK FOR THIS EXACT SYMPTOM].  Matching agent_memory rows are prepended as
        [INSTANCE FACT] entries (V40: signature column on agent_memory).
        """
        # Signature-matching lookup — runs before the semantic query so runbooks and
        # instance facts always appear first regardless of ChromaDB relevance scoring.
        sig_chunks: list[Any] = []
        _sig = getattr(self, "_signature", "")
        if _sig and ":" in _sig and self._db_path:
            sig_chunks = await asyncio.to_thread(
                _fetch_signature_chunks, self._db_path, _sig
            )
            if sig_chunks:
                log.debug(
                    "pre_inject_signature_match",
                    signature=_sig,
                    count=len(sig_chunks),
                    ids=[getattr(c, "chunk_id", "") for c in sig_chunks],
                )

        effective_query = (query_text or initial_context)[:500]
        try:
            chunks = await asyncio.to_thread(
                self._knowledge_store.query,  # type: ignore[union-attr]
                query_text=effective_query,
                top_k=3,
                min_score=RAG_MIN_SCORE,
            )
        except Exception as exc:
            log.warning("knowledge_injection_failed", error=str(exc))
            return ""

        # Prepend signature matches, deduplicating semantic results by chunk_id.
        sig_ids = {
            getattr(c, "chunk_id", "") for c in sig_chunks if getattr(c, "chunk_id", "")
        }
        deduped_semantic = [
            c for c in chunks if getattr(c, "chunk_id", "") not in sig_ids
        ]

        # When an exact-signature runbook is present, cap supplemental chunks to 2
        # and keep only those whose metadata domain/slug or text mentions the session
        # domain/slug.  This prevents irrelevant chunks (e.g., Xbox notes appearing in a
        # LIFX investigation) from padding the context.
        if sig_chunks and deduped_semantic:
            _sig_parts = _sig.split(":")
            _session_domain = _sig_parts[-1].lower() if len(_sig_parts) >= 2 else ""
            if _session_domain:
                _relevant = [
                    c
                    for c in deduped_semantic
                    if _session_domain
                    in (
                        getattr(c, "metadata", {}).get("impacted_integration", "") or ""
                    ).lower()
                    or _session_domain
                    in (getattr(c, "metadata", {}).get("domain", "") or "").lower()
                    or _session_domain in getattr(c, "text", "").lower()
                ]
                deduped_semantic = _relevant[:2]
            else:
                deduped_semantic = deduped_semantic[:2]

        chunks = sig_chunks + deduped_semantic

        # Fetch matching agent_memory rows as [INSTANCE FACT] chunks (V40).
        if _sig and ":" in _sig and self._db_path:
            memory_chunks = await asyncio.to_thread(
                _fetch_memory_chunks, self._db_path, _sig
            )
            if memory_chunks:
                log.debug("pre_inject_memory_facts", count=len(memory_chunks))
                chunks = memory_chunks + chunks

        if not chunks:
            return ""
        # Signal to the executor so the auto-gap logic knows KB had results.
        if self._executor is not None:
            self._executor._query_knowledge_had_results = True
            # Record pre-injected chunk ids so _query_knowledge can dedup (S2).
            for _c in chunks:
                _cid = getattr(_c, "chunk_id", "")
                if _cid:
                    self._executor._pre_injected_chunk_ids.add(_cid)
            # Collect strategy_ids for usage tracking at session end.
            for _c in chunks:
                if getattr(_c, "collection", "") == "strategies":
                    _sid = getattr(_c, "metadata", {}).get("strategy_id") or getattr(
                        _c, "chunk_id", ""
                    )
                    if _sid:
                        self._executor._injected_strategy_ids.add(_sid)
        # Track the top strategy chunk's signature for chat opt-in learning (S4b).
        # Only set it once — the first strategies chunk in rank order wins.
        if not self._top_retrieved_signature:
            for _c in chunks:
                if getattr(_c, "collection", "") == "strategies":
                    _csig = (getattr(_c, "metadata", {}) or {}).get("signature", "")
                    if _csig and ":" in _csig:
                        self._top_retrieved_signature = _csig
                        break
        repair_chunks = [
            c for c in chunks if getattr(c, "collection", "") == "repair_history"
        ]
        if repair_chunks:
            log.info("pre_inject_similar_episodes", count=len(repair_chunks))
        block = _format_knowledge_block(chunks)
        log.debug("agent_loop_knowledge_injected", runbooks_found=len(chunks))
        import config as _al_cfg

        if _al_cfg.DEBUG_LEVEL >= 3:
            log.debug(
                "knowledge_chunks", count=len(chunks), query=effective_query[:100]
            )
            log.debug("knowledge_pre_inject", block=block[:500])
        return block

    def _per_call_timeout_seconds(self) -> float:
        """Compute the per-call LLM timeout from historical latency data.

        Uses P95(recent_latency_ms) × factor, clamped to [min, max].
        Falls back to min_timeout when the db_path is absent or has < 5 samples.
        """
        from utils.llm.llm_stats import expected_timeout_ms

        provider = "local"
        try:
            from utils.llm.llm_factory import _provider_name

            provider = _provider_name()
        except Exception as exc:  # nosec B110
            log.warning("llm_provider_detection_failed", error=str(exc))
        timeout_ms = expected_timeout_ms(
            self._db_path or "",
            model=self._model,
            provider=provider,
            percentile=AGENT_LLM_LATENCY_PERCENTILE,
            lookback=AGENT_LLM_LATENCY_LOOKBACK,
            factor=AGENT_PER_CALL_TIMEOUT_FACTOR,
            min_ms=AGENT_PER_CALL_MIN_TIMEOUT_SECONDS * 1000.0,
            max_ms=AGENT_PER_CALL_MAX_TIMEOUT_SECONDS * 1000.0,
        )
        return timeout_ms / 1000.0

    def _review_timeout(self, steps: list[AgentStep], elapsed: float) -> float:
        """Adaptive timeout for the limit-review call.

        Uses 3× the observed average per-step time, clamped to [30s, 300s].
        Falls back to 60s if no steps recorded yet.
        """
        if not steps:
            return 60.0
        avg = elapsed / len(steps)
        return max(30.0, min(3.0 * avg, 300.0))

    async def _review_limit(
        self,
        messages: list[dict],
        steps: list[AgentStep],
        reason: str,
        total_calls_used: int,
        elapsed: float,
    ) -> LimitReviewDecision:
        """Ask the LLM whether to extend the budget or give up gracefully."""
        step_trace = _format_step_trace(steps)
        review_prompt = (
            f"[LIMIT REVIEW] You have hit the {reason} limit "
            f"({total_calls_used} tool calls used, {elapsed:.0f}s elapsed). "
            f"Step trace:\n{step_trace}\n\n"
            "Assess: (1) Why did you hit this limit? "
            "(2) Would additional calls lead to a solution — if so, how many? "
            "(3) If you cannot make progress, summarize what you learned."
        )
        timeout = self._review_timeout(steps, elapsed)
        review_messages = messages + [{"role": "user", "content": review_prompt}]
        log.debug(
            "limit_review_start",
            reason=reason,
            total_calls_used=total_calls_used,
            elapsed=round(elapsed, 1),
            timeout=round(timeout, 1),
            review_prompt=review_prompt[:500],
            messages_count=len(review_messages),
        )
        _review_t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._llm.chat(
                    model=self._model,
                    messages=review_messages,
                    options={"temperature": 0.0},
                    format=LimitReviewDecision.model_json_schema(),
                ),
                timeout=timeout,
            )
            decision = LimitReviewDecision.model_validate_json(
                response["message"]["content"]
            )
            log.info(
                "limit_review_decision",
                can_resolve=decision.can_resolve_with_more,
                additional_calls=decision.additional_calls_requested,
                reason_limit_hit=decision.reason_limit_hit[:120],
                summary=decision.summary_if_giving_up[:120],
            )
            if self._db_path:
                try:
                    from utils.debug.capture import record_one_shot as _ros

                    _ros(
                        "_review_limit",
                        self._model,
                        f"{reason}: {total_calls_used} calls, {elapsed:.0f}s",
                        decision.summary_if_giving_up[:500]
                        or decision.reason_limit_hit[:500],
                        (time.monotonic() - _review_t0) * 1000,
                        "extend" if decision.can_resolve_with_more else "give_up",
                        self._db_path,
                    )
                except Exception:  # nosec B110
                    pass
            return decision
        except Exception:
            log.warning("limit_review_failed", timeout_used=timeout)
            return LimitReviewDecision(
                reason_limit_hit="review failed",
                can_resolve_with_more=False,
                summary_if_giving_up="",
            )

    async def run(
        self,
        initial_context: str,
        initial_messages: Optional[list[dict]] = None,
        knowledge_query: str = "",
        signature: str = "",
        subject_key: str = "",
        prior_subject_keys: Optional[list[str]] = None,
    ) -> AgentLoopResult:
        """Run the agent loop and return a result describing what happened.

        The loop drives the model with the tool registry until one of:
        - finish_repair is called → outcome "success"
        - tool call budget exhausted → outcome "exhausted"
        - wall clock exceeded → outcome "timeout"
        - apply_fix returned failure → outcome "fix_failed"

        Parameters
        ----------
        initial_context:
            The current user message / task description.
        initial_messages:
            Optional prior conversation history as {"role", "content"} dicts.
            Inserted between the system prompt and the current user message so
            the model has proper multi-turn context.
        signature:
            Symptom-class signature (from runbook_signature.py). Used for
            exact-match runbook lookup, runbook_usage keying, and distillation
            dedup. Falls back to self._trigger when empty.
        prior_subject_keys:
            Subject keys whose recent sessions are pre-injected as
            [PREVIOUS INVESTIGATION] lines.  Defaults to ``[subject_key]``.
        """
        self._executor.reset()
        self._llm_captures = []
        self._tool_call_records = []
        self._seen_calls = {}
        self._episode_id = str(uuid.uuid4())
        if self._executor is not None:
            self._executor.set_episode_id(self._episode_id)
        self._signature = signature or self._trigger
        if self._executor is not None:
            self._executor.set_signature(self._signature)
        self._subject_key = subject_key
        self._raw_initial_context = initial_context

        # Use the caller-supplied knowledge_query for a more targeted ChromaDB search
        # (e.g. update manager passes "<component> <from>→<to> update breaking changes").
        # Fall back to the start of initial_context if none supplied.
        _effective_knowledge_query = knowledge_query or initial_context[:500]

        _knowledge_block = ""
        if self._knowledge_store is not None:
            _knowledge_block = await self._pre_inject_knowledge(
                initial_context, query_text=_effective_knowledge_query
            )
            if _knowledge_block and self._context_inject_callback is not None:
                self._context_inject_callback(_knowledge_block[:1000])

        _prior_block = ""
        if self._db_path:
            _prior_block = await asyncio.to_thread(
                _fetch_prior_sessions,
                self._db_path,
                (
                    prior_subject_keys
                    if prior_subject_keys is not None
                    else [subject_key]
                ),
            )

        _profile_block = self._pre_inject_ha_profile()

        # Assemble first user message: profile → knowledge → prior → trigger (last).
        # Trigger is placed last so it receives the highest recency weight.
        _parts: list[str] = []
        if _profile_block:
            _parts.append(f"<ha_profile>\n{_profile_block}\n</ha_profile>")
        if _knowledge_block:
            _parts.append(f"<knowledge>\n{_knowledge_block}\n</knowledge>")
        if _prior_block:
            _parts.append(
                f"<previous_investigations>\n{_prior_block}\n</previous_investigations>"
            )
        _parts.append(f"<trigger>\n{initial_context}\n</trigger>")
        _assembled_context = "\n\n".join(_parts)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
        ]
        if initial_messages:
            messages.extend(initial_messages)
        messages.append({"role": "user", "content": _assembled_context})
        self._messages = messages  # expose for inject_context()
        tools = self._registry.get_ollama_tools()
        steps: list[AgentStep] = []
        tool_call_count = 0
        start_time = time.monotonic()
        outcome: str = "exhausted"
        episode_stub: Optional[dict] = None

        _tl_label = _TRIGGER_LABELS.get(self._trigger, self._trigger)
        _tl_activity = self._activity_type or self._trigger
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "INFO",
                "agent_loop",
                f"{_tl_label} — {_tl_activity} started",
                detail={
                    "trigger": self._trigger,
                    "activity_type": _tl_activity,
                    "model": self._model,
                    "episode_id": self._episode_id,
                },
            )
        except Exception:  # nosec B110
            pass

        try:
            result = await self._loop_body(
                messages, tools, steps, tool_call_count, start_time
            )
            outcome, episode_stub = result
        except asyncio.TimeoutError:
            # A per-call LLM timeout fired — the LLM call stalled beyond its
            # expected latency.  Treat this as "stuck" (not a wall-clock budget
            # overrun) so callers can distinguish a slow-but-working LLM from a
            # genuinely hung one.
            elapsed = time.monotonic() - start_time
            log.warning(
                "agent_loop_stuck",
                steps=len(steps),
                elapsed=round(elapsed, 2),
            )
            decision = await self._review_limit(
                messages, steps, "stuck", len(steps), elapsed
            )
            if decision.summary_if_giving_up and episode_stub is None:
                episode_stub = {"summary": decision.summary_if_giving_up}
            outcome = "stuck"
        except Exception as exc:
            # Unexpected crash (e.g. HTTP 500 from Ollama, XML parse error).
            # Capture as "failed" so the episode writer still runs and callers
            # can handle it through the normal non-success path instead of
            # propagating an unhandled exception to the work queue.
            elapsed = time.monotonic() - start_time
            log.error(
                "agent_loop_crash",
                error=str(exc),
                error_type=type(exc).__name__,
                steps=len(steps),
                elapsed=round(elapsed, 2),
            )
            outcome = "failed"

        log.info(
            "agent_loop_complete",
            outcome=outcome,
            steps=len(steps),
            elapsed=round(time.monotonic() - start_time, 2),
        )

        if outcome == "success" and self._db_path is not None:
            try:
                await asyncio.to_thread(
                    self._record_episode, steps, episode_stub, start_time
                )
            except Exception as exc:  # nosec B110
                log.error("repair_episode_record_failed", error=str(exc))

        # Record runbook usage for the declared runbook (or fallback to all seen).
        # Use getattr to stay safe when executor is a replay stub without these attrs.
        if self._db_path is not None and self._executor is not None:
            _seen_ids: set = getattr(self._executor, "_injected_strategy_ids", set())
            _declared: "Optional[str]" = getattr(
                self._executor, "_declared_runbook_id", None
            )
            if _seen_ids or _declared:
                _signature = self._signature
                try:
                    await asyncio.to_thread(
                        self._executor.record_runbook_usage,
                        set(_seen_ids),
                        outcome,
                        self._episode_id,
                        _signature,
                        _declared,
                    )
                except Exception as exc:  # nosec B110
                    log.warning("runbook_usage_record_failed", error=str(exc))

        _duration = round(time.monotonic() - start_time, 2)
        self._messages = None  # loop finished; disable inject_context
        _tl_level = "INFO" if outcome == "success" else "WARN"
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                _tl_level,
                "agent_loop",
                f"{_tl_label} — {outcome}",
                detail={
                    "outcome": outcome,
                    "steps": len(steps),
                    "duration_seconds": _duration,
                    "episode_id": self._episode_id,
                },
            )
        except Exception:  # nosec B110
            pass

        if self._capture_llm and self._llm_captures and outcome != "success":
            self._llm_captures[-1].outcome_path = "exhaustion_fallback"

        debug_log_path: Optional[str] = None
        if self._capture_llm and self._llm_captures:
            try:
                from utils.debug.episode_writer import write_episode_html
                from paths import get_dirs as _get_dirs
                import datetime as _datetime

                _ep_dir = (
                    _get_dirs().data_dir
                    / "debug_episodes"
                    / f"{self._trigger}_{self._episode_id}"
                )
                _session_meta = {
                    "session_id": self._episode_id,
                    "outcome": outcome,
                    "model": self._model,
                    "provider": "local",
                    "timestamp": _datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "trigger": self._trigger,
                    "initial_context": self._raw_initial_context,
                }
                _conv = [m for m in messages if m.get("role") in ("user", "assistant")]
                await asyncio.to_thread(
                    write_episode_html,
                    _ep_dir,
                    _session_meta,
                    self._llm_captures,
                    _conv,
                    list(self._tool_call_records),
                )
                debug_log_path = str(_ep_dir / "index.html")
                log.info("episode_html_written", path=debug_log_path)
            except Exception as exc:  # nosec B110
                log.warning("episode_html_write_failed", error=str(exc))

        await self._enqueue_distillation(outcome, steps, episode_stub)

        return AgentLoopResult(
            outcome=outcome,  # type: ignore[arg-type]
            steps=steps,
            episode_stub=episode_stub,
            episode_id=self._episode_id or None,
            capability_gap=bool((episode_stub or {}).get("capability_gap", False)),
            gap_description=(episode_stub or {}).get("gap_description", ""),
            llm_captures=list(self._llm_captures),
            tool_call_records=list(self._tool_call_records),
            debug_log_path=debug_log_path,
            top_retrieved_signature=self._top_retrieved_signature,
            stuck_reason=self._stuck_reason,
        )

    async def _enqueue_distillation(
        self,
        outcome: str,
        steps: list[AgentStep],
        episode_stub: Optional[dict],
    ) -> None:
        """Write a session_outcomes row and enqueue gap writing for non-success sessions.

        session_outcomes records the stashed distillation payload so that S5 can trigger
        full LLM-based distillation after verification.  In supervisor mode, LLM-based
        candidate distillation is no longer queued immediately for success sessions —
        the hook fires after verification instead.  Non-success sessions still write gaps
        (no LLM needed).  Outside the supervisor (standalone runs, tests) the queue is
        absent; gap writing still happens for non-success.
        """
        if self._knowledge_store is None or self._db_path is None:
            return

        from utils.knowledge.runbook_distiller import (
            write_gap,
        )
        from utils.agent.work_queue import (
            PRIORITY_LOW,
            WorkItem,
            get_work_queue_or_none,
        )

        queue = get_work_queue_or_none()
        # For chat sessions, compose sig_chat with the top retrieved strategy signature.
        if self._activity_type == "chat" and self._top_retrieved_signature:
            from utils.knowledge.runbook_signature import sig_chat as _sig_chat

            sig = _sig_chat(self._top_retrieved_signature)
        else:
            sig = self._signature
        _db = self._db_path
        _store = self._knowledge_store
        _model = self._model
        _ep = self._episode_id
        _llm = self._llm
        _initial_ctx = self._raw_initial_context

        # Write session_outcomes row with stashed distillation payload.
        # Distillation will be triggered after verification (S5).
        # Chat sessions are included: the user verifies via "Pueo was right/wrong" (S4b).
        if _ep and self._activity_type not in ("",):
            try:
                import json as _json

                from agents.ha_agent_advanced import write_session_outcome

                _conclusion = _json.dumps(dict(episode_stub) if episode_stub else {})
                _payload = _json.dumps(
                    {
                        "steps": [s.model_dump() for s in steps],
                        "initial_context": _initial_ctx,
                        "episode_stub": dict(episode_stub) if episode_stub else None,
                    }
                )
                await asyncio.to_thread(
                    write_session_outcome,
                    _ep,
                    sig,
                    self._activity_type,
                    getattr(self, "_subject_key", ""),
                    _conclusion,
                    _payload,
                    getattr(self._executor, "_hitl_card_id", None),
                )
            except Exception as exc:  # nosec B110
                log.warning("session_outcome_write_failed", error=str(exc))

        if queue is not None:
            # Enqueue procedural distillation for any session that reached a conclusion
            # (ADR 049).  This lane never waits for user verification — it only captures
            # HOW to gather evidence, not what the verdict was.
            _PROCEDURAL_OUTCOMES = frozenset(
                {"success", "needs_action", "monitoring", "awaiting_approval"}
            )
            if outcome in _PROCEDURAL_OUTCOMES and _ep and _db and _store:
                try:
                    from utils.knowledge.runbook_distiller import (
                        enqueue_procedural_distillation,
                    )

                    await enqueue_procedural_distillation(
                        episode_id=_ep,
                        db_path=_db,
                        knowledge_store=_store,
                    )
                except Exception as _pe:  # nosec B110
                    log.warning(
                        "procedural_distillation_enqueue_failed", error=str(_pe)
                    )

            # Supervisor mode: only queue gap writing for non-success (no LLM).
            # Success sessions are deferred until S5 wires distillation to verification.
            # awaiting_approval is not a failure: its card's executor verifies it.
            # host_sleep-stuck sessions are infrastructure interruptions, not knowledge gaps.
            _is_sleep_stuck = outcome == "stuck" and self._stuck_reason == "host_sleep"
            if outcome not in ("success", "awaiting_approval") and not _is_sleep_stuck:
                _steps_snap = list(steps)
                _outcome = outcome
                _stub = dict(episode_stub) if episode_stub else None

                async def _run_gap() -> None:
                    from utils.knowledge.runbook_distiller import distill_and_persist

                    await distill_and_persist(
                        trigger=sig,
                        signature=sig,
                        steps=_steps_snap,
                        outcome=_outcome,
                        episode_id=_ep,
                        db_path=_db,
                        knowledge_store=_store,
                        model=_model,
                        initial_context=_initial_ctx,
                        episode_stub=_stub,
                        llm_client=_llm,
                    )

                submitted = await queue.submit(
                    WorkItem(
                        priority=PRIORITY_LOW,
                        activity_type="runbook_distillation",
                        description=f"Distill {sig}",
                        dedup_key=f"distill_{sig}_{_ep}",
                        suppress_while_running=frozenset(),
                        coro_factory=_run_gap,
                    )
                )
                log.debug(
                    "gap_distillation_enqueued",
                    signature=sig,
                    outcome=outcome,
                    submitted=submitted,
                )
            else:
                # Success session: auto-verify immediately when no HITL card was sent
                # during this session. Sessions that sent a card (or are awaiting an
                # approval or auto-apply) defer — "Pueo was right"/"got this wrong" or
                # the action's executor verifies them (ADR 046).
                _hitl_queued = getattr(self._executor, "_hitl_card_queued", False)
                if not _hitl_queued and _ep and _db and _store:
                    try:
                        from agents.ha_agent_advanced import (
                            resolve_session_outcome_by_episode_id,
                        )
                        from utils.knowledge.runbook_distiller import (
                            enqueue_distillation_after_verification,
                        )

                        await asyncio.to_thread(
                            resolve_session_outcome_by_episode_id,
                            _ep,
                            "success",
                            "system",
                            None,
                            _store,
                        )
                        submitted = await enqueue_distillation_after_verification(
                            episode_id=_ep,
                            verification="success",
                            correction=None,
                            db_path=_db,
                            knowledge_store=_store,
                            model=_model,
                            llm_client=_llm,
                        )
                        log.info(
                            "distillation_auto_verified",
                            signature=sig,
                            episode_id=_ep,
                            submitted=submitted,
                        )
                    except Exception as _ae:  # nosec B110
                        log.warning(
                            "distillation_auto_verify_failed",
                            error=str(_ae),
                        )
                else:
                    log.debug(
                        "distillation_deferred_pending_verification",
                        signature=sig,
                        episode_id=_ep,
                    )
        else:
            # Standalone / test mode: write gap for non-success sessions only.
            # Success distillation requires verification (no queue = skip).
            # host_sleep-stuck sessions are infrastructure interruptions, not gaps.
            _is_sleep_stuck_sa = (
                outcome == "stuck" and self._stuck_reason == "host_sleep"
            )
            if (
                outcome not in ("success", "awaiting_approval")
                and not _is_sleep_stuck_sa
            ):
                try:
                    await asyncio.to_thread(
                        write_gap,
                        _db,
                        _store,
                        sig,
                        sig,
                        list(steps),
                        outcome,
                        _ep,
                    )
                    log.info(
                        "distillation_gap_written_standalone",
                        trigger=sig,
                        outcome=outcome,
                    )
                except Exception as exc:  # nosec B110
                    log.warning("distillation_gap_write_failed", error=str(exc))

    async def _maybe_extend_budget(
        self,
        messages: list[dict],
        steps: list[AgentStep],
        tool_call_count: int,
        start_time: float,
        reason: str,
        episode_stub: Optional[dict],
    ) -> tuple[bool, Optional[dict]]:
        """Ask the LLM whether to extend the budget. Returns (extended, episode_stub).

        If extended=True the caller should continue the loop (self._max_tool_calls
        has been raised). If False the caller should return "exhausted".
        """
        elapsed = time.monotonic() - start_time
        decision = await self._review_limit(
            messages, steps, reason, tool_call_count, elapsed
        )
        if decision.summary_if_giving_up and episode_stub is None:
            episode_stub = {"summary": decision.summary_if_giving_up}
        if decision.can_resolve_with_more and decision.additional_calls_requested > 0:
            headroom = self._absolute_max - tool_call_count
            grant = min(
                decision.additional_calls_requested,
                AGENT_MAX_EXTENSION_CALLS,
                headroom,
            )
            if grant > 0:
                self._max_tool_calls = tool_call_count + grant
                log.info(
                    "agent_loop_budget_extended",
                    reason=reason,
                    grant=grant,
                    total_cap=self._absolute_max,
                )
                return True, episode_stub
        return False, episode_stub

    async def _loop_body(
        self,
        messages: list[dict],
        tools: list[dict],
        steps: list[AgentStep],
        tool_call_count: int,
        start_time: float,
    ) -> tuple[str, Optional[dict]]:
        episode_stub: Optional[dict] = None
        no_tool_streak = 0  # consecutive plain-text responses with no tool calls
        no_tool_extension_count = 0  # times budget was extended due to no_tool_streak
        last_plain_text: str = ""  # most recent plain-text content (fallback summary)
        _pending_nudges: list[str] = []  # nudges injected before the next LLM call
        _call_seq = 0
        _cache_only_streak = 0  # consecutive LLM iterations where every call was cached
        _sleep_retry_done = False  # allow at most one sleep-retry per session

        known_tool_names = {t["function"]["name"] for t in tools}
        guardrail = ToolResultGuardrail(
            max_result_tokens=AGENT_MAX_TOOL_RESULT_TOKENS,
            max_tool_calls=self._max_tool_calls,
            known_tool_names=known_tool_names,
        )

        # Compute timeout once per session; P95 latency doesn't change within a run.
        per_call_secs = await asyncio.to_thread(self._per_call_timeout_seconds)

        # Derive call options once per session from model capabilities + runtime context.
        _call_opts = await asyncio.to_thread(_derive_loop_call_options, self._model)

        while True:
            # Budget check at top of loop so extensions can continue without restart.
            if tool_call_count >= self._max_tool_calls:
                log.warning("agent_loop_budget_exhausted", count=tool_call_count)
                extended, episode_stub = await self._maybe_extend_budget(
                    messages, steps, tool_call_count, start_time, "budget", episode_stub
                )
                if extended:
                    continue
                return "exhausted", episode_stub

            _t0 = time.monotonic()
            _t0_wall = time.time()
            if self._on_llm_call_start is not None:
                try:
                    self._on_llm_call_start(self._model, self._trigger)
                except Exception:  # nosec B110
                    pass
            _call_options: dict = {"temperature": _call_opts.temperature}
            if _call_opts.num_ctx > 0:
                _call_options["num_ctx"] = _call_opts.num_ctx
            try:
                response = await asyncio.wait_for(
                    self._llm.chat_with_tools(
                        model=self._model,
                        messages=messages,
                        tools=tools,
                        options=_call_options,
                        think=_call_opts.think,
                        keep_alive=_call_opts.keep_alive,
                    ),
                    timeout=per_call_secs,
                )
            except asyncio.TimeoutError:
                from utils.system.sleep_guard import (
                    HOST_SLEEP_THRESHOLD_SECONDS,
                    host_sleep_seconds,
                )

                _slept = host_sleep_seconds(
                    _t0_wall, _t0, time.time(), time.monotonic()
                )
                if _slept >= HOST_SLEEP_THRESHOLD_SECONDS and not _sleep_retry_done:
                    _sleep_retry_done = True
                    log.info(
                        "llm_call_retry_after_sleep",
                        slept_s=round(_slept, 1),
                        timeout_seconds=round(per_call_secs, 1),
                    )
                    continue  # retry this LLM call once
                if _slept >= HOST_SLEEP_THRESHOLD_SECONDS:
                    self._stuck_reason = "host_sleep"
                log.warning(
                    "llm_call_stuck",
                    model=self._model,
                    timeout_seconds=round(per_call_secs, 1),
                )
                raise  # propagates to run() as outcome="stuck"
            _latency_ms = (time.monotonic() - _t0) * 1000.0
            if self._on_llm_call_done is not None:
                try:
                    self._on_llm_call_done(self._model, _latency_ms)
                except Exception:  # nosec B110
                    pass
            _timing = (
                response.pop("_ollama_timing", {}) if isinstance(response, dict) else {}
            )
            _thinking_text = (
                response.pop("_thinking", None) if isinstance(response, dict) else None
            )
            if self._db_path:
                from utils.llm.llm_stats import record_llm_call

                _provider = "local"
                try:
                    from utils.llm.llm_factory import _provider_name

                    _provider = _provider_name()
                except Exception:  # nosec B110
                    pass
                asyncio.create_task(
                    asyncio.to_thread(
                        record_llm_call,
                        self._db_path,
                        model=self._model,
                        provider=_provider,
                        call_type="chat_with_tools",
                        latency_ms=_latency_ms,
                        ollama_eval_ms=_timing.get("eval_ms"),
                        ollama_load_ms=_timing.get("load_ms"),
                        input_tokens=_timing.get("input_tokens"),
                        output_tokens=_timing.get("output_tokens"),
                        episode_id=self._episode_id,
                        thinking_text=_thinking_text,
                    )
                )

            _top_tool = None
            _tc_preview = response.get("tool_calls") or []
            if _tc_preview:
                _top_tool = (
                    _tc_preview[0].get("function", {}).get("name")
                    if isinstance(_tc_preview[0], dict)
                    else getattr(
                        getattr(_tc_preview[0], "function", None), "name", None
                    )
                )
            log.info(
                "llm_call",
                model=self._model,
                call_num=tool_call_count,
                top_tool=_top_tool,
                duration_ms=round(_latency_ms),
            )
            import config as _al_cfg

            if _al_cfg.DEBUG_LEVEL >= 1:
                _finish_reason = (
                    response.get("finish_reason", "")
                    if isinstance(response, dict)
                    else ""
                )
                log.debug(
                    "llm_call_detail",
                    model=self._model,
                    finish_reason=_finish_reason,
                    duration_ms=round(_latency_ms),
                )
                _hyp = (
                    (response.get("content", "") or "")[:500]
                    if isinstance(response, dict)
                    else ""
                )
                if _hyp:
                    log.debug("llm_hypothesis", content=_hyp)

            if self._capture_llm:
                from utils.debug.capture import LLMCallRecord

                _call_seq += 1
                _thinking: str | None = None
                _resp_content = response.get("content", "") or ""
                if "<think>" in _resp_content:
                    import re as _re

                    _m = _re.search(r"<think>(.*?)</think>", _resp_content, _re.DOTALL)
                    _thinking = _m.group(1).strip() if _m else None
                _tc_raw = response.get("tool_calls") or []
                self._llm_captures.append(
                    LLMCallRecord(
                        seq=_call_seq,
                        request_messages=list(messages),
                        request_tools=[
                            t["function"]["name"] for t in tools if "function" in t
                        ],
                        response_content=_resp_content,
                        response_tool_calls=list(_tc_raw),
                        thinking=_thinking,
                        nudges_injected=list(_pending_nudges),
                        duration_ms=_latency_ms,
                    )
                )
                _pending_nudges = []

            tool_calls_raw = response.get("tool_calls")
            if not tool_calls_raw:
                content = response.get("content", "")
                known = {t["function"]["name"] for t in tools}
                fallback = _parse_content_as_tool_call(content, known)
                if fallback:
                    # Model embedded the tool call in content instead of
                    # tool_calls (a known qwen2.5-coder quirk).  Patch the
                    # response so the rest of the loop works normally.
                    log.info(
                        "agent_loop_content_tool_call_fallback",
                        content=content[:120],
                    )
                    response["tool_calls"] = fallback
                    response["content"] = ""  # prevent raw JSON from confusing history
                    tool_calls_raw = fallback
                else:
                    no_tool_streak += 1
                    if content:
                        last_plain_text = content
                    if no_tool_streak >= 2:
                        log.info(
                            "agent_loop_no_tool_calls",
                            content=content[:120],
                        )
                        if last_plain_text and episode_stub is None:
                            episode_stub = {"summary": last_plain_text}
                        # Cap extensions for no_tool_streak: if the model can't
                        # make tool calls, more budget won't help — give up after
                        # 2 extension attempts to prevent an infinite loop.
                        if no_tool_extension_count < 2:
                            extended, episode_stub = await self._maybe_extend_budget(
                                messages,
                                steps,
                                tool_call_count,
                                start_time,
                                "no_tool_streak",
                                episode_stub,
                            )
                            if extended:
                                no_tool_extension_count += 1
                                no_tool_streak = 0
                                continue
                        return "exhausted", episode_stub
                    # Single plain-text response — inject a recovery nudge and
                    # retry rather than giving up immediately.
                    log.info(
                        "agent_loop_no_tool_calls_nudge",
                        content=content[:120],
                    )
                    messages.append({"role": "assistant", "content": content or ""})
                    # When no tools have been called yet, nudging straight to
                    # the terminal tool causes the model to skip investigation
                    # and hallucinate answers.  Instead, demand any tool call
                    # and let the system prompt guide which one to use first.
                    _primary_field = _TERMINAL_TOOL_PRIMARY_FIELD.get(
                        self._terminal_tool_name, "summary"
                    )
                    if tool_call_count == 0:
                        nudge = (
                            "You must call a tool — do not return plain text. "
                            "For questions requiring live HA data (disk, config, "
                            "logs, backups), call the appropriate investigative tool "
                            f"first (e.g. get_disk_usage, read_config, read_logs). "
                            f"For general knowledge questions, call "
                            f"{self._terminal_tool_name} with your answer in the "
                            f'"{_primary_field}" field.'
                        )
                    else:
                        calls_remaining = self._max_tool_calls - tool_call_count
                        if calls_remaining <= 2:
                            nudge = (
                                f"Budget nearly exhausted. Call "
                                f"{self._terminal_tool_name} NOW with your findings "
                                f'in the "{_primary_field}" field.'
                            )
                        else:
                            if content:
                                nudge = (
                                    "Your response was plain text and was not delivered "
                                    "to the user. Call "
                                    f"{self._terminal_tool_name} now with the following "
                                    f'text as the "{_primary_field}" field:\n\n'
                                    f"{content[:3000]}"
                                )
                            else:
                                nudge = (
                                    "Your response was not a valid tool call. "
                                    "The user cannot see plain text — it is not delivered. "
                                    "Call the next appropriate tool now, or if investigation "
                                    f"is complete call {self._terminal_tool_name} with "
                                    f'your findings in the "{_primary_field}" field.'
                                )
                    _pending_nudges.append(nudge)
                    messages.append({"role": "user", "content": nudge})
                    continue

            no_tool_streak = 0  # reset: we received tool calls this iteration

            # Append the assistant message (with tool_calls) to history.
            messages.append(response)

            _tc_count_before = tool_call_count
            for tc_raw in tool_calls_raw:
                if tool_call_count >= self._max_tool_calls:
                    log.warning(
                        "agent_loop_budget_exhausted_mid_batch", count=tool_call_count
                    )
                    extended, episode_stub = await self._maybe_extend_budget(
                        messages,
                        steps,
                        tool_call_count,
                        start_time,
                        "budget",
                        episode_stub,
                    )
                    if not extended:
                        return "exhausted", episode_stub
                    # Extended — fall through and execute this tool call.

                fn = tc_raw.get("function", {})
                tool_call = ToolCall(
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments", {}),
                )
                ts = time.time()

                # Repeat guard: skip re-execution of identical (name, args) calls.
                _call_sig = (
                    tool_call.name,
                    json.dumps(tool_call.arguments, sort_keys=True, default=str),
                )
                _is_repeat_exempt = (
                    tool_call.name == self._terminal_tool_name
                    or tool_call.name.startswith("finish_")
                    or tool_call.name in _REPEAT_GUARD_EXEMPT_TOOLS
                )
                _cached = None if _is_repeat_exempt else self._seen_calls.get(_call_sig)

                if _cached is not None:
                    _prior_step, _cached_text = _cached
                    log.info(
                        "agent_loop_repeated_call",
                        tool=tool_call.name,
                        prior_step=_prior_step,
                    )
                    tool_result = ToolResult(
                        tool_name=tool_call.name,
                        success=True,
                        output=_cached_text,
                    )
                    result_text = (
                        f"[Repeated call — identical result from step {_prior_step}]"
                        f" {_cached_text}"
                    )
                    _tc_duration_ms = round((time.time() - ts) * 1000, 1)
                    if self._capture_llm:
                        self._tool_call_records.append(
                            ToolCallRecord(
                                seq=tool_call_count,
                                name=tool_call.name,
                                args=dict(tool_call.arguments),
                                output=_cached_text,
                                error=None,
                                success=True,
                                discard_previous=False,
                                duration_ms=_tc_duration_ms,
                            )
                        )
                    step = AgentStep(
                        step_number=tool_call_count,
                        tool_call=tool_call,
                        tool_result=tool_result,
                        timestamp=ts,
                    )
                    steps.append(step)
                    if self._step_callback is not None:
                        self._step_callback(step)
                    if self._timeline_callback is not None:
                        _sl = f"step {tool_call_count} — {tool_call.name}: CACHED"
                        await self._timeline_callback(tool_call.name, _sl)
                    if LLM_PROVIDER != "local":
                        result_text = sanitize_update_context(result_text)
                    messages.append(
                        {
                            "role": "tool",
                            "content": result_text,
                            "name": tool_call.name,
                        }
                    )
                    log.info(
                        "agent_loop_step",
                        step=tool_call_count,
                        tool=tool_call.name,
                        success=True,
                    )
                    continue  # no budget increment for cached calls

                if self._pre_step_callback is not None:
                    await self._pre_step_callback(tool_call)
                tool_result = await self._executor.execute(tool_call)
                _tc_duration_ms = round((time.time() - ts) * 1000, 1)
                if self._capture_llm:
                    self._tool_call_records.append(
                        ToolCallRecord(
                            seq=tool_call_count,
                            name=tool_call.name,
                            args=dict(tool_call.arguments),
                            output=tool_result.output if tool_result.success else "",
                            error=(
                                tool_result.error if not tool_result.success else None
                            ),
                            success=tool_result.success,
                            discard_previous=tool_result.discard_previous,
                            duration_ms=_tc_duration_ms,
                        )
                    )
                tool_call_count += 1

                step = AgentStep(
                    step_number=tool_call_count,
                    tool_call=tool_call,
                    tool_result=tool_result,
                    timestamp=ts,
                )
                steps.append(step)
                if self._step_callback is not None:
                    self._step_callback(step)
                if self._timeline_callback is not None:
                    status = "OK" if tool_result.success else "ERR"
                    status_line = f"step {tool_call_count} — {tool_call.name}: {status}"
                    await self._timeline_callback(tool_call.name, status_line)

                # Feed tool result back to the conversation (with guardrail inspection).
                result_text = (
                    tool_result.output
                    if tool_result.success
                    else f"Error: {tool_result.error}"
                )
                remaining_budget = self._max_tool_calls - tool_call_count
                result_text = guardrail.process(
                    tool_name=tool_call.name,
                    tool_args=tool_call.arguments,
                    raw_output=result_text,
                    success=tool_result.success,
                    remaining_budget=remaining_budget,
                )
                if not _is_repeat_exempt:
                    self._seen_calls[_call_sig] = (tool_call_count, result_text)
                if LLM_PROVIDER != "local":
                    result_text = sanitize_update_context(result_text)
                messages.append(
                    {
                        "role": "tool",
                        "content": result_text,
                        "name": tool_call.name,
                    }
                )

                if tool_result.discard_previous:
                    reason = tool_call.arguments.get("reason", "")
                    for _i in range(len(messages) - 2, -1, -1):
                        if (
                            messages[_i].get("role") == "tool"
                            and messages[_i].get("name") != "discard_result"
                        ):
                            messages[_i]["content"] = f"[Discarded: {reason}]"
                            log.info(
                                "tool_result_discarded",
                                step=tool_call_count,
                                prev_tool=messages[_i].get("name"),
                                reason=reason[:120],
                            )
                            break

                log.info(
                    "agent_loop_step",
                    step=tool_call_count,
                    tool=tool_call.name,
                    success=tool_result.success,
                )
                log.debug(
                    "agent_loop_tool_detail",
                    step=tool_call_count,
                    tool=tool_call.name,
                    args_preview=str(tool_call.arguments)[:300],
                    output_preview=(
                        str(tool_result.output)[:300] if tool_result.output else ""
                    ),
                    success=tool_result.success,
                )
                if _al_cfg.DEBUG_LEVEL >= 1:
                    log.debug(
                        "tool_call",
                        tool_name=tool_call.name,
                        args_preview=str(tool_call.arguments)[:200],
                        result_preview=(result_text or "")[:200],
                    )
                if _al_cfg.DEBUG_LEVEL >= 2:
                    log.debug(
                        "tool_call_full",
                        tool_name=tool_call.name,
                        args=str(tool_call.arguments),
                        result=(result_text or ""),
                    )

                if tool_call.name == self._terminal_tool_name:
                    episode_stub = {
                        "summary": tool_call.arguments.get("summary", ""),
                        "root_cause": tool_call.arguments.get("root_cause", ""),
                        "human_explanation": tool_call.arguments.get(
                            "human_explanation", ""
                        ),
                        "recommendation": tool_call.arguments.get("recommendation", ""),
                        "action_taken": tool_call.arguments.get("action_taken", ""),
                        "steps": tool_call_count,
                        "capability_gap": bool(
                            tool_call.arguments.get("capability_gap", False)
                        ),
                        "gap_description": tool_call.arguments.get(
                            "gap_description", ""
                        ),
                    }
                    # Stored in conclusion_json; the watching poll checks it (ADR 050).
                    from utils.knowledge.session_verification import (
                        normalize_prediction,
                    )

                    _prediction = normalize_prediction(
                        tool_call.arguments.get("prediction")
                    )
                    if _prediction is not None:
                        episode_stub["prediction"] = _prediction
                    if self._capture_llm and self._llm_captures:
                        self._llm_captures[-1].outcome_path = self._terminal_tool_name
                    return "success", episode_stub

                if tool_result.awaiting_approval:
                    return "awaiting_approval", episode_stub

                if tool_call.name == "apply_fix" and not tool_result.success:
                    return "fix_failed", episode_stub

            # Guard: if every tool call in this batch was served from cache (no budget
            # consumed), the LLM is stuck repeating the same calls. After 3 consecutive
            # all-cached iterations, terminate rather than looping forever.
            if tool_call_count == _tc_count_before:
                _cache_only_streak += 1
                if _cache_only_streak >= 3:
                    log.warning(
                        "agent_loop_cache_only_streak",
                        count=_cache_only_streak,
                        step=tool_call_count,
                    )
                    return "stuck", episode_stub
            else:
                _cache_only_streak = 0

            # All tool calls in this response processed; prompt the model to continue.
            messages.append(
                {"role": "user", "content": "Continue. Call the next tool."}
            )
