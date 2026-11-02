"""Chronological, fully expanded debug episode report.

``build_episode_view`` turns an ``episode_data.json`` dict (schema v1 or v2)
plus optional post-session DB extras into an :class:`EpisodeView`.
``render_episode_html`` renders that view as one self-contained page: no
``<details>``, no side pages, no JavaScript, nothing truncated.

Both functions are pure, so the static ``index.html`` (``extras=None``) and the
live ``/episodes/{id}`` route share one renderer.

Pairing rule: each ``request_messages`` repeats the full history, so call *i*'s
new messages are ``request[i][len(request[i-1]):]``.  The tool-role messages
appended after call *i* tell how many tool records belong to it — robust to the
repeat-guard cache (which reuses ``seq``) and to tool calls parsed from content
(which leave ``response_tool_calls`` empty).
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Top-level labelled blocks of the first user message (AgentLoop pre-inject).
_CONTEXT_BLOCK_RE = re.compile(r"<([a-z_]+)>\n?(.*?)\n?</\1>", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_DISCARDED_PREFIX = "[Discarded:"

_CONTEXT_LABELS = {
    "ha_profile": "HA profile",
    "knowledge": "Knowledge",
    "previous_investigations": "Previous investigations",
    "trigger": "Trigger",
}


@dataclass
class MessageView:
    role: str
    content: str
    is_nudge: bool = False


@dataclass
class ToolView:
    name: str
    args: dict[str, Any]
    success: bool
    body: str  # full output, or the error text on failure
    duration_ms: float | None = None
    seq: int | None = None
    discard_previous: bool = False
    cached: bool = False
    model_saw: str | None = None  # set only when it differs from body
    discarded_later: bool = False  # content later replaced in history
    has_record: bool = True  # False when rebuilt from a tool message only


@dataclass
class TurnView:
    index: int  # 1-based position in the session
    seq: int | None
    duration_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    eval_ms: float | None
    load_ms: float | None
    outcome_path: str | None
    injected: list[MessageView] = field(default_factory=list)
    thinking: str | None = None
    response_text: str = ""
    tools: list[ToolView] = field(default_factory=list)
    not_executed: list[dict[str, Any]] = field(default_factory=list)
    history_note: str | None = None


@dataclass
class EpisodeView:
    episode_id: str
    schema_version: int
    trigger: str
    session_type: str
    signature: str
    subject_key: str
    model: str
    provider: str
    started_at: str
    ended_at: str
    duration_seconds: float | None
    outcome: str
    stuck_reason: str
    top_retrieved_signature: str
    finish_tool: str
    finish_args: dict[str, Any]
    llm_call_count: int
    tool_call_count: int
    input_tokens: int | None
    output_tokens: int | None
    context_blocks: list[tuple[str, str]]
    turns: list[TurnView]
    trailing: list[MessageView]
    unpaired_tools: list[ToolView]
    system_prompt: str
    offered_tools: list[str]
    extras: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# View building (pure)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _msg(m: dict[str, Any], nudges: set[str]) -> MessageView:
    content = _text(m.get("content"))
    role = str(m.get("role", "?"))
    return MessageView(role=role, content=content, is_nudge=content in nudges)


def parse_context_blocks(text: str) -> list[tuple[str, str]]:
    """Split the first user message into ``(label, body)`` blocks.

    Text outside the labelled tags becomes a leading "Context" block, so no
    character of the message is dropped.  Untagged (older) messages return one
    "Context" block.
    """
    if not text:
        return []
    blocks: list[tuple[str, str]] = []
    outside: list[str] = []
    pos = 0
    for m in _CONTEXT_BLOCK_RE.finditer(text):
        outside.append(text[pos : m.start()])
        tag = m.group(1)
        blocks.append((_CONTEXT_LABELS.get(tag, tag), m.group(2)))
        pos = m.end()
    outside.append(text[pos:])
    rest = "".join(outside).strip()
    if rest:
        blocks.insert(0, ("Context", rest))
    return blocks


def compute_deltas(
    requests: list[list[dict[str, Any]]],
) -> tuple[list[list[dict[str, Any]]], list[str | None]]:
    """Return per-call new messages and a note per call when history shrank.

    ``deltas[0]`` is the whole first request; ``deltas[i]`` is what call *i*
    added on top of call *i-1*'s request.
    """
    deltas: list[list[dict[str, Any]]] = []
    notes: list[str | None] = []
    prev_len = 0
    for i, req in enumerate(requests):
        if i and len(req) < prev_len:
            deltas.append([])
            notes.append(
                f"History shrank from {prev_len} to {len(req)} messages "
                "before this call (trimmed or compressed)."
            )
        else:
            deltas.append(list(req[prev_len:]))
            notes.append(None)
        prev_len = len(req)
    return deltas, notes


def _pick(primary: Any, fallback: Any) -> Any:
    return primary if primary is not None else fallback


def _tool_from_record(rec: dict[str, Any], msg: dict[str, Any] | None) -> ToolView:
    success = bool(rec.get("success", True))
    error = rec.get("error")
    body = _text(rec.get("output")) if success or not error else _text(error)
    seen_raw = body if success or not error else f"Error: {body}"
    model_saw = rec.get("model_saw")
    discarded_later = False
    if msg is not None:
        content = _text(msg.get("content"))
        if content.startswith(_DISCARDED_PREFIX):
            discarded_later = True
        elif "model_saw" not in rec and content != seen_raw:
            # v1 file: derive what the model saw from the history message.
            model_saw = content
    return ToolView(
        name=str(rec.get("name", "?")),
        args=rec.get("args") or {},
        success=success,
        body=body,
        duration_ms=rec.get("duration_ms"),
        seq=rec.get("seq"),
        discard_previous=bool(rec.get("discard_previous")),
        cached=bool(rec.get("cached")),
        model_saw=model_saw,
        discarded_later=discarded_later,
    )


def _tool_from_message(msg: dict[str, Any]) -> ToolView:
    content = _text(msg.get("content"))
    return ToolView(
        name=str(msg.get("name", "?")),
        args={},
        success=not content.startswith("Error:"),
        body=content,
        discarded_later=content.startswith(_DISCARDED_PREFIX),
        has_record=False,
    )


def _finish_from_tools(tool_calls: list[dict[str, Any]]) -> tuple[str, dict]:
    for rec in reversed(tool_calls):
        name = str(rec.get("name", ""))
        if name.startswith("finish_"):
            return name, dict(rec.get("args") or {})
    return "", {}


def build_episode_view(
    data: dict[str, Any], extras: dict[str, Any] | None = None
) -> EpisodeView:
    """Build the report view from an episode_data.json dict (v1 or v2)."""
    calls: list[dict[str, Any]] = list(data.get("llm_calls") or [])
    records: list[dict[str, Any]] = list(data.get("tool_calls") or [])
    final_messages: list[dict[str, Any]] = list(data.get("messages") or [])
    db_calls: list[dict[str, Any]] = list((extras or {}).get("llm_calls") or [])
    # The llm_calls fallback is usable only when it lines up one-to-one.
    if len(db_calls) != len(calls):
        db_calls = []

    requests = [list(c.get("request_messages") or []) for c in calls]
    deltas, notes = compute_deltas(requests)
    # Messages added after the last call (v2 full history only).
    last_len = len(requests[-1]) if requests else 0
    tail = final_messages[last_len:] if len(final_messages) > last_len else []

    turns: list[TurnView] = []
    rec_pos = 0
    trailing: list[MessageView] = []
    for i, call in enumerate(calls):
        nudges = {str(n) for n in call.get("nudges_injected") or []}
        db = db_calls[i] if db_calls else {}
        thinking = (
            call.get("thinking_native")
            or call.get("thinking")
            or db.get("thinking_text")
            or None
        )
        content = _text(call.get("response_content"))
        if call.get("thinking"):
            content = _THINK_RE.sub("", content).strip()
        turn = TurnView(
            index=i + 1,
            seq=call.get("seq"),
            duration_ms=_pick(call.get("duration_ms"), db.get("latency_ms")),
            input_tokens=_pick(call.get("input_tokens"), db.get("input_tokens")),
            output_tokens=_pick(call.get("output_tokens"), db.get("output_tokens")),
            eval_ms=_pick(call.get("eval_ms"), db.get("ollama_eval_ms")),
            load_ms=_pick(call.get("load_ms"), db.get("ollama_load_ms")),
            outcome_path=call.get("outcome_path"),
            thinking=thinking,
            response_text=content,
            history_note=notes[i],
        )
        # Messages injected before this call: the first request's user/system
        # messages are the starting context (shown separately), so skip call 1.
        if i:
            injected = [
                _msg(m, nudges)
                for m in deltas[i]
                if m.get("role") not in ("assistant", "tool")
            ]
            turn.injected = injected
        present = {m.content for m in turn.injected}
        for n in call.get("nudges_injected") or []:
            if i == 0 or str(n) not in present:
                turn.injected.append(MessageView("user", str(n), is_nudge=True))

        # Messages after this call: the next call's delta, or the v2 tail.
        after = deltas[i + 1] if i + 1 < len(calls) else tail
        tool_msgs = [m for m in after if m.get("role") == "tool"]
        last = i + 1 == len(calls)
        take = len(tool_msgs)
        if last:
            take = max(take, len(records) - rec_pos)
        for j in range(take):
            msg = tool_msgs[j] if j < len(tool_msgs) else None
            if rec_pos < len(records):
                turn.tools.append(_tool_from_record(records[rec_pos], msg))
                rec_pos += 1
            elif msg is not None:
                turn.tools.append(_tool_from_message(msg))
        requested = list(call.get("response_tool_calls") or [])
        for tc in requested[len(turn.tools) :]:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            turn.not_executed.append(
                {"name": fn.get("name", "?"), "arguments": fn.get("arguments", {})}
            )
        if last:
            trailing = [
                _msg(m, set())
                for m in tail
                if m.get("role") not in ("assistant", "tool")
            ]
        turns.append(turn)

    first_req = requests[0] if requests else final_messages
    first_user = next(
        (_text(m.get("content")) for m in first_req if m.get("role") == "user"), ""
    )
    # Tool records no LLM call claimed (only when no calls were captured).
    unpaired = [_tool_from_record(r, None) for r in records[rec_pos:]]
    if not calls:
        if not records:
            unpaired = [
                _tool_from_message(m) for m in final_messages if m.get("role") == "tool"
            ]
        user_seen = False
        for m in final_messages:
            role = m.get("role")
            if role in ("system", "tool", "assistant"):
                continue
            if role == "user" and not user_seen:
                user_seen = True  # shown as the starting context
                continue
            trailing.append(_msg(m, set()))
    context_blocks = parse_context_blocks(first_user) or (
        [("Context", _text(data.get("initial_context")))]
        if data.get("initial_context")
        else []
    )
    system_prompt = _text(data.get("system_prompt")) or next(
        (_text(m.get("content")) for m in first_req if m.get("role") == "system"), ""
    )
    offered = list(data.get("offered_tools") or [])
    if not offered and calls:
        offered = list(calls[0].get("request_tools") or [])

    finish_tool = _text(data.get("finish_tool"))
    finish_args = data.get("finish_args")
    if not finish_tool or finish_args is None:
        derived_tool, derived_args = _finish_from_tools(records)
        finish_tool = finish_tool or derived_tool
        if finish_args is None:
            finish_args = derived_args or data.get("episode_stub") or {}
    if not isinstance(finish_args, dict):
        finish_args = {"value": finish_args}

    in_tok = [t.input_tokens for t in turns if t.input_tokens is not None]
    out_tok = [t.output_tokens for t in turns if t.output_tokens is not None]
    outcome_row = (extras or {}).get("session_outcome") or {}
    return EpisodeView(
        episode_id=_text(data.get("episode_id")),
        schema_version=int(data.get("schema_version") or 1),
        trigger=_text(data.get("trigger")),
        session_type=_text(data.get("session_type") or outcome_row.get("session_type")),
        signature=_text(data.get("signature") or outcome_row.get("signature")),
        subject_key=_text(data.get("subject_key") or outcome_row.get("subject_key")),
        model=_text(data.get("model")),
        provider=_text(data.get("provider")),
        started_at=_text(data.get("started_at") or data.get("timestamp")),
        ended_at=_text(data.get("ended_at")),
        duration_seconds=data.get("duration_seconds"),
        outcome=_text(data.get("outcome")),
        stuck_reason=_text(data.get("stuck_reason")),
        top_retrieved_signature=_text(data.get("top_retrieved_signature")),
        finish_tool=finish_tool,
        finish_args=finish_args,
        llm_call_count=len(calls),
        tool_call_count=len(records),
        input_tokens=sum(in_tok) if in_tok else None,
        output_tokens=sum(out_tok) if out_tok else None,
        context_blocks=context_blocks,
        turns=turns,
        trailing=trailing,
        unpaired_tools=unpaired,
        system_prompt=system_prompt,
        offered_tools=offered,
        extras=extras,
    )


# ---------------------------------------------------------------------------
# HTML rendering (pure)
# ---------------------------------------------------------------------------

_CSS = """
:root { --bg:#f6f7f9; --card:#fff; --fg:#1f2328; --muted:#5f6b76;
  --border:#d8dee4; --pre:#f1f3f5; --accent:#0d6efd; --ok:#1a7f37;
  --okbg:#dafbe1; --err:#cf222e; --errbg:#ffebe9; --warnbg:#fff8c5;
  --warn:#9a6700; --think:#eef4ff; }
@media (prefers-color-scheme: dark) { :root { --bg:#16181c; --card:#1f2228;
  --fg:#e6e8eb; --muted:#9aa4ae; --border:#353a42; --pre:#262a31;
  --accent:#6ea8fe; --ok:#56d364; --okbg:#12261a; --err:#ff7b72;
  --errbg:#2d1517; --warnbg:#2e2610; --warn:#e3b341; --think:#18233a; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,sans-serif; }
.wrap { max-width:62rem; margin:0 auto; padding:1rem; }
h1 { font-size:1.35rem; margin:.2rem 0 .8rem; word-break:break-word; }
h2 { font-size:1.1rem; margin:1.6rem 0 .6rem; padding-bottom:.25rem;
  border-bottom:2px solid var(--border); }
h3 { font-size:.95rem; margin:.9rem 0 .3rem; color:var(--muted); }
.card { background:var(--card); border:1px solid var(--border);
  border-radius:8px; padding:.8rem 1rem; margin:.6rem 0; }
table.kv { border-collapse:collapse; width:100%; }
table.kv th, table.kv td { text-align:left; vertical-align:top;
  padding:.3rem .5rem; border-bottom:1px solid var(--border); }
table.kv th { width:13rem; color:var(--muted); font-weight:600; }
pre { background:var(--pre); padding:.6rem .7rem; border-radius:6px;
  margin:.3rem 0; font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap; overflow-wrap:anywhere; }
.toc { position:sticky; top:0; z-index:2; background:var(--bg);
  padding:.4rem 0; border-bottom:1px solid var(--border); font-size:.85rem; }
.toc a { margin-right:.55rem; color:var(--accent); text-decoration:none; }
.turn { background:var(--card); border:1px solid var(--border);
  border-radius:8px; margin:1rem 0; scroll-margin-top:2.6rem; }
.turn-h { padding:.45rem .9rem; border-bottom:1px solid var(--border);
  font-weight:600; display:flex; flex-wrap:wrap; gap:.4rem 1rem; }
.turn-h .m { font-weight:400; color:var(--muted); font-size:.85rem; }
.turn-b { padding:.6rem .9rem; }
.nudge { background:var(--warnbg); border-left:4px solid var(--warn);
  padding:.45rem .7rem; border-radius:0 6px 6px 0; margin:.35rem 0; }
.nudge b { color:var(--warn); }
.thinking { background:var(--think); color:var(--muted);
  border-left:4px solid var(--accent); padding:.45rem .7rem;
  border-radius:0 6px 6px 0; margin:.35rem 0; white-space:pre-wrap; }
.resp { white-space:pre-wrap; overflow-wrap:anywhere; margin:.35rem 0; }
.tool { border:1px solid var(--border); border-radius:6px; margin:.6rem 0; }
.tool-h { padding:.35rem .7rem; font-weight:600; display:flex;
  flex-wrap:wrap; gap:.3rem .8rem; align-items:center; }
.tool.ok .tool-h { background:var(--okbg); }
.tool.err .tool-h { background:var(--errbg); }
.tool-b { padding:.4rem .7rem; }
.badge { font-size:.75rem; font-weight:600; padding:1px 7px;
  border-radius:10px; border:1px solid var(--border); background:var(--card); }
.ok .mark { color:var(--ok); } .err .mark { color:var(--err); }
.note { color:var(--muted); font-size:.85rem; margin:.3rem 0; }
.outcome-fallback { color:var(--err); } .outcome-finish { color:var(--ok); }
.muted { color:var(--muted); }
"""


def _esc(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _pre(value: Any) -> str:
    return f"<pre>{_esc(value)}</pre>"


def _pretty(value: Any) -> str:
    try:
        s = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(value)
    return _pre(s)


def _cell(value: Any) -> str:
    """Key/value cell: plain text for scalars, a <pre> for structures/long text."""
    if isinstance(value, (dict, list)):
        return _pretty(value)
    text = _text(value)
    if "\n" in text or len(text) > 160:
        return _pre(text)
    return _esc(text)


def _kv_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_cell(v)}</td></tr>"
        for k, v in rows
        if v not in (None, "", [], {})
    )
    return f"<table class='kv'>{body}</table>" if body else ""


def _fmt_ms(ms: Any) -> str:
    if ms is None:
        return ""
    ms = float(ms)
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _fmt_epoch(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return _text(ts)


def _header(v: EpisodeView) -> str:
    outcome = v.outcome
    if v.stuck_reason:
        outcome += f" ({v.stuck_reason})"
    tokens = ""
    if v.input_tokens is not None or v.output_tokens is not None:
        tokens = f"{v.input_tokens or 0} in / {v.output_tokens or 0} out"
    duration = f"{v.duration_seconds:.1f} s" if v.duration_seconds else ""
    rows: list[tuple[str, Any]] = [
        ("Outcome", outcome),
        ("Trigger", v.trigger),
        ("Session type", v.session_type),
        ("Signature", v.signature),
        ("Subject key", v.subject_key),
        ("Top retrieved signature", v.top_retrieved_signature),
        ("Model", f"{v.model} ({v.provider})" if v.provider else v.model),
        ("Started", v.started_at),
        ("Ended", v.ended_at),
        ("Duration", duration),
        ("LLM calls", str(v.llm_call_count)),
        ("Tool calls", str(v.tool_call_count)),
        ("Tokens", tokens),
        ("Episode id", v.episode_id),
        ("Schema", f"v{v.schema_version}"),
    ]
    finish = ""
    if v.finish_tool or v.finish_args:
        finish = (
            f"<h3>Conclusion — {_esc(v.finish_tool or 'episode stub')}</h3>"
            + _kv_table(list(v.finish_args.items()))
        )
    return f"<div class='card'>{_kv_table(rows)}{finish}</div>"


def _after_session(extras: dict[str, Any] | None) -> str:
    if extras is None:
        return (
            "<h2 id='after'>After the session</h2><p class='note'>Static copy — "
            "open this episode in the Pueo dashboard (<code>/episodes/&lt;id&gt;"
            "</code>) for verification, card and learning results.</p>"
        )
    parts: list[str] = []
    so = extras.get("session_outcome")
    if so:
        conclusion: Any = so.get("conclusion_json")
        try:
            conclusion = json.loads(conclusion) if conclusion else None
        except (TypeError, ValueError):
            pass
        parts.append(
            "<h3>Session outcome</h3>"
            + _kv_table(
                [
                    ("Verification", so.get("verification")),
                    ("Verified by", so.get("verified_by")),
                    ("Verified at", so.get("verified_at")),
                    ("Correction", so.get("correction")),
                    ("Distilled at", so.get("distilled_at")),
                    ("Card id", so.get("card_id")),
                    ("Recorded", so.get("created_at")),
                    ("Stored conclusion", conclusion),
                ]
            )
        )
    else:
        parts.append("<p class='note'>No session_outcomes row.</p>")
    card = extras.get("card")
    if card:
        parts.append(
            "<h3>HITL card</h3>"
            + _kv_table(
                [
                    ("Title", card.get("subject")),
                    ("Status", card.get("status")),
                    ("Resolution", card.get("resolution")),
                    ("Resolved at", _fmt_epoch(card.get("resolved_at"))),
                    ("Sent at", _fmt_epoch(card.get("sent_at"))),
                    ("Body", card.get("body")),
                ]
            )
        )
    usage = extras.get("runbook_usage") or []
    if usage:
        parts.append("<h3>Runbooks followed</h3>")
        for u in usage:
            parts.append(
                _kv_table(
                    [
                        ("Runbook", u.get("strategy_id")),
                        ("Outcome", u.get("outcome")),
                        ("Verification", u.get("verification")),
                        ("Verified at", u.get("verified_at")),
                    ]
                )
            )
    entries = extras.get("learned_entries") or []
    parts.append(f"<h3>Learned entries ({len(entries)})</h3>")
    if not entries:
        parts.append("<p class='note'>None reference this episode.</p>")
    for e in entries:
        state = "retired" if e.get("retired_at") else "live"
        if e.get("promoted_to"):
            state = f"promoted to {e['promoted_to']}"
        parts.append(
            "<div class='card'>"
            f"<b>{_esc(e.get('kind'))}</b> <span class='muted'>"
            f"{_esc(e.get('signature'))} · ✓{_esc(e.get('success_count'))} "
            f"✗{_esc(e.get('failure_count'))} · {_esc(state)}"
            + (f" · tool {_esc(e.get('tool'))}" if e.get("tool") else "")
            + f"</span><div class='resp'>{_esc(e.get('text'))}</div></div>"
        )
    facts = extras.get("facts") or []
    if facts:
        parts.append(f"<h3>Instance facts since this session ({len(facts)})</h3>")
        for f in facts:
            parts.append(
                f"<div class='card'><b>{_esc(f.get('key'))}</b> <span class='muted'>"
                f"{_esc(f.get('source'))} · {_esc(_fmt_epoch(f.get('ts')))}</span>"
                f"<div class='resp'>{_esc(f.get('content'))}</div></div>"
            )
    return "<h2 id='after'>After the session</h2>" + "".join(parts)


def _message_block(m: MessageView) -> str:
    label = "Nudge" if m.is_nudge else f"Injected {m.role} message"
    return f"<div class='nudge'><b>{_esc(label)}</b>{_pre(m.content)}</div>"


def _tool_block(t: ToolView) -> str:
    cls = "ok" if t.success else "err"
    badges = []
    if t.cached:
        badges.append("cached (repeat call)")
    if t.discard_previous:
        badges.append("previous result discarded")
    if t.discarded_later:
        badges.append("result later discarded from history")
    if not t.has_record:
        badges.append("no tool record — from history")
    badge_html = "".join(f"<span class='badge'>{_esc(b)}</span>" for b in badges)
    seq = f"#{t.seq} " if t.seq is not None else ""
    out = [
        f"<div class='tool {cls}'><div class='tool-h'>"
        f"<span class='mark'>{'✓' if t.success else '✗'}</span>"
        f"<span>{_esc(seq)}{_esc(t.name)}</span>"
        f"<span class='muted'>{_esc(_fmt_ms(t.duration_ms))}</span>{badge_html}"
        "</div><div class='tool-b'>"
    ]
    if t.args:
        out.append("<h3>Args</h3>" + _pretty(t.args))
    out.append(
        f"<h3>{'Output' if t.success else 'Error'} ({len(t.body)} chars)</h3>"
        + _pre(t.body)
    )
    if t.model_saw is not None:
        out.append(
            f"<p class='note'>Model saw {len(t.model_saw)} of {len(t.body)} chars:"
            "</p>" + _pre(t.model_saw)
        )
    out.append("</div></div>")
    return "".join(out)


def _turn_block(t: TurnView) -> str:
    meta = [
        _fmt_ms(t.duration_ms),
        (
            f"{t.input_tokens or 0}→{t.output_tokens or 0} tok"
            if t.input_tokens is not None or t.output_tokens is not None
            else ""
        ),
        f"eval {_fmt_ms(t.eval_ms)}" if t.eval_ms is not None else "",
        f"load {_fmt_ms(t.load_ms)}" if t.load_ms is not None else "",
    ]
    outcome = ""
    if t.outcome_path:
        cls = (
            "outcome-fallback"
            if t.outcome_path == "exhaustion_fallback"
            else "outcome-finish"
        )
        outcome = f"<span class='{cls}'>→ {_esc(t.outcome_path)}</span>"
    parts = [
        f"<div class='turn' id='call-{t.index}'><div class='turn-h'>"
        f"<span>LLM call {t.index}</span>"
        + "".join(f"<span class='m'>{_esc(m)}</span>" for m in meta if m)
        + outcome
        + "</div><div class='turn-b'>"
    ]
    if t.history_note:
        parts.append(f"<p class='note'>{_esc(t.history_note)}</p>")
    parts.extend(_message_block(m) for m in t.injected)
    if t.thinking:
        parts.append(f"<h3>Thinking</h3><div class='thinking'>{_esc(t.thinking)}</div>")
    if t.response_text:
        parts.append(
            f"<h3>Response</h3><div class='resp'>{_esc(t.response_text)}</div>"
        )
    if not t.tools and not t.not_executed:
        parts.append("<p class='note'>No tool calls this turn.</p>")
    parts.extend(_tool_block(tool) for tool in t.tools)
    for ne in t.not_executed:
        parts.append(
            f"<div class='tool err'><div class='tool-h'><span>{_esc(ne['name'])}"
            "</span><span class='badge'>requested, not executed</span></div>"
            f"<div class='tool-b'>{_pretty(ne['arguments'])}</div></div>"
        )
    parts.append("</div></div>")
    return "".join(parts)


def render_episode_html(view: EpisodeView) -> str:
    """Render *view* as a self-contained HTML page (all content escaped)."""
    title = view.signature or view.trigger or "episode"
    toc = ["<a href='#top'>Header</a>", "<a href='#after'>After</a>"]
    toc.append("<a href='#context'>Context</a>")
    toc.extend(f"<a href='#call-{t.index}'>{t.index}</a>" for t in view.turns)
    toc.append("<a href='#appendix'>Appendix</a>")

    ctx = "".join(
        f"<h3>{_esc(label)}</h3>{_pre(body)}" for label, body in view.context_blocks
    )
    turns = "".join(_turn_block(t) for t in view.turns) or (
        "<p class='note'>No LLM calls were recorded.</p>"
    )
    trailing = ""
    if view.trailing:
        trailing = "<h2>After the last call</h2>" + "".join(
            _message_block(m) for m in view.trailing
        )
    if view.unpaired_tools:
        trailing += "<h2>Tool calls without a recorded LLM call</h2>" + "".join(
            _tool_block(t) for t in view.unpaired_tools
        )
    tools = ", ".join(view.offered_tools)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Episode {_esc(view.episode_id[:8])} — {_esc(title)}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<nav class="toc">{''.join(toc)}</nav>
<h1 id="top">{_esc(title)} — {_esc(view.outcome or '?')}</h1>
{_header(view)}
{_after_session(view.extras)}
<h2 id="context">Starting context</h2>
{ctx or "<p class='note'>(none recorded)</p>"}
<h2>Transcript</h2>
{turns}
{trailing}
<h2 id="appendix">Appendix</h2>
<h3>Offered tools ({len(view.offered_tools)})</h3>
{_pre(tools) if tools else "<p class='note'>(not recorded)</p>"}
<h3>System prompt</h3>
{_pre(view.system_prompt) if view.system_prompt else "<p class='note'>(not recorded)</p>"}
</div></body></html>
"""
