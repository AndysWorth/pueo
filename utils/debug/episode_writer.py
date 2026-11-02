"""Debug episode bundle writer (episode_data.json + index.html)."""

from __future__ import annotations

import dataclasses
import json
import shutil
import time
from pathlib import Path
from typing import Any

from utils.debug.capture import LLMCallRecord, ToolCallRecord


def write_episode_html(
    episode_dir: Path,
    session_meta: dict[str, Any],
    captures: list[LLMCallRecord],
    conversation: list[dict[str, Any]],
    tool_call_records: list[ToolCallRecord] | None = None,
) -> None:
    """Write a self-contained debug episode bundle to *episode_dir*.

    Always writes episode_data.json and index.html (also for sessions with no
    tool calls).  index.html is the chronological report from
    ``episode_report`` rendered without DB extras, so it reads offline; the
    dashboard's ``/episodes/{id}`` renders the same view with them.
    """
    from utils.debug.episode_report import build_episode_view, render_episode_html

    episode_dir.mkdir(parents=True, exist_ok=True)
    episode_data = build_episode_data(
        session_meta, captures, tool_call_records or [], conversation
    )
    (episode_dir / "episode_data.json").write_text(
        json.dumps(episode_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    # Round-trip through JSON so the static page sees exactly what's on disk.
    view = build_episode_view(json.loads(json.dumps(episode_data, default=str)))
    (episode_dir / "index.html").write_text(render_episode_html(view), encoding="utf-8")


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
