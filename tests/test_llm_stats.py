"""Tests for utils/llm/llm_stats.py."""

import sqlite3
import time

import pytest

from utils.llm.llm_stats import expected_timeout_ms, record_llm_call


@pytest.fixture()
def db(tmp_path):
    """Temporary SQLite DB with the llm_calls table."""
    path = str(tmp_path / "test.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE llm_calls ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL NOT NULL, "
            "episode_id TEXT, "
            "session_id INTEGER, "
            "model TEXT NOT NULL, "
            "provider TEXT NOT NULL, "
            "call_type TEXT NOT NULL, "
            "latency_ms REAL NOT NULL, "
            "ollama_eval_ms REAL, "
            "ollama_load_ms REAL, "
            "input_tokens INTEGER, "
            "output_tokens INTEGER"
            ")"
        )
    return path


def test_expected_timeout_returns_default_when_no_rows(db):
    result = expected_timeout_ms(
        db, model="qwen2.5:7b", provider="local", default_ms=999_000.0
    )
    assert result == 999_000.0


def test_expected_timeout_returns_default_when_fewer_than_min_samples(db):
    for _ in range(4):
        record_llm_call(
            db,
            model="qwen2.5:7b",
            provider="local",
            call_type="chat_with_tools",
            latency_ms=5000.0,
        )
    result = expected_timeout_ms(
        db, model="qwen2.5:7b", provider="local", min_samples=5, default_ms=600_000.0
    )
    assert result == 600_000.0


def test_expected_timeout_computes_correct_p95(db):
    # 20 samples: 19 at 10_000 ms, 1 at 150_000 ms
    for _ in range(19):
        record_llm_call(
            db, model="m", provider="local", call_type="c", latency_ms=10_000.0
        )
    record_llm_call(
        db, model="m", provider="local", call_type="c", latency_ms=150_000.0
    )

    result = expected_timeout_ms(
        db,
        model="m",
        provider="local",
        percentile=95.0,
        lookback=100,
        factor=1.0,
        min_ms=0.0,
        max_ms=1_000_000.0,
        min_samples=5,
        default_ms=600_000.0,
    )
    # P95 of [10k x19, 150k x1] should be close to 10_000 with a slight interpolation toward 150_000
    assert result > 10_000.0
    assert result < 150_000.0


def test_expected_timeout_clamps_to_min(db):
    for _ in range(10):
        record_llm_call(
            db, model="m", provider="local", call_type="c", latency_ms=100.0
        )
    result = expected_timeout_ms(
        db,
        model="m",
        provider="local",
        factor=1.0,
        min_ms=60_000.0,
        max_ms=1_800_000.0,
        min_samples=5,
        default_ms=600_000.0,
    )
    assert result == 60_000.0


def test_expected_timeout_clamps_to_max(db):
    for _ in range(10):
        record_llm_call(
            db, model="m", provider="local", call_type="c", latency_ms=600_000.0
        )
    result = expected_timeout_ms(
        db,
        model="m",
        provider="local",
        factor=10.0,
        min_ms=300_000.0,
        max_ms=1_800_000.0,
        min_samples=5,
        default_ms=600_000.0,
    )
    assert result == 1_800_000.0


def test_record_llm_call_inserts_row(db):
    record_llm_call(
        db,
        model="qwen2.5:7b",
        provider="local",
        call_type="chat_with_tools",
        latency_ms=12_345.6,
        ollama_eval_ms=10_000.0,
        ollama_load_ms=500.0,
        input_tokens=200,
        output_tokens=50,
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT model, provider, latency_ms, ollama_eval_ms FROM llm_calls"
        ).fetchone()
    assert row[0] == "qwen2.5:7b"
    assert row[1] == "local"
    assert abs(row[2] - 12_345.6) < 0.01
    assert abs(row[3] - 10_000.0) < 0.01


def test_expected_timeout_returns_default_on_missing_table(tmp_path):
    # DB exists but table doesn't — should not raise, returns default
    bad_db = str(tmp_path / "empty.db")
    sqlite3.connect(bad_db).close()
    result = expected_timeout_ms(
        bad_db, model="m", provider="local", default_ms=42_000.0
    )
    assert result == 42_000.0


def test_expected_timeout_ignores_different_model(db):
    for _ in range(10):
        record_llm_call(
            db, model="other-model", provider="local", call_type="c", latency_ms=1000.0
        )
    result = expected_timeout_ms(
        db, model="qwen2.5:7b", provider="local", min_samples=5, default_ms=555_000.0
    )
    assert result == 555_000.0
