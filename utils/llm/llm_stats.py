"""LLM call latency tracking and per-call timeout derivation.

Records each LLM call's wall-clock latency to the llm_calls SQLite table and
computes an expected per-call timeout based on historical percentiles.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional


def record_llm_call(
    db_path: str,
    *,
    model: str,
    provider: str,
    call_type: str,
    latency_ms: float,
    ollama_eval_ms: Optional[float] = None,
    ollama_load_ms: Optional[float] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    episode_id: Optional[str] = None,
    session_id: Optional[int] = None,
) -> None:
    """Insert one row into the llm_calls table."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO llm_calls "
            "(ts, episode_id, session_id, model, provider, call_type, "
            "latency_ms, ollama_eval_ms, ollama_load_ms, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                episode_id,
                session_id,
                model,
                provider,
                call_type,
                latency_ms,
                ollama_eval_ms,
                ollama_load_ms,
                input_tokens,
                output_tokens,
            ),
        )


def expected_timeout_ms(
    db_path: str,
    *,
    model: str,
    provider: str,
    percentile: float = 95.0,
    lookback: int = 100,
    factor: float = 5.0,
    min_ms: float = 300_000.0,
    max_ms: float = 1_800_000.0,
    min_samples: int = 5,
    default_ms: float = 600_000.0,
) -> float:
    """Return an expected per-call timeout in milliseconds.

    Queries recent llm_calls rows for this model/provider, computes the
    Nth percentile of latency_ms, multiplies by factor, and clamps to
    [min_ms, max_ms].  Returns default_ms when fewer than min_samples exist.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT latency_ms FROM llm_calls "
                "WHERE model = ? AND provider = ? "
                "ORDER BY ts DESC LIMIT ?",
                (model, provider, lookback),
            ).fetchall()
    except Exception:
        return default_ms

    if len(rows) < min_samples:
        return default_ms

    values = sorted(r[0] for r in rows)
    n = len(values)
    # Linear interpolation for percentile
    idx = (percentile / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    p_val = values[lo] + frac * (values[hi] - values[lo])

    result = p_val * factor
    return max(min_ms, min(max_ms, result))
