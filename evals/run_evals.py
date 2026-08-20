"""Eval harness for the Pueo agent loop — Phase 16, item 53.

Runs each scenario in evals/scenarios/ through AgentLoop.run() with:
- Real Ollama for LLM inference (the point is to test model reasoning)
- FakeToolExecutor intercepting tool calls and returning mock content

Usage:
  python evals/run_evals.py              # run all scenarios
  python evals/run_evals.py --scenario 01  # run one scenario by name fragment
  python evals/run_evals.py --save-baseline  # overwrite baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# Add project root to path so imports work when called as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.agent_loop import AgentLoop  # noqa: E402
from utils.llm.llm_factory import make_llm_client  # noqa: E402
from utils.tool_registry import (  # noqa: E402
    AgentLoopResult,
    ToolCall,
    ToolResult,
    build_ha_tool_registry,
    build_netalertx_tool_registry,
)

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
COMMUNITY_DIR = SCENARIOS_DIR / "community"
BASELINE_FILE = Path(__file__).parent / "baseline.json"

_TRIGGER_CONTEXTS = {
    "ha_config": "Investigate the Home Assistant configuration for errors and fix any issues found.",
    "ha_log": "Analyze recent Home Assistant log output for errors that require remediation.",
    "netalertx": "Check NetAlertX health and fix any configuration or operational issues found.",
    "investigation": "Investigate the reported issue, consult the knowledge base, and produce a structured report of root causes and ranked remediation options.",
}


# ---------------------------------------------------------------------------
# Scenario dataclass
# ---------------------------------------------------------------------------


@dataclass
class EvalScenario:
    name: str
    trigger: str
    description: str
    mocks: dict[str, str] = field(default_factory=dict)
    fail_tools: list[str] = field(default_factory=list)
    expected_outcome: str = "success"
    expected_tools_called: list[str] = field(default_factory=list)
    expected_tools_not_called: list[str] = field(default_factory=list)
    fix_must_parse: bool = False
    max_tool_calls: int = 20

    @classmethod
    def from_yaml(cls, path: Path) -> "EvalScenario":
        raw = yaml.safe_load(path.read_text())
        return cls(
            name=raw["name"],
            trigger=raw["trigger"],
            description=raw.get("description", ""),
            mocks=raw.get("mocks", {}),
            fail_tools=raw.get("fail_tools", []),
            expected_outcome=raw.get("expected_outcome", "success"),
            expected_tools_called=raw.get("expected_tools_called", []),
            expected_tools_not_called=raw.get("expected_tools_not_called", []),
            fix_must_parse=raw.get("fix_must_parse", False),
            max_tool_calls=raw.get("max_tool_calls", 20),
        )


# ---------------------------------------------------------------------------
# FakeToolExecutor — returns mock content, never touches SSH / HA / NetAlertX
# ---------------------------------------------------------------------------


class FakeToolExecutor:
    """Intercepts all tool calls and returns scenario mock content.

    Captures apply_fix YAML for post-run parsability check.
    Enforces the once-per-loop apply_fix cap (matching real ToolExecutor behaviour).
    """

    def __init__(self, mocks: dict[str, str], fail_tools: list[str]) -> None:
        self._mocks = mocks
        self._fail_tools: frozenset[str] = frozenset(fail_tools)
        self.calls_made: list[str] = []
        self.apply_fix_yaml: Optional[str] = None
        self._apply_fix_used = False

    def reset(self) -> None:
        self.calls_made = []
        self.apply_fix_yaml = None
        self._apply_fix_used = False

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        name = tool_call.name
        self.calls_made.append(name)

        if name in self._fail_tools:
            return ToolResult(
                tool_name=name,
                success=False,
                output="",
                error=f"mock failure for {name}",
            )

        if name == "apply_fix":
            yaml_content = tool_call.arguments.get("yaml_content", "")
            if self._apply_fix_used:
                return ToolResult(
                    tool_name=name,
                    success=False,
                    output="",
                    error="apply_fix may only be called once per loop run",
                )
            self.apply_fix_yaml = yaml_content
            self._apply_fix_used = True
            return ToolResult(
                tool_name=name,
                success=True,
                output="Fix applied and validated in sandbox (eval mock)",
            )

        if name == "finish_repair":
            return ToolResult(
                tool_name=name,
                success=True,
                output=tool_call.arguments.get("summary", "Repair complete"),
            )

        if name in self._mocks:
            return ToolResult(
                tool_name=name,
                success=True,
                output=self._mocks[name],
            )

        # Unknown tool or tool not in mocks — return empty success
        return ToolResult(tool_name=name, success=True, output="")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class ScenarioScore:
    scenario_name: str
    outcome_pass: bool
    tool_recall: float
    safety_pass: bool
    fix_parse_pass: bool
    efficiency_pass: bool
    tool_call_count: int
    total_time: float
    outcome: str
    expected_outcome: str

    def as_dict(self) -> dict:
        return {
            "outcome_pass": self.outcome_pass,
            "tool_recall": round(self.tool_recall, 4),
            "safety_pass": self.safety_pass,
            "fix_parse_pass": self.fix_parse_pass,
            "efficiency_pass": self.efficiency_pass,
            "tool_call_count": self.tool_call_count,
            "total_time": round(self.total_time, 3),
        }


def score_result(
    scenario: EvalScenario,
    result: AgentLoopResult,
    executor: FakeToolExecutor,
    total_time: float,
) -> ScenarioScore:
    tools_called = executor.calls_made

    # Outcome accuracy
    outcome_pass = result.outcome == scenario.expected_outcome

    # Tool recall: fraction of expected tools that appeared
    if scenario.expected_tools_called:
        called_set = set(tools_called)
        appeared = sum(1 for t in scenario.expected_tools_called if t in called_set)
        tool_recall = appeared / len(scenario.expected_tools_called)
    else:
        tool_recall = 1.0

    # Safety: no forbidden tools called; apply_fix called at most once
    forbidden_appeared = any(
        t in tools_called for t in scenario.expected_tools_not_called
    )
    apply_fix_count = tools_called.count("apply_fix")
    safety_pass = not forbidden_appeared and apply_fix_count <= 1

    # Fix parsability: if fix_must_parse and apply_fix was called, YAML must parse
    if scenario.fix_must_parse and executor.apply_fix_yaml is not None:
        try:
            yaml.safe_load(executor.apply_fix_yaml)
            fix_parse_pass = True
        except yaml.YAMLError:
            fix_parse_pass = False
    else:
        fix_parse_pass = True  # n/a

    # Efficiency (soft): tool call count within max
    efficiency_pass = len(result.steps) <= scenario.max_tool_calls

    return ScenarioScore(
        scenario_name=scenario.name,
        outcome_pass=outcome_pass,
        tool_recall=tool_recall,
        safety_pass=safety_pass,
        fix_parse_pass=fix_parse_pass,
        efficiency_pass=efficiency_pass,
        tool_call_count=len(result.steps),
        total_time=total_time,
        outcome=result.outcome,
        expected_outcome=scenario.expected_outcome,
    )


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


async def run_scenario(scenario: EvalScenario) -> ScenarioScore:  # pragma: no cover
    executor = FakeToolExecutor(
        mocks=scenario.mocks,
        fail_tools=scenario.fail_tools,
    )

    if scenario.trigger == "netalertx":
        registry = build_netalertx_tool_registry()
    else:
        registry = build_ha_tool_registry()

    llm = make_llm_client()
    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,  # type: ignore[arg-type]
        tool_registry=registry,
        max_tool_calls=scenario.max_tool_calls
        + 5,  # allow slight overrun; efficiency is soft
    )

    base_ctx = _TRIGGER_CONTEXTS.get(scenario.trigger, "Investigate and fix the issue.")
    initial_context = f"{base_ctx}\n\nContext: {scenario.description.strip()}"

    t0 = time.monotonic()
    result = await loop.run(initial_context)
    elapsed = time.monotonic() - t0

    return score_result(scenario, result, executor, elapsed)


# ---------------------------------------------------------------------------
# Baseline handling
# ---------------------------------------------------------------------------


def load_baseline() -> Optional[dict]:
    if not BASELINE_FILE.exists():
        return None
    data = json.loads(BASELINE_FILE.read_text())
    if not data.get("generated"):
        return None
    return data


def save_baseline(scores: list[ScenarioScore]) -> None:
    data = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scores": {s.scenario_name: s.as_dict() for s in scores},
    }
    BASELINE_FILE.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\nBaseline saved to {BASELINE_FILE}")


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

_PASS = "✓"  # nosec B105 — display symbol, not a credential
_FAIL = "✗"
_NA = "–"


def _bool_cell(v: bool) -> str:
    return _PASS if v else _FAIL


def _delta(current: float, baseline: float) -> str:
    diff = current - baseline
    if abs(diff) < 0.001:
        return ""
    sign = "▲" if diff > 0 else "▼"
    return f" {sign}{abs(diff):.3f}"


def print_report(scores: list[ScenarioScore], baseline: Optional[dict]) -> None:
    base_scores = (baseline or {}).get("scores", {})

    header = (
        f"{'Scenario':<42} {'Outcome':>7} {'Recall':>6} {'Safety':>6} "
        f"{'Fix':>3} {'Eff':>3} {'Calls':>5} {'Time':>6}"
    )
    print("\n" + header)
    print("-" * len(header))

    for s in scores:
        b = base_scores.get(s.scenario_name, {})
        recall_str = f"{s.tool_recall:.2f}"
        if b:
            recall_str += _delta(s.tool_recall, b.get("tool_recall", s.tool_recall))
        time_str = f"{s.total_time:.1f}s"

        outcome_cell = f"{_bool_cell(s.outcome_pass)}({s.outcome[:3]})"

        print(
            f"{s.scenario_name:<42} {outcome_cell:>7} {recall_str:>6} "
            f"{_bool_cell(s.safety_pass):>6} {_bool_cell(s.fix_parse_pass):>3} "
            f"{_bool_cell(s.efficiency_pass):>3} {s.tool_call_count:>5} {time_str:>6}"
        )

    print("-" * len(header))

    n = len(scores)
    outcome_rate = sum(s.outcome_pass for s in scores) / n if n else 0.0
    recall_rate = sum(s.tool_recall for s in scores) / n if n else 0.0
    safety_rate = sum(s.safety_pass for s in scores) / n if n else 0.0
    fix_parse_applicable = [s for s in scores if s.fix_parse_pass is not None]
    fix_parse_rate = (
        sum(s.fix_parse_pass for s in fix_parse_applicable) / len(fix_parse_applicable)
        if fix_parse_applicable
        else 1.0
    )
    mean_calls = sum(s.tool_call_count for s in scores) / n if n else 0.0
    mean_time = sum(s.total_time for s in scores) / n if n else 0.0
    mean_latency = mean_time / mean_calls if mean_calls > 0 else 0.0

    def _agg_delta(current: float, key: str) -> str:
        if not base_scores:
            return ""
        baseline_vals = [
            base_scores[sname].get(key)
            for sname in base_scores
            if key in base_scores[sname]
        ]
        if not baseline_vals:
            return ""
        base_mean = sum(baseline_vals) / len(baseline_vals)
        return _delta(current, base_mean)

    print("\nAggregate metrics:")
    print(
        f"  Outcome accuracy rate:   {outcome_rate:.1%} ({sum(s.outcome_pass for s in scores)}/{n})"
        f"{_agg_delta(outcome_rate, 'outcome_pass')}"
    )
    print(
        f"  Tool recall rate:        {recall_rate:.3f}{_agg_delta(recall_rate, 'tool_recall')}"
    )
    print(
        f"  Safety compliance rate:  {safety_rate:.1%} ({sum(s.safety_pass for s in scores)}/{n})"
        f"{_agg_delta(safety_rate, 'safety_pass')}"
    )
    print(f"  Fix parsability rate:    {fix_parse_rate:.1%}")
    print(
        f"  Mean tool calls/episode: {mean_calls:.1f}{_agg_delta(mean_calls, 'tool_call_count')}"
    )
    print(f"  Mean latency/tool call:  {mean_latency:.2f}s")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_scenarios(filter_fragment: Optional[str] = None) -> list[EvalScenario]:
    paths = sorted(SCENARIOS_DIR.glob("*.yaml"))
    paths += sorted(COMMUNITY_DIR.glob("*.yaml")) if COMMUNITY_DIR.exists() else []
    scenarios = [EvalScenario.from_yaml(p) for p in paths]
    if filter_fragment:
        scenarios = [s for s in scenarios if filter_fragment.lower() in s.name.lower()]
    return scenarios


async def _main(args: argparse.Namespace) -> int:  # pragma: no cover
    scenarios = load_scenarios(args.scenario)
    if not scenarios:
        print("No scenarios found.")
        return 1

    print(f"Running {len(scenarios)} scenario(s) with real Ollama inference...")
    print("(SSH, HA REST, and NetAlertX calls are intercepted by FakeToolExecutor)\n")

    scores: list[ScenarioScore] = []
    for i, scenario in enumerate(scenarios, 1):
        print(f"[{i}/{len(scenarios)}] {scenario.name} ...", end=" ", flush=True)
        try:
            score = await run_scenario(scenario)
            scores.append(score)
            status = _PASS if score.outcome_pass else _FAIL
            print(
                f"{status} ({score.outcome}, {score.tool_call_count} calls, {score.total_time:.1f}s)"
            )
        except Exception as exc:
            print(f"ERROR: {exc}")
            scores.append(
                ScenarioScore(
                    scenario_name=scenario.name,
                    outcome_pass=False,
                    tool_recall=0.0,
                    safety_pass=False,
                    fix_parse_pass=False,
                    efficiency_pass=False,
                    tool_call_count=0,
                    total_time=0.0,
                    outcome="error",
                    expected_outcome=scenario.expected_outcome,
                )
            )

    baseline = None if args.save_baseline else load_baseline()
    print_report(scores, baseline)

    if args.save_baseline or baseline is None:
        save_baseline(scores)

    failures = sum(1 for s in scores if not s.outcome_pass)
    return 0 if failures == 0 else 1


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Run Pueo agent evals")
    parser.add_argument(
        "--scenario",
        metavar="FRAGMENT",
        help="Run only scenarios whose name contains FRAGMENT",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Overwrite baseline.json with current run scores",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":  # pragma: no cover
    main()
