"""HTML episode report generator for debug mode."""

from __future__ import annotations

import dataclasses
import html
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from utils.debug.capture import LLMCallRecord, ToolCallRecord

_PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem 2rem;
       background: #f8f9fa; color: #212529; }
h1 { margin-top: 0; font-size: 1.4rem; }
h2 { font-size: 1.1rem; border-bottom: 2px solid #dee2e6; padding-bottom: .3rem; }
.meta { display: flex; gap: 1.5rem; flex-wrap: wrap; background: #e9ecef;
        padding: .8rem 1rem; border-radius: 6px; margin-bottom: 1.5rem; }
.meta span { font-size: .85rem; }
.meta .label { font-weight: 600; color: #495057; }
.call-card { background: #fff; border: 1px solid #dee2e6; border-radius: 6px;
             margin-bottom: 1rem; overflow: hidden; }
.call-header { background: #343a40; color: #fff; padding: .5rem 1rem;
               font-weight: 600; display: flex; justify-content: space-between; }
.call-body { padding: 1rem; }
.nudge { background: #fff3cd; border-left: 4px solid #ffc107;
         padding: .5rem .8rem; margin-bottom: .6rem; border-radius: 0 4px 4px 0;
         font-size: .85rem; white-space: pre-wrap; }
.outcome-finish { color: #198754; font-weight: 600; }
.outcome-fallback { color: #dc3545; font-weight: 600; }
details summary { cursor: pointer; font-weight: 600; padding: .3rem 0; }
pre { background: #f1f3f5; padding: .8rem; border-radius: 4px; overflow-x: auto;
      font-size: .8rem; margin: .4rem 0; white-space: pre-wrap; word-break: break-all; }
.thinking { background: #e8f4fd; border-left: 4px solid #0d6efd;
            padding: .6rem .8rem; border-radius: 0 4px 4px 0; }
a { color: #0d6efd; }
table { border-collapse: collapse; width: 100%; }
td, th { padding: .4rem .7rem; border: 1px solid #dee2e6; font-size: .85rem; }
th { background: #e9ecef; }
.msg-role { font-weight: 600; color: #495057; white-space: nowrap; }
.msg-content { font-size: .82rem; white-space: pre-wrap; word-break: break-word; }
.tool-card { background: #fff; border: 1px solid #dee2e6; border-radius: 6px;
             margin-bottom: .6rem; overflow: hidden; }
.tool-header { padding: .4rem .8rem; font-weight: 600; font-size: .9rem;
               display: flex; gap: 1rem; align-items: center; }
.tool-success .tool-header { background: #d1e7dd; }
.tool-error .tool-header { background: #f8d7da; }
.tool-body { padding: .6rem .8rem; }
.tool-discard { background: #fff3cd; border-radius: 3px; padding: 1px 6px;
                font-size: .8rem; font-weight: 600; }
"""


def _esc(text: Any) -> str:
    return html.escape(str(text))


def _json_block(data: Any) -> str:
    try:
        s = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except Exception:
        s = str(data)
    return f"<pre>{_esc(s)}</pre>"


def _inline_block(content: str, label: str) -> str:
    """Return *content* inline in a <details> block (never a side file)."""
    return (
        f"<details><summary>{_esc(label)}</summary><pre>{_esc(content)}</pre></details>"
    )


def _response_html(rec: LLMCallRecord) -> str:
    """Inline thinking, text content and tool calls for one LLM call."""
    parts = []
    thinking = rec.thinking_native or rec.thinking
    if thinking:
        parts.append(
            "<details open><summary>Thinking</summary>"
            f"<div class='thinking'><pre>{_esc(thinking)}</pre></div></details>"
        )
    display_content = rec.response_content
    if rec.thinking:
        display_content = re.sub(
            r"<think>.*?</think>", "", display_content, flags=re.DOTALL
        ).strip()
    if display_content:
        parts.append(_inline_block(display_content, "Response text"))
    if rec.response_tool_calls:
        parts.append(
            "<details><summary>Response tool calls</summary>"
            f"{_json_block(rec.response_tool_calls)}</details>"
        )
    return "\n".join(parts)


def _token_label(rec: LLMCallRecord) -> str:
    if rec.input_tokens is None and rec.output_tokens is None:
        return ""
    return f" · {rec.input_tokens or 0}→{rec.output_tokens or 0} tok"


def _build_tool_call_log(tool_call_records: list[ToolCallRecord]) -> str:
    if not tool_call_records:
        return ""
    cards = ""
    for rec in tool_call_records:
        success_badge = "✓" if rec.success else "✗"
        card_cls = "tool-success" if rec.success else "tool-error"
        discard_badge = (
            "<span class='tool-discard'>previous result discarded</span>"
            if rec.discard_previous
            else ""
        )
        cached_badge = (
            "<span class='tool-discard'>cached (repeat)</span>" if rec.cached else ""
        )
        args_block = _inline_block(
            json.dumps(rec.args, indent=2, ensure_ascii=False, default=str), "Args"
        )
        body_text = rec.error if not rec.success and rec.error else rec.output
        output_block = _inline_block(
            str(body_text), "Output" if rec.success else "Error"
        )
        saw_block = (
            _inline_block(
                rec.model_saw,
                f"Model saw ({len(rec.model_saw)} of {len(str(body_text))} chars)",
            )
            if rec.model_saw is not None
            else ""
        )
        cards += f"""
<div class='tool-card {_esc(card_cls)}'>
  <div class='tool-header'>
    <span>{rec.seq}. {_esc(rec.name)}</span>
    <span>{success_badge} {rec.duration_ms:.0f} ms</span>
    {discard_badge}{cached_badge}
  </div>
  <div class='tool-body'>
    {args_block}
    {output_block}
    {saw_block}
  </div>
</div>"""
    return f"<h2>Tool Call Log</h2>\n{cards}"


def _build_index(
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    conversation: list[dict[str, Any]],
    tool_call_records: list[ToolCallRecord] | None = None,
) -> str:
    # Session header
    outcome = session_meta.get("outcome", "?")
    model = session_meta.get("model", "?")
    provider = session_meta.get("provider", "?")
    session_id = session_meta.get("session_id", "?")
    ts = session_meta.get("timestamp", "")

    # Conversation thread
    conv_rows = ""
    for msg in conversation:
        role = msg.get("role", "?")
        if role == "system":
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False, default=str)
        tool_name = msg.get("name", "") if role == "tool" else ""
        role_display = f"{role}: {tool_name}" if tool_name else role
        conv_rows += (
            f"<tr><td class='msg-role'>{_esc(role_display)}</td>"
            f"<td class='msg-content'>{_esc(str(content)[:2000])}</td></tr>\n"
        )
    conv_html = (
        f"<table><tr><th>Role</th><th>Content</th></tr>{conv_rows}</table>"
        if conv_rows
        else "<p>(no messages)</p>"
    )

    # LLM call timeline
    call_cards = ""
    for rec in captures:
        nudge_html = "".join(
            f"<div class='nudge'><strong>Nudge injected:</strong><br>{_esc(n)}</div>"
            for n in rec.nudges_injected
        )
        resp_html = _response_html(rec)
        tc_names = (
            ", ".join(
                tc.get("function", {}).get("name", "?")
                for tc in rec.response_tool_calls
            )
            if rec.response_tool_calls
            else "(plain text)"
        )
        outcome_label = ""
        if rec.outcome_path:
            cls = (
                "outcome-finish"
                if rec.outcome_path != "exhaustion_fallback"
                else "outcome-fallback"
            )
            outcome_label = f" <span class='{cls}'>→ {_esc(rec.outcome_path)}</span>"
        call_cards += f"""
<div class='call-card'>
  <div class='call-header'>
    <span>Call {rec.seq}</span>
    <span>{rec.duration_ms:.0f} ms{_esc(_token_label(rec))}{outcome_label}</span>
  </div>
  <div class='call-body'>
    {nudge_html}
    <p><strong>Tools called:</strong> {_esc(tc_names)}
      · request: {len(rec.request_messages)} messages</p>
    {resp_html}
  </div>
</div>"""

    tool_log_html = (
        _build_tool_call_log(tool_call_records) if tool_call_records is not None else ""
    )
    tool_count_meta = (
        f"  <span><span class='label'>Tool calls:</span> {len(tool_call_records)}</span>\n"
        if tool_call_records is not None
        else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>Debug Episode — Session {_esc(str(session_id))}</title>
<style>{_PAGE_CSS}</style></head><body>
<h1>🐛 Debug Episode — Session {_esc(str(session_id))}</h1>
<div class='meta'>
  <span><span class='label'>Model:</span> {_esc(model)}</span>
  <span><span class='label'>Provider:</span> {_esc(provider)}</span>
  <span><span class='label'>Outcome:</span> {_esc(outcome)}</span>
  <span><span class='label'>LLM calls:</span> {len(captures)}</span>
{tool_count_meta}  <span><span class='label'>Timestamp:</span> {_esc(ts)}</span>
</div>
<h2>Conversation Thread</h2>
{conv_html}
{tool_log_html}
<h2>LLM Call Timeline</h2>
{call_cards if call_cards else '<p>(no captures)</p>'}
</body></html>"""


def write_episode_html(
    episode_dir: Path,
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    conversation: list[dict[str, Any]],
    tool_call_records: list[ToolCallRecord] | None = None,
) -> None:
    """Write a self-contained debug episode bundle to *episode_dir*.

    Always writes index.html and episode_data.json (also for sessions with no
    tool calls).  Every piece of content is inlined — no side files.
    """
    episode_dir.mkdir(parents=True, exist_ok=True)
    index_html = _build_index(session_meta, captures, conversation, tool_call_records)
    (episode_dir / "index.html").write_text(index_html, encoding="utf-8")
    _write_episode_data_json(
        episode_dir, session_meta, captures, tool_call_records or [], conversation
    )


# Meta keys copied verbatim into episode_data.json when present (schema v2).
_V2_META_KEYS = (
    "session_type",
    "signature",
    "subject_key",
    "provider",
    "started_at",
    "ended_at",
    "duration_seconds",
    "steps",
    "stuck_reason",
    "top_retrieved_signature",
    "finish_tool",
    "finish_args",
    "episode_stub",
    "system_prompt",
    "offered_tools",
    "agent_episode_id",
)


def build_episode_data(
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    tool_call_records: list[ToolCallRecord],
    conversation: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the episode_data.json dict (schema v2; v1 keys unchanged)."""
    episode_data: dict[str, Any] = {
        "schema_version": 2,
        "episode_id": session_meta.get("session_id", ""),
        "trigger": session_meta.get("trigger", ""),
        "model": session_meta.get("model", ""),
        "outcome": session_meta.get("outcome", ""),
        "timestamp": session_meta.get("timestamp", ""),
        "initial_context": session_meta.get("initial_context", ""),
    }
    for key in _V2_META_KEYS:
        if key in session_meta:
            episode_data[key] = session_meta[key]
    episode_data["messages"] = list(conversation or [])
    episode_data["llm_calls"] = [dataclasses.asdict(r) for r in captures]
    episode_data["tool_calls"] = [dataclasses.asdict(r) for r in tool_call_records]
    return episode_data


def _write_episode_data_json(
    episode_dir: Path,
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    tool_call_records: list[ToolCallRecord],
    conversation: list[dict[str, Any]] | None = None,
) -> None:
    """Write machine-readable episode_data.json for replay and the report."""
    episode_data = build_episode_data(
        session_meta, captures, tool_call_records, conversation
    )
    (episode_dir / "episode_data.json").write_text(
        json.dumps(episode_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def rotate_old_episodes(base_dir: Path, retention_days: int) -> int:
    """Delete episode subdirectories older than *retention_days*. Returns count removed."""
    if not base_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for child in base_dir.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
