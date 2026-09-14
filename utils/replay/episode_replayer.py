"""Episode replayer — loads episode_data.json and re-runs AgentLoop deterministically."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

from utils.replay.replay_client import ReplayExhaustedError, ReplayLLMClient
from utils.replay.replay_executor import (
    ReplayDivergenceError,
    ReplayExhaustedError as _ExecutorExhausted,
    ReplayToolExecutor,
)


@dataclasses.dataclass
class ReplayResult:
    episode_id: str
    original_outcome: str
    replay_outcome: str
    matched: bool
    diverged_at_llm_call: int | None  # None if clean
    diverged_at_tool_call: int | None  # None if clean
    error: str | None


def _registry_for_trigger(trigger: str) -> Any:
    """Return the appropriate ToolRegistry based on the episode trigger."""
    from utils.agent.tool_registry import (
        build_chat_tool_registry,
        build_ha_tool_registry,
    )

    _netalertx_triggers = {"netalertx", "health_diagnosis", "installer_diagnosis"}
    if trigger in _netalertx_triggers:
        from utils.agent.tool_registry import build_netalertx_tool_registry

        return build_netalertx_tool_registry()
    if trigger == "manual":
        return build_chat_tool_registry()
    return build_ha_tool_registry()


class EpisodeReplayer:
    """Load and deterministically replay a recorded agent loop episode."""

    def load_episode_data(self, episode_dir: Path) -> dict[str, Any]:
        data_file = episode_dir / "episode_data.json"
        if not data_file.exists():
            raise FileNotFoundError(
                f"episode_data.json not found in {episode_dir}. "
                "Run the agent with capture_llm=True to generate it."
            )
        return json.loads(data_file.read_text(encoding="utf-8"))

    def find_episode_dir(self, data_dir: Path, episode_id: str, trigger: str) -> Path:
        return data_dir / "debug_episodes" / f"{trigger}_{episode_id}"

    def find_episode_dir_by_id(self, data_dir: Path, episode_id: str) -> Path | None:
        """Search debug_episodes/ for a directory ending with the episode ID."""
        base = data_dir / "debug_episodes"
        if not base.exists():
            return None
        for child in base.iterdir():
            if child.is_dir() and child.name.endswith(f"_{episode_id}"):
                return child
        return None

    async def run_deterministic(
        self,
        data: dict[str, Any],
        tool_registry: Any | None = None,
        system_prompt: Any = None,
    ) -> ReplayResult:
        """Re-run the agent loop using pre-recorded LLM responses and tool results.

        Parameters
        ----------
        data:
            Parsed episode_data.json dict.
        tool_registry:
            ToolRegistry to expose to AgentLoop.  Defaults to the registry
            appropriate for the recorded trigger.
        system_prompt:
            Override the system prompt.  Defaults to the standard prompt for
            the terminal tool inferred from the recorded LLM responses.
        """
        from utils.agent.agent_loop import AgentLoop, _UNSET_PROMPT

        episode_id = data.get("episode_id", "")
        original_outcome = data.get("outcome", "")
        trigger = data.get("trigger", "manual")
        model = data.get("model", "")
        initial_context = data.get("initial_context", "")
        llm_calls = data.get("llm_calls", [])
        tool_calls = data.get("tool_calls", [])

        if tool_registry is None:
            tool_registry = _registry_for_trigger(trigger)

        llm_client = ReplayLLMClient(llm_calls)
        tool_executor = ReplayToolExecutor(tool_calls)  # type: ignore[assignment]

        # Infer terminal tool from the last LLM call's outcome_path or tool calls.
        terminal_tool = _infer_terminal_tool(llm_calls, original_outcome)

        _sp = system_prompt if system_prompt is not None else _UNSET_PROMPT
        loop = AgentLoop(
            llm_client=llm_client,  # type: ignore[arg-type]
            tool_executor=tool_executor,  # type: ignore[arg-type]
            tool_registry=tool_registry,
            model=model,
            system_prompt=_sp,
            trigger=trigger,
            terminal_tool_name=terminal_tool,
            capture_llm=False,
            # Disable DB recording and knowledge injection for replay
            db_path=None,
            knowledge_store=None,
        )

        try:
            result = await loop.run(initial_context)
        except (ReplayExhaustedError, _ExecutorExhausted) as exc:
            return ReplayResult(
                episode_id=episode_id,
                original_outcome=original_outcome,
                replay_outcome="replay_exhausted",
                matched=False,
                diverged_at_llm_call=llm_client._pos,
                diverged_at_tool_call=tool_executor._pos,
                error=str(exc),
            )
        except ReplayDivergenceError as exc:
            return ReplayResult(
                episode_id=episode_id,
                original_outcome=original_outcome,
                replay_outcome="replay_diverged",
                matched=False,
                diverged_at_llm_call=llm_client._pos,
                diverged_at_tool_call=tool_executor._pos,
                error=str(exc),
            )
        except Exception as exc:
            return ReplayResult(
                episode_id=episode_id,
                original_outcome=original_outcome,
                replay_outcome="replay_error",
                matched=False,
                diverged_at_llm_call=None,
                diverged_at_tool_call=None,
                error=str(exc),
            )

        matched = result.outcome == original_outcome
        return ReplayResult(
            episode_id=episode_id,
            original_outcome=original_outcome,
            replay_outcome=result.outcome,
            matched=matched,
            diverged_at_llm_call=None if matched else llm_client._pos,
            diverged_at_tool_call=None if matched else tool_executor._pos,
            error=None,
        )


def _infer_terminal_tool(llm_calls: list[dict], outcome: str) -> str:
    """Infer the terminal tool name from the recorded LLM response sequence."""
    _outcome_to_terminal = {
        "success": "finish_repair",
        "exhausted": "finish_repair",
        "stuck": "finish_repair",
    }
    # Look for finish_* calls in the last LLM response's tool calls.
    for call in reversed(llm_calls):
        for tc in call.get("response_tool_calls") or []:
            name = tc.get("function", {}).get("name", "")
            if name.startswith("finish_"):
                return name
    return _outcome_to_terminal.get(outcome, "finish_repair")
