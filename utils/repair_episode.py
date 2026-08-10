"""RepairEpisode dataclass and SQLite persistence helpers (Phase 19, item 77)."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Optional

from pydantic import BaseModel, Field

from utils.tool_registry import ToolCall

EpisodeTrigger = str  # "ha_log" | "netalertx" | "manual" | "escalated"


class RepairEpisode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = Field(default_factory=time.time)
    trigger: EpisodeTrigger
    symptoms: list[str]
    tool_sequence: list[ToolCall]
    hypothesis_chain: list[str]
    fix_applied: Optional[str] = None
    verification_result: bool
    model_used: str
    escalated: bool
    duration_seconds: float


def serialize_episode(db_path: str, episode: RepairEpisode) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO repair_episodes
                (id, timestamp, trigger, symptoms, tool_sequence, hypothesis_chain,
                 fix_applied, verification_result, model_used, escalated, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode.id,
                episode.timestamp,
                episode.trigger,
                json.dumps(episode.symptoms),
                json.dumps([tc.model_dump() for tc in episode.tool_sequence]),
                json.dumps(episode.hypothesis_chain),
                episode.fix_applied,
                int(episode.verification_result),
                episode.model_used,
                int(episode.escalated),
                episode.duration_seconds,
            ),
        )
        conn.commit()


def load_episodes(db_path: str, since: Optional[float] = None) -> list[RepairEpisode]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM repair_episodes"
        params: tuple = ()
        if since is not None:
            query += " WHERE timestamp >= ?"
            params = (since,)
        query += " ORDER BY timestamp DESC"
        rows = conn.execute(query, params).fetchall()

    episodes = []
    for row in rows:
        episodes.append(
            RepairEpisode(
                id=row["id"],
                timestamp=row["timestamp"],
                trigger=row["trigger"],
                symptoms=json.loads(row["symptoms"]),
                tool_sequence=[
                    ToolCall(**tc) for tc in json.loads(row["tool_sequence"])
                ],
                hypothesis_chain=json.loads(row["hypothesis_chain"]),
                fix_applied=row["fix_applied"],
                verification_result=bool(row["verification_result"]),
                model_used=row["model_used"],
                escalated=bool(row["escalated"]),
                duration_seconds=row["duration_seconds"],
            )
        )
    return episodes
