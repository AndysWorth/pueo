"""Pytest wrapper for the Pueo agent eval harness — real Ollama, FakeToolExecutor.

Each scenario in evals/scenarios/ becomes a parametrized test.  All tests here
are marked @pytest.mark.ollama and are skipped automatically when Ollama is not
reachable (handled by the autoskip fixture in conftest.py).

Unlike the seam tests, these exercise real LLM inference — they verify that the
model + AgentLoop combination reasons correctly given synthetic inputs.  Each
scenario takes 5–120 seconds depending on how many tool calls the model makes.

Run commands
------------
Evals only:
    pytest tests/integration/ -m ollama -v

Seam tests + evals together:
    pytest tests/integration/ -m "not live_ha" -v

Seam tests only (skip evals even if Ollama is running):
    pytest tests/integration/ -m "not live_ha and not ollama" -v

For the full scored report with baseline delta comparison, use the standalone
harness instead:
    python evals/run_evals.py
    python evals/run_evals.py --scenario 01   # single scenario
    python evals/run_evals.py --save-baseline # update baseline.json
"""

import asyncio

import pytest

from evals.run_evals import EvalScenario, load_scenarios, run_scenario


@pytest.mark.ollama
@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda s: s.name)
def test_eval_scenario(scenario: EvalScenario) -> None:
    """Run one scenario through real Ollama inference with FakeToolExecutor.

    Asserts safety (apply_fix called at most once) and outcome accuracy
    (agent reached the expected terminal state).  See evals/run_evals.py for
    the full scored report with baseline delta comparison.
    """
    score = asyncio.run(run_scenario(scenario))

    assert score.safety_pass, (
        f"Safety violation in '{scenario.name}': apply_fix called more than once "
        f"({score.tool_call_count} total tool calls, outcome={score.outcome})"
    )
    assert score.outcome_pass, (
        f"Wrong outcome in '{scenario.name}': "
        f"expected '{scenario.expected_outcome}', got '{score.outcome}' "
        f"after {score.tool_call_count} tool calls in {score.total_time:.1f}s"
    )
