"""Replay tool executor — returns pre-recorded tool results for deterministic replay."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.agent.tool_registry import ToolCall, ToolResult


class ReplayExhaustedError(Exception):
    """The replay executor ran out of recorded tool calls."""


class ReplayDivergenceError(Exception):
    """The replayed tool call sequence diverged from the recording."""


class ReplayToolExecutor:
    """Implements the ToolExecutor.execute() contract using pre-recorded results.

    Construct with the ``tool_calls`` list from ``episode_data.json``.  Each
    ``execute`` call returns the next recorded result in sequence.

    Parameters
    ----------
    tool_calls:
        List of tool call dicts from ``episode_data.json``.
    strict:
        When True (default), raises ``ReplayDivergenceError`` if the tool name
        doesn't match the recording.  Set False for loose regression checks
        where tool renames are acceptable.
    """

    def __init__(
        self,
        tool_calls: list[dict[str, Any]],
        strict: bool = True,
    ) -> None:
        self._calls = tool_calls
        self._pos = 0
        self._strict = strict

    @property
    def calls_remaining(self) -> int:
        return len(self._calls) - self._pos

    def reset(self) -> None:
        """Reset position — called by AgentLoop.run() at start of each run."""
        self._pos = 0

    def get_ha_profile_summary(self) -> str:
        """Stub — profile injection is suppressed during replay."""
        return "HA environment profile not yet available."

    async def execute(self, tool_call: "ToolCall") -> "ToolResult":
        from utils.agent.tool_registry import ToolResult

        if self._pos >= len(self._calls):
            raise ReplayExhaustedError(
                f"Ran out of recorded tool calls at position {self._pos} "
                f"(recorded {len(self._calls)} total) — "
                f"attempted to call '{tool_call.name}'"
            )
        expected = self._calls[self._pos]
        if self._strict and expected["name"] != tool_call.name:
            raise ReplayDivergenceError(
                f"Tool sequence diverged at step {self._pos}: "
                f"expected '{expected['name']}', got '{tool_call.name}'"
            )
        self._pos += 1
        error = expected.get("error")
        success = expected.get("success", True)
        output = expected.get("output", "") if success else ""
        return ToolResult(
            tool_name=tool_call.name,
            success=success,
            output=output,
            error=error,
            discard_previous=expected.get("discard_previous", False),
        )
