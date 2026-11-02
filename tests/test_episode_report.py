"""Tests for the chronological debug episode report (#1057).

Covers utils/debug/episode_report.py (view building, pairing, deltas, context
blocks, v1 compatibility, rendering, escaping) and
utils/debug/episode_extras.py (read-only DB joins, card lookup, folder lookup).
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from utils.debug.episode_extras import (
    find_episode_dir,
    load_card,
    load_episode_extras,
)
from utils.debug.episode_report import (
    build_episode_view,
    compute_deltas,
    parse_context_blocks,
    render_episode_html,
)

_SYS = {"role": "system", "content": "You are Pueo."}
_USER = {
    "role": "user",
    "content": (
        "<ha_profile>\nCore: 2026.10.4\n</ha_profile>\n\n"
        "<knowledge>\n[SEED RUNBOOK] post restart\n</knowledge>\n\n"
        "<trigger>\nDevices offline after restart\n</trigger>"
    ),
}


def _tc(name: str, **args) -> dict:
    return {"function": {"name": name, "arguments": args}}


def _v1_episode() -> dict:
    """Real-shaped v1 file: 3 calls, nudge, failed tool, exhaustion fallback."""
    a1 = {
        "role": "assistant",
        "content": "Plan",
        "tool_calls": [_tc("get_integration_diagnostics"), _tc("read_logs")],
    }
    t1 = {
        "role": "tool",
        "name": "get_integration_diagnostics",
        "content": "Error: 404",
    }
    t2 = {"role": "tool", "name": "read_logs", "content": "(no log records)"}
    n1 = {"role": "user", "content": "Continue. Call the next tool."}
    a2 = {"role": "assistant", "content": "", "tool_calls": [_tc("get_logbook")]}
    t3 = {"role": "tool", "name": "get_logbook", "content": "logbook rows"}
    req1 = [_SYS, _USER]
    req2 = req1 + [a1, t1, t2, n1]
    req3 = req2 + [a2, t3, n1]
    return {
        "episode_id": "b05ae6fb-a4d7-4c2c-b43e-1ee2f90458f8",
        "trigger": "device_availability_poll",
        "model": "qwen3.5:27b",
        "outcome": "error",
        "timestamp": "2026-10-30 00:05:12",
        "initial_context": "Devices offline after restart",
        "llm_calls": [
            {
                "seq": 1,
                "request_messages": req1,
                "request_tools": ["read_logs", "get_logbook"],
                "response_content": "Plan",
                "response_tool_calls": a1["tool_calls"],
                "thinking": None,
                "nudges_injected": [],
                "duration_ms": 61000.0,
                "outcome_path": None,
            },
            {
                "seq": 2,
                "request_messages": req2,
                "request_tools": ["read_logs", "get_logbook"],
                "response_content": "",
                "response_tool_calls": a2["tool_calls"],
                "thinking": None,
                "nudges_injected": [],
                "duration_ms": 30000.0,
                "outcome_path": None,
            },
            {
                "seq": 3,
                "request_messages": req3,
                "request_tools": ["read_logs", "get_logbook"],
                "response_content": "",
                "response_tool_calls": [_tc("recall", query="x")],
                "thinking": None,
                "nudges_injected": [],
                "duration_ms": 20000.0,
                "outcome_path": "exhaustion_fallback",
            },
        ],
        "tool_calls": [
            {
                "seq": 0,
                "name": "get_integration_diagnostics",
                "args": {},
                "output": "",
                "error": "404",
                "success": False,
                "discard_previous": False,
                "duration_ms": 12.0,
            },
            {
                "seq": 1,
                "name": "read_logs",
                "args": {},
                "output": "(no log records)",
                "error": None,
                "success": True,
                "discard_previous": False,
                "duration_ms": 900.0,
            },
            {
                "seq": 2,
                "name": "get_logbook",
                "args": {},
                "output": "logbook rows",
                "error": None,
                "success": True,
                "discard_previous": False,
                "duration_ms": 40.0,
            },
            {
                "seq": 3,
                "name": "recall",
                "args": {"query": "x"},
                "output": "no memories",
                "error": None,
                "success": True,
                "discard_previous": False,
                "duration_ms": 5.0,
            },
        ],
    }


def _v2_episode() -> dict:
    """v2 file with a cached repeat that shares seq with the next real call."""
    a1 = {
        "role": "assistant",
        "content": "",
        "tool_calls": [_tc("read_logs"), _tc("read_logs"), _tc("finish_x")],
    }
    t1 = {"role": "tool", "name": "read_logs", "content": "LOG"}
    t2 = {
        "role": "tool",
        "name": "read_logs",
        "content": "[Repeated call — identical result from step 1] LOG",
    }
    data = {
        "schema_version": 2,
        "episode_id": "11111111-2222-3333-4444-555555555555",
        "trigger": "repair_poll",
        "session_type": "repair_issue",
        "signature": "repair:hassio:restart_required",
        "subject_key": "hassio/abc",
        "model": "qwen3.5:27b",
        "provider": "ollama",
        "outcome": "success",
        "started_at": "2026-11-02T15:00:00Z",
        "ended_at": "2026-11-02T15:02:00Z",
        "duration_seconds": 120.0,
        "finish_tool": "finish_x",
        "finish_args": {"outcome": "success", "summary": "fixed"},
        "system_prompt": "SYSTEM V2",
        "offered_tools": ["read_logs", "finish_x"],
        "llm_calls": [
            {
                "seq": 1,
                "request_messages": [_SYS, _USER],
                "request_tools": ["read_logs", "finish_x"],
                "response_content": "",
                "response_tool_calls": a1["tool_calls"],
                "thinking": None,
                "thinking_native": "NATIVE THOUGHT",
                "nudges_injected": [],
                "duration_ms": 1500.0,
                "outcome_path": "finish_x",
                "input_tokens": 900,
                "output_tokens": 40,
                "eval_ms": 1200.0,
                "load_ms": 30.0,
            }
        ],
        "tool_calls": [
            {
                "seq": 0,
                "name": "read_logs",
                "args": {},
                "output": "LOG",
                "error": None,
                "success": True,
                "discard_previous": False,
                "duration_ms": 5.0,
                "cached": False,
                "model_saw": None,
            },
            {
                "seq": 1,
                "name": "read_logs",
                "args": {},
                "output": "LOG",
                "error": None,
                "success": True,
                "discard_previous": False,
                "duration_ms": 0.1,
                "cached": True,
                "model_saw": t2["content"],
            },
            {
                "seq": 1,
                "name": "finish_x",
                "args": {"outcome": "success"},
                "output": "done",
                "error": None,
                "success": True,
                "discard_previous": False,
                "duration_ms": 1.0,
                "cached": False,
                "model_saw": None,
            },
        ],
    }
    data["messages"] = [_SYS, _USER, a1, t1, t2]
    return data


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestParseContextBlocks:
    def test_labelled_blocks_in_order(self):
        blocks = parse_context_blocks(_USER["content"])
        assert [b[0] for b in blocks] == ["HA profile", "Knowledge", "Trigger"]
        assert blocks[2][1] == "Devices offline after restart"

    def test_untagged_text_is_one_block(self):
        assert parse_context_blocks("Available update: core") == [
            ("Context", "Available update: core")
        ]

    def test_text_outside_tags_kept_first(self):
        blocks = parse_context_blocks("lead in\n<trigger>\nT\n</trigger>")
        assert blocks == [("Context", "lead in"), ("Trigger", "T")]

    def test_unknown_tag_uses_tag_name(self):
        assert parse_context_blocks("<custom_block>x</custom_block>") == [
            ("custom_block", "x")
        ]

    def test_empty(self):
        assert parse_context_blocks("") == []


class TestComputeDeltas:
    def test_growing_history(self):
        r1 = [_SYS, _USER]
        r2 = r1 + [{"role": "assistant", "content": "a"}]
        deltas, notes = compute_deltas([r1, r2])
        assert deltas == [r1, [{"role": "assistant", "content": "a"}]]
        assert notes == [None, None]

    def test_shrunk_history_noted(self):
        deltas, notes = compute_deltas([[_SYS, _USER, _USER], [_SYS]])
        assert deltas[1] == []
        assert notes[1] and "shrank from 3 to 1" in notes[1]


# ---------------------------------------------------------------------------
# build_episode_view
# ---------------------------------------------------------------------------


class TestBuildEpisodeViewV1:
    def test_pairs_tools_to_calls_by_history(self):
        v = build_episode_view(_v1_episode())
        assert [[t.name for t in turn.tools] for turn in v.turns] == [
            ["get_integration_diagnostics", "read_logs"],
            ["get_logbook"],
            ["recall"],
        ]

    def test_injected_messages_are_deltas_only(self):
        v = build_episode_view(_v1_episode())
        assert v.turns[0].injected == []
        assert [m.content for m in v.turns[1].injected] == [
            "Continue. Call the next tool."
        ]
        # Repeated history (system prompt, first user msg) never re-appears.
        assert all("You are Pueo" not in m.content for m in v.turns[2].injected)

    def test_failed_tool_body_is_error(self):
        tool = build_episode_view(_v1_episode()).turns[0].tools[0]
        assert tool.success is False
        assert tool.body == "404"
        assert tool.model_saw is None  # history "Error: 404" == raw form

    def test_v1_model_saw_derived_from_history(self):
        data = _v1_episode()
        data["llm_calls"][1]["request_messages"][4] = {
            "role": "tool",
            "name": "read_logs",
            "content": "(no log",
        }
        tool = build_episode_view(data).turns[0].tools[1]
        assert tool.model_saw == "(no log"

    def test_discarded_history_flagged_not_model_saw(self):
        data = _v1_episode()
        data["llm_calls"][1]["request_messages"][3] = {
            "role": "tool",
            "name": "get_integration_diagnostics",
            "content": "[Discarded: superseded]",
        }
        tool = build_episode_view(data).turns[0].tools[0]
        assert tool.discarded_later is True
        assert tool.model_saw is None

    def test_missing_v2_fields_blank_and_finish_derived(self):
        v = build_episode_view(_v1_episode())
        assert v.schema_version == 1
        assert v.signature == "" and v.provider == "" and v.ended_at == ""
        assert v.started_at == "2026-10-30 00:05:12"
        assert v.finish_tool == ""  # no finish_* call in this session
        assert v.offered_tools == ["read_logs", "get_logbook"]
        assert v.system_prompt == "You are Pueo."
        assert v.turns[2].outcome_path == "exhaustion_fallback"

    def test_finish_args_derived_from_last_finish_record(self):
        data = _v1_episode()
        data["tool_calls"][-1]["name"] = "finish_device_availability"
        data["tool_calls"][-1]["args"] = {"outcome": "monitoring"}
        v = build_episode_view(data)
        assert v.finish_tool == "finish_device_availability"
        assert v.finish_args == {"outcome": "monitoring"}

    def test_episode_stub_used_when_no_finish(self):
        data = _v1_episode()
        data["episode_stub"] = {"summary": "plain text"}
        assert build_episode_view(data).finish_args == {"summary": "plain text"}

    def test_db_llm_calls_fill_tokens_and_thinking(self):
        rows = [
            {
                "latency_ms": 1.0,
                "input_tokens": 100 + i,
                "output_tokens": 10,
                "ollama_eval_ms": 50.0,
                "ollama_load_ms": 2.0,
                "thinking_text": "DB THINK" if i == 0 else None,
            }
            for i in range(3)
        ]
        v = build_episode_view(_v1_episode(), {"llm_calls": rows})
        assert v.turns[0].input_tokens == 100
        assert v.turns[0].thinking == "DB THINK"
        assert v.turns[0].duration_ms == 61000.0  # file value wins
        assert v.input_tokens == 303 and v.output_tokens == 30

    def test_db_llm_calls_ignored_when_counts_differ(self):
        v = build_episode_view(_v1_episode(), {"llm_calls": [{"input_tokens": 5}]})
        assert v.turns[0].input_tokens is None
        assert v.input_tokens is None

    def test_requested_but_not_executed(self):
        data = _v1_episode()
        data["tool_calls"].pop()  # recall never ran (budget hit mid-batch)
        v = build_episode_view(data)
        assert v.turns[2].tools == []
        assert v.turns[2].not_executed == [
            {"name": "recall", "arguments": {"query": "x"}}
        ]

    def test_tool_message_without_record(self):
        data = _v1_episode()
        data["tool_calls"] = data["tool_calls"][:1]
        v = build_episode_view(data)
        rebuilt = v.turns[0].tools[1]
        assert rebuilt.has_record is False
        assert rebuilt.body == "(no log records)"

    def test_think_tags_stripped_from_response(self):
        data = _v1_episode()
        data["llm_calls"][0]["response_content"] = "<think>hmm</think>Plan"
        data["llm_calls"][0]["thinking"] = "hmm"
        turn = build_episode_view(data).turns[0]
        assert turn.thinking == "hmm"
        assert turn.response_text == "Plan"

    def test_nudge_marked(self):
        data = _v1_episode()
        data["llm_calls"][1]["nudges_injected"] = ["Continue. Call the next tool."]
        turn = build_episode_view(data).turns[1]
        assert len(turn.injected) == 1 and turn.injected[0].is_nudge

    def test_nudge_missing_from_history_still_shown(self):
        data = _v1_episode()
        data["llm_calls"][0]["nudges_injected"] = ["first nudge"]
        turn = build_episode_view(data).turns[0]
        assert [(m.content, m.is_nudge) for m in turn.injected] == [
            ("first nudge", True)
        ]

    def test_initial_context_fallback_without_calls(self):
        data = {"episode_id": "e", "initial_context": "only ctx"}
        v = build_episode_view(data)
        assert v.context_blocks == [("Context", "only ctx")]
        assert v.turns == []


class TestBuildEpisodeViewV2:
    def test_cached_call_with_shared_seq_paired_in_order(self):
        turn = build_episode_view(_v2_episode()).turns[0]
        assert [(t.name, t.seq, t.cached) for t in turn.tools] == [
            ("read_logs", 0, False),
            ("read_logs", 1, True),
            ("finish_x", 1, False),
        ]
        assert turn.tools[1].model_saw.startswith("[Repeated call")
        assert turn.tools[0].model_saw is None

    def test_header_fields(self):
        v = build_episode_view(_v2_episode())
        assert v.schema_version == 2
        assert v.signature == "repair:hassio:restart_required"
        assert v.provider == "ollama"
        assert v.finish_args == {"outcome": "success", "summary": "fixed"}
        assert v.system_prompt == "SYSTEM V2"
        assert v.offered_tools == ["read_logs", "finish_x"]
        assert v.input_tokens == 900 and v.output_tokens == 40
        assert v.turns[0].thinking == "NATIVE THOUGHT"
        assert v.turns[0].eval_ms == 1200.0

    def test_trailing_messages_after_last_call(self):
        data = _v2_episode()
        data["messages"].append({"role": "user", "content": "budget extended"})
        v = build_episode_view(data)
        assert [m.content for m in v.trailing] == ["budget extended"]

    def test_signature_falls_back_to_session_outcome(self):
        data = _v1_episode()
        extras = {"session_outcome": {"signature": "device:offline:dlna_dmr"}}
        assert build_episode_view(data, extras).signature == "device:offline:dlna_dmr"

    def test_non_dict_finish_args_wrapped(self):
        data = _v2_episode()
        data["finish_args"] = "text"
        assert build_episode_view(data).finish_args == {"value": "text"}

    def test_records_without_calls_are_unpaired(self):
        data = _v2_episode()
        data["llm_calls"] = []
        v = build_episode_view(data)
        assert [t.name for t in v.unpaired_tools] == [
            "read_logs",
            "read_logs",
            "finish_x",
        ]


# ---------------------------------------------------------------------------
# render_episode_html
# ---------------------------------------------------------------------------


class TestRenderEpisodeHtml:
    def test_full_long_output_and_no_details(self):
        data = _v1_episode()
        data["tool_calls"][1]["output"] = "L" * 20000 + "TAIL_MARKER"
        page = render_episode_html(build_episode_view(data))
        assert "TAIL_MARKER" in page
        assert "L" * 20000 in page
        assert "<details" not in page
        assert "<script" not in page

    def test_sections_in_reading_order(self):
        page = render_episode_html(build_episode_view(_v2_episode()))
        order = [
            'id="top"',
            "id='after'",
            'id="context"',
            "id='call-1'",
            'id="appendix"',
        ]
        positions = [page.index(marker) for marker in order]
        assert positions == sorted(positions)
        assert "href='#call-1'" in page

    def test_header_and_turn_details(self):
        page = render_episode_html(build_episode_view(_v2_episode()))
        assert "repair:hassio:restart_required" in page
        assert "qwen3.5:27b (ollama)" in page
        assert "Conclusion — finish_x" in page
        assert "<th>summary</th><td>fixed</td>" in page
        assert "900→40 tok" in page
        assert "eval 1.2 s" in page
        assert "NATIVE THOUGHT" in page
        assert "cached (repeat call)" in page
        assert "Model saw" in page

    def test_v1_page_shows_nudge_failure_and_fallback(self):
        page = render_episode_html(build_episode_view(_v1_episode()))
        assert "Injected user message" in page
        assert "Continue. Call the next tool." in page
        assert "Error (3 chars)" in page
        assert "→ exhaustion_fallback" in page
        assert "outcome-fallback" in page
        assert "Static copy" in page  # extras=None

    def test_everything_escaped(self):
        data = _v2_episode()
        evil = "<script>alert(1)</script>"
        data["tool_calls"][0]["output"] = evil
        data["finish_args"] = {"summary": evil}
        data["signature"] = evil
        data["llm_calls"][0]["thinking_native"] = evil
        page = render_episode_html(build_episode_view(data))
        assert "<script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page

    def test_extras_sections_rendered(self):
        extras = {
            "session_outcome": {
                "verification": "success",
                "verified_by": "user",
                "card_id": "card1",
                "conclusion_json": json.dumps({"outcome": "success"}),
            },
            "card": {"subject": "Restart HA?", "status": "APPROVED", "sent_at": 0},
            "runbook_usage": [{"strategy_id": "seed:seed_x.md", "outcome": "success"}],
            "learned_entries": [
                {
                    "kind": "pitfall",
                    "signature": "repair:*",
                    "text": "LEARNED TEXT",
                    "success_count": 2,
                    "failure_count": 0,
                    "retired_at": None,
                    "promoted_to": None,
                    "tool": "read_logs",
                },
                {
                    "kind": "decision_rule",
                    "signature": "repair:a",
                    "text": "RETIRED TEXT",
                    "success_count": 1,
                    "failure_count": 2,
                    "retired_at": "x",
                    "promoted_to": "repair:*",
                },
            ],
            "facts": [
                {"key": "zha_quirk", "content": "FACT TEXT", "source": "agent", "ts": 0}
            ],
            "llm_calls": [],
        }
        page = render_episode_html(build_episode_view(_v2_episode(), extras))
        for text in (
            "Restart HA?",
            "APPROVED",
            "seed:seed_x.md",
            "Learned entries (2)",
            "LEARNED TEXT",
            "promoted to repair:*",
            "FACT TEXT",
            "1970-01-01 00:00:00Z",
            "&quot;outcome&quot;: &quot;success&quot;",
        ):
            assert text in page, text
        assert "Static copy" not in page

    def test_empty_extras(self):
        extras = {"session_outcome": None, "learned_entries": []}
        page = render_episode_html(build_episode_view(_v1_episode(), extras))
        assert "No session_outcomes row" in page
        assert "None reference this episode" in page

    def test_no_calls_placeholder_and_unpaired_section(self):
        data = _v2_episode()
        data["llm_calls"] = []
        page = render_episode_html(build_episode_view(data))
        assert "No LLM calls were recorded" in page
        assert "Tool calls without a recorded LLM call" in page

    def test_history_note_and_not_executed_rendered(self):
        data = _v1_episode()
        data["tool_calls"].pop()
        data["llm_calls"][2]["request_messages"] = [_SYS]
        page = render_episode_html(build_episode_view(data))
        assert "History shrank" in page
        assert "requested, not executed" in page


# ---------------------------------------------------------------------------
# episode_extras
# ---------------------------------------------------------------------------

_EP = "11111111-2222-3333-4444-555555555555"


def _make_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE session_outcomes (episode_id TEXT PRIMARY KEY,
              signature TEXT, session_type TEXT, subject_key TEXT,
              conclusion_json TEXT, distill_payload_json TEXT, created_at TEXT,
              verification TEXT, verified_at TEXT, verified_by TEXT,
              correction TEXT, distilled_at TEXT, card_id TEXT);
            CREATE TABLE runbook_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,
              strategy_id TEXT NOT NULL, episode_id TEXT, signature TEXT,
              outcome TEXT NOT NULL, created_at TEXT, verified_at TEXT,
              verification TEXT);
            CREATE TABLE runbook_entries (id TEXT PRIMARY KEY,
              signature TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
              tool TEXT, episode_refs TEXT NOT NULL DEFAULT '[]',
              success_count INTEGER NOT NULL DEFAULT 0,
              failure_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, retired_at TEXT, promoted_to TEXT);
            CREATE TABLE agent_memory (id INTEGER PRIMARY KEY, key TEXT NOT NULL,
              content TEXT NOT NULL, source TEXT NOT NULL, ts REAL NOT NULL,
              signature TEXT, subject_key TEXT);
            CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL, episode_id TEXT, session_id INTEGER,
              model TEXT NOT NULL, provider TEXT NOT NULL, call_type TEXT NOT NULL,
              latency_ms REAL NOT NULL, ollama_eval_ms REAL, ollama_load_ms REAL,
              input_tokens INTEGER, output_tokens INTEGER, thinking_text TEXT);
            """
        )
        conn.execute(
            "INSERT INTO session_outcomes (episode_id, signature, subject_key,"
            " created_at, verification, verified_by, card_id)"
            " VALUES (?, 'repair:x', 'sk', '2026-11-02T15:00:00Z', 'success',"
            " 'user', 'card1')",
            (_EP,),
        )
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, episode_id, outcome)"
            " VALUES ('seed:seed_x.md', ?, 'success')",
            (_EP,),
        )
        for rid, refs in (("e1", [_EP]), ("e2", ["other"]), ("e3", ["other", _EP])):
            conn.execute(
                "INSERT INTO runbook_entries (id, signature, kind, text,"
                " episode_refs, created_at, updated_at)"
                " VALUES (?, 'repair:x', 'pitfall', ?, ?, ?, 'u')",
                (rid, rid, json.dumps(refs), rid),
            )
        start = 1793631600.0  # 2026-11-02T15:00:00Z
        for key, ts, sig, sk in (
            ("new_fact", start + 10, "repair:x", "sk"),
            ("old_fact", start - 10, "repair:x", "sk"),
            ("other_sig", start + 10, "repair:y", "sk"),
            ("other_subject", start + 10, "repair:x", "zz"),
            ("unscoped", start + 20, "repair:x", None),
        ):
            conn.execute(
                "INSERT INTO agent_memory (key, content, source, ts, signature,"
                " subject_key) VALUES (?, 'c', 'agent', ?, ?, ?)",
                (key, ts, sig, sk),
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO llm_calls (ts, episode_id, model, provider,"
                " call_type, latency_ms, input_tokens) VALUES (?, ?, 'm', 'p',"
                " 'chat_with_tools', 1.0, ?)",
                (float(i), _EP, 10 + i),
            )


class TestLoadEpisodeExtras:
    def test_joins(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db)
        hitl = tmp_path / "hitl"
        hitl.mkdir()
        (hitl / "card1.json").write_text(
            json.dumps({"subject": "Card title", "body": "b", "sent_at": 5})
        )
        (hitl / "card1.approved").write_text("approved by user")
        ex = load_episode_extras(db, _EP, hitl, "2026-11-02T15:00:00Z")
        assert ex["session_outcome"]["verification"] == "success"
        assert [u["strategy_id"] for u in ex["runbook_usage"]] == ["seed:seed_x.md"]
        assert sorted(e["id"] for e in ex["learned_entries"]) == ["e1", "e3"]
        assert [f["key"] for f in ex["facts"]] == ["new_fact", "unscoped"]
        assert [r["input_tokens"] for r in ex["llm_calls"]] == [10, 11]
        assert ex["card"]["subject"] == "Card title"
        assert ex["card"]["status"] == "APPROVED"
        assert ex["card"]["resolution"] == "approved by user"

    def test_facts_window_falls_back_to_created_at(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db)
        ex = load_episode_extras(db, _EP)
        assert [f["key"] for f in ex["facts"]] == ["new_fact", "unscoped"]
        assert ex["card"] is None  # no HITL dir given

    def test_unknown_episode(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db)
        ex = load_episode_extras(db, "nope")
        assert ex["session_outcome"] is None
        assert ex["card"] is None and ex["facts"] == [] and ex["runbook_usage"] == []

    def test_missing_db_and_empty_id(self, tmp_path):
        assert load_episode_extras(tmp_path / "none.db", _EP)["session_outcome"] is None
        assert load_episode_extras(tmp_path / "none.db", "")["llm_calls"] == []

    def test_db_without_tables(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(db).close()
        ex = load_episode_extras(db, _EP, None, "2026-11-02T15:00:00Z")
        assert ex["session_outcome"] is None and ex["learned_entries"] == []

    def test_db_is_not_modified(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db)
        before = db.read_bytes()
        load_episode_extras(db, _EP, None, "garbage-date")
        assert db.read_bytes() == before


class TestLoadCard:
    def test_pending_and_missing(self, tmp_path):
        (tmp_path / "c.json").write_text(json.dumps({"subject": "S"}))
        assert load_card(tmp_path, "c")["status"] == "PENDING"
        assert load_card(tmp_path, "gone")["status"] == "MISSING"

    def test_rejects_unsafe_ids(self, tmp_path):
        assert load_card(tmp_path, "../secret") is None
        assert load_card(tmp_path, None) is None
        assert load_card(None, "c") is None


class TestFindEpisodeDir:
    def test_finds_newest_folder_with_json(self, tmp_path):
        import os

        old = tmp_path / f"manual_{_EP}"
        new = tmp_path / f"session_{_EP}"
        empty = tmp_path / f"other_{_EP}"
        for d in (old, new, empty):
            d.mkdir()
        (old / "episode_data.json").write_text("{}")
        (new / "episode_data.json").write_text("{}")
        os.utime(old, (1, 1))
        assert find_episode_dir(tmp_path, _EP) == new

    def test_rejects_bad_ids_and_missing(self, tmp_path):
        assert find_episode_dir(tmp_path, "../x") is None
        assert find_episode_dir(tmp_path, "*") is None
        assert find_episode_dir(tmp_path, "") is None
        assert find_episode_dir(tmp_path, "abc") is None
        assert find_episode_dir(tmp_path / "missing", "abc") is None


def test_view_does_not_mutate_input():
    data = _v1_episode()
    snapshot = copy.deepcopy(data)
    render_episode_html(build_episode_view(data))
    assert data == snapshot
