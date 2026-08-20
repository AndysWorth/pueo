"""Cloud billing guard — item 76.

Tracks Claude API spend in the cloud_spend SQLite table, exposes helpers for
per-incident and daily cap checks, and raises BillingCapError when limits are
exceeded.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import config

# USD per million tokens for each model family. Keys are prefix-matched against
# the model string (e.g. "claude-sonnet" matches "claude-sonnet-4-5").
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-haiku": (0.25, 1.25),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "claude-fable": (3.00, 15.00),
}
_PRICE_FALLBACK: tuple[float, float] = (3.00, 15.00)


class BillingCapError(Exception):
    """Raised when a billing cap (per-incident or daily) would be exceeded."""


def _price_for(model: str) -> tuple[float, float]:
    for prefix, prices in _PRICE_TABLE.items():
        if prefix in model.lower():
            return prices
    return _PRICE_FALLBACK


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a single API call."""
    in_price, out_price = _price_for(model)
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def _midnight_ts() -> float:
    """Unix timestamp for local midnight today."""
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def get_daily_spend() -> float:
    """Return total cloud spend in USD since local midnight."""
    since = _midnight_ts()
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cloud_spend WHERE timestamp >= ?",
            (since,),
        ).fetchone()
    return float(row[0]) if row else 0.0


def get_incident_spend(incident_id: str) -> float:
    """Return total cloud spend in USD for a specific incident."""
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cloud_spend WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
    return float(row[0]) if row else 0.0


def record_cloud_spend(
    incident_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Persist a single API call's token usage and cost to cloud_spend."""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO cloud_spend
                (incident_id, timestamp, model, input_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (incident_id, time.time(), model, input_tokens, output_tokens, cost_usd),
        )
        conn.commit()


def check_billing_caps(
    incident_id: str, model: str, estimated_input: int, estimated_output: int
) -> None:
    """Raise BillingCapError if caps would be exceeded by the estimated call.

    Checks both per-incident and daily caps against the configured thresholds.
    """
    estimated = estimate_cost(model, estimated_input, estimated_output)

    incident_so_far = get_incident_spend(incident_id)
    if incident_so_far + estimated > config.CLOUD_MAX_COST_PER_INCIDENT_USD:
        raise BillingCapError(
            f"Per-incident cap ${config.CLOUD_MAX_COST_PER_INCIDENT_USD:.2f} would be "
            f"exceeded: ${incident_so_far:.4f} spent + ${estimated:.4f} estimated"
        )

    daily_so_far = get_daily_spend()
    if daily_so_far + estimated > config.CLOUD_MAX_DAILY_SPEND_USD:
        raise BillingCapError(
            f"Daily cap ${config.CLOUD_MAX_DAILY_SPEND_USD:.2f} would be exceeded: "
            f"${daily_so_far:.4f} spent today + ${estimated:.4f} estimated"
        )
