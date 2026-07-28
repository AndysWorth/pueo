"""Tests for the eval harness components — FakeToolExecutor, EvalScenario, scoring."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# EvalScenario
# ---------------------------------------------------------------------------


class TestEvalScenario:
    def test_from_yaml_minimal(self, tmp_path):
        from evals.run_evals import EvalScenario

        src = tmp_path / "scenario.yaml"
        src.write_text("name: test_scenario\ntrigger: ha_config\ndescription: A test\n")
        s = EvalScenario.from_yaml(src)
        assert s.name == "test_scenario"
        assert s.trigger == "ha_config"
        assert s.description == "A test"
        assert s.mocks == {}
        assert s.fail_tools == []
        assert s.expected_outcome == "success"
        assert s.fix_must_parse is False
        assert s.max_tool_calls == 20

    def test_from_yaml_full(self, tmp_path):
        from evals.run_evals import EvalScenario

        data = {
            "name": "full_scenario",
            "trigger": "ha_log",
            "description": "Full scenario",
            "mocks": {"read_logs": "some log output"},
            "fail_tools": ["apply_fix"],
            "expected_outcome": "fix_failed",
            "expected_tools_called": ["read_logs", "apply_fix"],
            "expected_tools_not_called": ["run_ha_command"],
            "fix_must_parse": True,
            "max_tool_calls": 5,
        }
        src = tmp_path / "full.yaml"
        src.write_text(yaml.dump(data))
        s = EvalScenario.from_yaml(src)
        assert s.fail_tools == ["apply_fix"]
        assert s.expected_outcome == "fix_failed"
        assert s.expected_tools_called == ["read_logs", "apply_fix"]
        assert s.expected_tools_not_called == ["run_ha_command"]
        assert s.fix_must_parse is True
        assert s.max_tool_calls == 5

    def test_all_scenario_files_parse(self):
        from evals.run_evals import SCENARIOS_DIR, EvalScenario

        paths = list(SCENARIOS_DIR.glob("*.yaml"))
        assert len(paths) >= 10, f"Expected ≥10 scenarios, found {len(paths)}"
        for p in paths:
            s = EvalScenario.from_yaml(p)
            assert s.name, f"{p.name}: missing name"
            assert s.trigger in (
                "ha_config",
                "ha_log",
                "netalertx",
            ), f"{p.name}: unknown trigger {s.trigger!r}"
            assert s.expected_outcome in (
                "success",
                "fix_failed",
                "exhausted",
            ), f"{p.name}: unknown outcome {s.expected_outcome!r}"


# ---------------------------------------------------------------------------
# FakeToolExecutor
# ---------------------------------------------------------------------------


class TestFakeToolExecutor:
    def _make_tc(self, name: str, arguments: dict | None = None):
        from utils.tool_registry import ToolCall

        return ToolCall(name=name, arguments=arguments or {})

    def test_returns_mocked_content(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={"read_config": "yaml: content"}, fail_tools=[])
        result = _run(exe.execute(self._make_tc("read_config")))
        assert result.success is True
        assert result.output == "yaml: content"
        assert result.tool_name == "read_config"

    def test_unknown_tool_returns_empty_success(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=[])
        result = _run(exe.execute(self._make_tc("query_knowledge")))
        assert result.success is True
        assert result.output == ""

    def test_fail_tool_returns_failure(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=["apply_fix"])
        result = _run(
            exe.execute(
                self._make_tc(
                    "apply_fix", {"yaml_content": "x: 1", "description": "fix"}
                )
            )
        )
        assert result.success is False
        assert "mock failure" in result.error

    def test_apply_fix_captures_yaml(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=[])
        result = _run(
            exe.execute(
                self._make_tc(
                    "apply_fix",
                    {
                        "yaml_content": "homeassistant:\n  name: My Home\n",
                        "description": "add key",
                    },
                )
            )
        )
        assert result.success is True
        assert exe.apply_fix_yaml == "homeassistant:\n  name: My Home\n"

    def test_apply_fix_once_per_loop_cap(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=[])
        _run(
            exe.execute(
                self._make_tc(
                    "apply_fix", {"yaml_content": "x: 1", "description": "first"}
                )
            )
        )
        result = _run(
            exe.execute(
                self._make_tc(
                    "apply_fix", {"yaml_content": "x: 2", "description": "second"}
                )
            )
        )
        assert result.success is False
        assert "once" in result.error

    def test_finish_repair_returns_success(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=[])
        result = _run(
            exe.execute(
                self._make_tc(
                    "finish_repair",
                    {"summary": "all done", "action_taken": "no_fix_needed"},
                )
            )
        )
        assert result.success is True
        assert result.output == "all done"

    def test_calls_made_tracks_order(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(
            mocks={"read_config": "cfg", "read_logs": "logs"}, fail_tools=[]
        )
        _run(exe.execute(self._make_tc("read_config")))
        _run(exe.execute(self._make_tc("read_logs")))
        _run(
            exe.execute(
                self._make_tc(
                    "finish_repair",
                    {"summary": "done", "action_taken": "no_fix_needed"},
                )
            )
        )
        assert exe.calls_made == ["read_config", "read_logs", "finish_repair"]

    def test_reset_clears_state(self):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=[])
        _run(
            exe.execute(
                self._make_tc(
                    "apply_fix", {"yaml_content": "x: 1", "description": "fix"}
                )
            )
        )
        exe.reset()
        assert exe.calls_made == []
        assert exe.apply_fix_yaml is None
        # apply_fix should work again after reset
        result = _run(
            exe.execute(
                self._make_tc(
                    "apply_fix", {"yaml_content": "x: 2", "description": "fix2"}
                )
            )
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# score_result
# ---------------------------------------------------------------------------


class TestScoreResult:
    def _make_result(self, outcome: str, tool_names: list[str]):
        from utils.tool_registry import AgentLoopResult, AgentStep, ToolCall, ToolResult

        steps = [
            AgentStep(
                step_number=i + 1,
                tool_call=ToolCall(name=name, arguments={}),
                tool_result=ToolResult(tool_name=name, success=True, output=""),
                timestamp=float(i),
            )
            for i, name in enumerate(tool_names)
        ]
        return AgentLoopResult(outcome=outcome, steps=steps)  # type: ignore[arg-type]

    def _make_executor(self, calls: list[str], apply_yaml: str | None = None):
        from evals.run_evals import FakeToolExecutor

        exe = FakeToolExecutor(mocks={}, fail_tools=[])
        exe.calls_made = list(calls)
        exe.apply_fix_yaml = apply_yaml
        return exe

    def test_outcome_pass(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t",
            trigger="ha_config",
            description="",
            expected_outcome="success",
        )
        result = self._make_result("success", ["read_config", "finish_repair"])
        exe = self._make_executor(["read_config", "finish_repair"])
        score = score_result(scenario, result, exe, 1.0)
        assert score.outcome_pass is True

    def test_outcome_fail(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t",
            trigger="ha_config",
            description="",
            expected_outcome="success",
        )
        result = self._make_result("exhausted", [])
        exe = self._make_executor([])
        score = score_result(scenario, result, exe, 1.0)
        assert score.outcome_pass is False

    def test_tool_recall_full(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t",
            trigger="ha_config",
            description="",
            expected_tools_called=["read_config", "apply_fix", "finish_repair"],
        )
        result = self._make_result(
            "success", ["read_config", "apply_fix", "finish_repair"]
        )
        exe = self._make_executor(["read_config", "apply_fix", "finish_repair"])
        score = score_result(scenario, result, exe, 1.0)
        assert score.tool_recall == 1.0

    def test_tool_recall_partial(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t",
            trigger="ha_config",
            description="",
            expected_tools_called=["read_config", "apply_fix", "finish_repair"],
        )
        # Only read_config was called (2 missing)
        result = self._make_result("exhausted", ["read_config"])
        exe = self._make_executor(["read_config"])
        score = score_result(scenario, result, exe, 1.0)
        assert abs(score.tool_recall - 1 / 3) < 0.01

    def test_tool_recall_empty_expected(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(name="t", trigger="ha_config", description="")
        result = self._make_result("success", [])
        exe = self._make_executor([])
        score = score_result(scenario, result, exe, 1.0)
        assert score.tool_recall == 1.0

    def test_safety_pass_no_forbidden(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t",
            trigger="ha_config",
            description="",
            expected_tools_not_called=["apply_fix"],
        )
        result = self._make_result("success", ["finish_repair"])
        exe = self._make_executor(["finish_repair"])
        score = score_result(scenario, result, exe, 1.0)
        assert score.safety_pass is True

    def test_safety_fail_forbidden_called(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t",
            trigger="ha_config",
            description="",
            expected_tools_not_called=["apply_fix"],
        )
        result = self._make_result("success", ["apply_fix", "finish_repair"])
        exe = self._make_executor(["apply_fix", "finish_repair"])
        score = score_result(scenario, result, exe, 1.0)
        assert score.safety_pass is False

    def test_safety_fail_apply_fix_called_twice(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(name="t", trigger="ha_config", description="")
        result = self._make_result(
            "success", ["apply_fix", "apply_fix", "finish_repair"]
        )
        exe = self._make_executor(["apply_fix", "apply_fix", "finish_repair"])
        score = score_result(scenario, result, exe, 1.0)
        assert score.safety_pass is False

    def test_fix_parse_pass_valid_yaml(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t", trigger="ha_config", description="", fix_must_parse=True
        )
        result = self._make_result("success", ["apply_fix", "finish_repair"])
        exe = self._make_executor(
            ["apply_fix", "finish_repair"],
            apply_yaml="homeassistant:\n  name: Home\n",
        )
        score = score_result(scenario, result, exe, 1.0)
        assert score.fix_parse_pass is True

    def test_fix_parse_fail_invalid_yaml(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t", trigger="ha_config", description="", fix_must_parse=True
        )
        result = self._make_result("success", ["apply_fix", "finish_repair"])
        exe = self._make_executor(
            ["apply_fix", "finish_repair"],
            apply_yaml="key: {\n  unclosed bracket",
        )
        score = score_result(scenario, result, exe, 1.0)
        assert score.fix_parse_pass is False

    def test_fix_parse_na_when_not_required(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t", trigger="ha_config", description="", fix_must_parse=False
        )
        result = self._make_result("success", ["finish_repair"])
        exe = self._make_executor(["finish_repair"], apply_yaml=None)
        score = score_result(scenario, result, exe, 1.0)
        # n/a case — must pass (True by default)
        assert score.fix_parse_pass is True

    def test_efficiency_pass(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t", trigger="ha_config", description="", max_tool_calls=5
        )
        result = self._make_result(
            "success", ["read_config", "apply_fix", "finish_repair"]
        )
        exe = self._make_executor(["read_config", "apply_fix", "finish_repair"])
        score = score_result(scenario, result, exe, 1.0)
        assert score.efficiency_pass is True

    def test_efficiency_fail_over_budget(self):
        from evals.run_evals import EvalScenario, score_result

        scenario = EvalScenario(
            name="t", trigger="ha_config", description="", max_tool_calls=2
        )
        result = self._make_result(
            "success", ["read_config", "read_logs", "apply_fix", "finish_repair"]
        )
        exe = self._make_executor(
            ["read_config", "read_logs", "apply_fix", "finish_repair"]
        )
        score = score_result(scenario, result, exe, 1.0)
        assert score.efficiency_pass is False


# ---------------------------------------------------------------------------
# load_scenarios
# ---------------------------------------------------------------------------


class TestLoadScenarios:
    def test_loads_all_scenarios(self):
        from evals.run_evals import load_scenarios

        scenarios = load_scenarios()
        assert len(scenarios) >= 10

    def test_filter_by_fragment(self):
        from evals.run_evals import load_scenarios

        scenarios = load_scenarios(filter_fragment="netalertx")
        assert all("netalertx" in s.name for s in scenarios)
        assert len(scenarios) >= 2

    def test_filter_no_match_returns_empty(self):
        from evals.run_evals import load_scenarios

        scenarios = load_scenarios(filter_fragment="zzz_no_match_zzz")
        assert scenarios == []


# ---------------------------------------------------------------------------
# Baseline JSON roundtrip
# ---------------------------------------------------------------------------


class TestBaseline:
    def test_save_and_load_baseline(self, tmp_path, monkeypatch):
        import evals.run_evals as harness
        from evals.run_evals import ScenarioScore, load_baseline, save_baseline

        monkeypatch.setattr(harness, "BASELINE_FILE", tmp_path / "baseline.json")

        score = ScenarioScore(
            scenario_name="test_scenario",
            outcome_pass=True,
            tool_recall=0.8,
            safety_pass=True,
            fix_parse_pass=True,
            efficiency_pass=True,
            tool_call_count=4,
            total_time=3.5,
            outcome="success",
            expected_outcome="success",
        )
        save_baseline([score])
        loaded = load_baseline()
        assert loaded is not None
        assert "test_scenario" in loaded["scores"]
        assert loaded["scores"]["test_scenario"]["tool_recall"] == pytest.approx(
            0.8, abs=0.001
        )

    def test_load_baseline_returns_none_for_placeholder(self, tmp_path, monkeypatch):
        import evals.run_evals as harness
        from evals.run_evals import load_baseline

        placeholder = tmp_path / "baseline.json"
        placeholder.write_text('{"generated": null, "scores": {}}\n')
        monkeypatch.setattr(harness, "BASELINE_FILE", placeholder)

        assert load_baseline() is None

    def test_load_baseline_returns_none_when_missing(self, tmp_path, monkeypatch):
        import evals.run_evals as harness
        from evals.run_evals import load_baseline

        monkeypatch.setattr(harness, "BASELINE_FILE", tmp_path / "nonexistent.json")
        assert load_baseline() is None


# ---------------------------------------------------------------------------
# Report helpers: _bool_cell, _delta, print_report
# ---------------------------------------------------------------------------


class TestReportHelpers:
    def test_bool_cell_true(self):
        from evals.run_evals import _bool_cell

        assert _bool_cell(True) == "✓"

    def test_bool_cell_false(self):
        from evals.run_evals import _bool_cell

        assert _bool_cell(False) == "✗"

    def test_delta_positive(self):
        from evals.run_evals import _delta

        result = _delta(1.0, 0.5)
        assert "▲" in result
        assert "0.500" in result

    def test_delta_negative(self):
        from evals.run_evals import _delta

        result = _delta(0.5, 1.0)
        assert "▼" in result
        assert "0.500" in result

    def test_delta_equal(self):
        from evals.run_evals import _delta

        assert _delta(1.0, 1.0) == ""

    def test_delta_below_threshold(self):
        from evals.run_evals import _delta

        assert _delta(1.0, 1.0009) == ""


class TestPrintReport:
    def _make_score(
        self,
        name: str = "test_scenario",
        outcome: str = "success",
        outcome_pass: bool = True,
        tool_recall: float = 1.0,
        safety_pass: bool = True,
        fix_parse_pass: bool = True,
        efficiency_pass: bool = True,
        tool_call_count: int = 3,
        total_time: float = 2.5,
    ):
        from evals.run_evals import ScenarioScore

        return ScenarioScore(
            scenario_name=name,
            outcome_pass=outcome_pass,
            tool_recall=tool_recall,
            safety_pass=safety_pass,
            fix_parse_pass=fix_parse_pass,
            efficiency_pass=efficiency_pass,
            tool_call_count=tool_call_count,
            total_time=total_time,
            outcome=outcome,
            expected_outcome="success",
        )

    def test_print_report_no_baseline(self, capsys):
        from evals.run_evals import print_report

        scores = [self._make_score(), self._make_score(name="other_scenario")]
        print_report(scores, None)
        out = capsys.readouterr().out
        assert "test_scenario" in out
        assert "Aggregate" in out
        assert "Outcome accuracy" in out

    def test_print_report_with_baseline_shows_delta(self, capsys):
        from evals.run_evals import print_report

        scores = [self._make_score(tool_recall=0.9)]
        baseline = {
            "generated": "2026-01-01T00:00:00Z",
            "scores": {
                "test_scenario": {
                    "tool_recall": 0.5,
                    "outcome_pass": True,
                    "safety_pass": True,
                    "fix_parse_pass": True,
                    "efficiency_pass": True,
                    "tool_call_count": 3,
                    "total_time": 2.5,
                }
            },
        }
        print_report(scores, baseline)
        out = capsys.readouterr().out
        assert "▲" in out  # delta shown for improved recall

    def test_print_report_empty_scores(self, capsys):
        from evals.run_evals import print_report

        print_report([], None)
        out = capsys.readouterr().out
        assert "Aggregate" in out

    def test_print_report_failure_case(self, capsys):
        from evals.run_evals import print_report

        scores = [
            self._make_score(outcome="exhausted", outcome_pass=False, tool_call_count=0)
        ]
        print_report(scores, None)
        out = capsys.readouterr().out
        assert "✗" in out

    def test_print_report_baseline_key_missing(self, capsys):
        from evals.run_evals import print_report

        scores = [self._make_score(name="new_scenario")]
        baseline = {
            "generated": "2026-01-01T00:00:00Z",
            "scores": {
                "different_scenario": {
                    "tool_recall": 0.5,
                    "outcome_pass": True,
                    "safety_pass": True,
                    "fix_parse_pass": True,
                    "efficiency_pass": True,
                    "tool_call_count": 3,
                    "total_time": 2.5,
                }
            },
        }
        print_report(scores, baseline)
        out = capsys.readouterr().out
        assert "Aggregate" in out

    def test_print_report_baseline_entries_have_no_keys(self, capsys):
        from evals.run_evals import print_report

        # Baseline entries exist but are empty dicts — _agg_delta's baseline_vals list
        # will be empty for every key, exercising the `if not baseline_vals: return ""`
        # branch inside _agg_delta.
        scores = [self._make_score()]
        baseline = {
            "generated": "2026-01-01T00:00:00Z",
            "scores": {"some_scenario": {}},
        }
        print_report(scores, baseline)
        out = capsys.readouterr().out
        assert "Aggregate" in out
