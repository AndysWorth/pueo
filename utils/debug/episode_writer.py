"""HTML episode report generator for debug mode."""

from __future__ import annotations

import html
import json
import shutil
import time
from pathlib import Path
from typing import Any

from utils.debug.capture import LLMCallRecord

_INLINE_THRESHOLD = 2048  # bytes: content below this is inlined in <details>

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
"""


def _esc(text: Any) -> str:
    return html.escape(str(text))


def _json_block(data: Any) -> str:
    try:
        s = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except Exception:
        s = str(data)
    return f"<pre>{_esc(s)}</pre>"


def _maybe_file_link(
    content: str,
    filename: str,
    label: str,
    episode_dir: Path,
) -> str:
    """Return inline <details> if small; write file and return a link if large."""
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= _INLINE_THRESHOLD:
        return (
            f"<details><summary>{_esc(label)}</summary>"
            f"<pre>{_esc(content)}</pre></details>"
        )
    (episode_dir / filename).write_text(content, encoding="utf-8")
    size_kb = len(encoded) // 1024
    return f"<a href='{_esc(filename)}'>{_esc(label)} ({size_kb} KB)</a>"


def _build_request_page(seq: int, rec: LLMCallRecord) -> str:
    rows = ""
    for msg in rec.request_messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False, default=str)
        rows += (
            f"<tr><td class='msg-role'>{_esc(role)}</td>"
            f"<td class='msg-content'>{_esc(str(content))}</td></tr>\n"
        )
    tools_html = _esc(", ".join(rec.request_tools)) if rec.request_tools else "(none)"
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>LLM Call {seq} — Request</title>
<style>{_PAGE_CSS}</style></head><body>
<h1>LLM Call {seq} — Full Request</h1>
<p><strong>Tools registered:</strong> {tools_html}</p>
<h2>Messages ({len(rec.request_messages)})</h2>
<table><tr><th>Role</th><th>Content</th></tr>
{rows}
</table>
</body></html>"""


def _build_response_page(seq: int, rec: LLMCallRecord) -> str:
    thinking_html = ""
    if rec.thinking:
        thinking_html = (
            f"<details open><summary>Thinking</summary>"
            f"<div class='thinking'><pre>{_esc(rec.thinking)}</pre></div></details>"
        )
    tc_html = ""
    if rec.response_tool_calls:
        tc_html = f"<h2>Tool Calls</h2>{_json_block(rec.response_tool_calls)}"
    content_html = ""
    display_content = rec.response_content
    if rec.thinking:
        import re as _re

        display_content = _re.sub(
            r"<think>.*?</think>", "", display_content, flags=_re.DOTALL
        ).strip()
    if display_content:
        content_html = f"<h2>Text Content</h2><pre>{_esc(display_content)}</pre>"
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>LLM Call {seq} — Response</title>
<style>{_PAGE_CSS}</style></head><body>
<h1>LLM Call {seq} — Full Response</h1>
<p><strong>Duration:</strong> {rec.duration_ms:.0f} ms</p>
{thinking_html}
{content_html}
{tc_html}
</body></html>"""


def _build_index(
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    conversation: list[dict[str, Any]],
    episode_dir: Path,
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
        conv_rows += (
            f"<tr><td class='msg-role'>{_esc(role)}</td>"
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
        req_link = _maybe_file_link(
            _build_request_page(rec.seq, rec),
            f"llm_{rec.seq}_request.html",
            f"Full request ({len(rec.request_messages)} messages)",
            episode_dir,
        )
        resp_link = _maybe_file_link(
            _build_response_page(rec.seq, rec),
            f"llm_{rec.seq}_response.html",
            f"Full response{' (thinking present)' if rec.thinking else ''}",
            episode_dir,
        )
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
    <span>{rec.duration_ms:.0f} ms{outcome_label}</span>
  </div>
  <div class='call-body'>
    {nudge_html}
    <p><strong>Tools called:</strong> {_esc(tc_names)}</p>
    {req_link}
    {resp_link}
  </div>
</div>"""

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
  <span><span class='label'>Timestamp:</span> {_esc(ts)}</span>
</div>
<h2>Conversation Thread</h2>
{conv_html}
<h2>LLM Call Timeline</h2>
{call_cards if call_cards else '<p>(no captures)</p>'}
</body></html>"""


def write_episode_html(
    episode_dir: Path,
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    conversation: list[dict[str, Any]],
) -> None:
    """Write an HTML debug episode bundle to *episode_dir*.

    Generates index.html plus per-call request/response files (only when
    content exceeds the inline threshold).
    """
    episode_dir.mkdir(parents=True, exist_ok=True)
    index_html = _build_index(session_meta, captures, conversation, episode_dir)
    (episode_dir / "index.html").write_text(index_html, encoding="utf-8")


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
