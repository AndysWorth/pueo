"""Agent loop controller — item 44.

AgentLoop drives iterative tool-calling with Ollama's tools API.
The model investigates and acts until it calls finish_repair, exhausts its
budget, or the wall-clock timeout fires.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

from config import (
    AGENT_MAX_EXTENSION_CALLS,
    AGENT_MAX_TOOL_CALLS,
    AGENT_MAX_TOTAL_CALLS,
    AGENT_MAX_WALL_SECONDS,
)
from utils.logging import get_logger
from utils.tool_registry import AgentLoopResult, AgentStep, ToolCall, ToolResult

_UNSET = object()  # sentinel for provider-aware model default

if TYPE_CHECKING:
    from interfaces import LLMClientProtocol
    from utils.tool_executor import ToolExecutor
    from utils.tool_registry import ToolRegistry

log = get_logger("agent_loop")


class LimitReviewDecision(BaseModel):
    reason_limit_hit: str = Field(description="Why the limit was reached")
    can_resolve_with_more: bool = Field(
        description="Would more budget/time lead to a solution?"
    )
    additional_calls_requested: int = Field(
        default=0, description="How many more tool calls needed (0 if giving up)"
    )
    summary_if_giving_up: str = Field(
        default="", description="What was learned, if cannot resolve"
    )


def _format_step_trace(steps: list["AgentStep"]) -> str:
    """One line per step, truncated to 80 chars each."""
    lines = []
    for s in steps:
        status = "OK" if s.tool_result.success else "ERR"
        detail = (s.tool_result.output or s.tool_result.error or "")[:60]
        lines.append(f"  {s.step_number}. {s.tool_call.name} → {status}: {detail}")
    return "\n".join(lines) if lines else "  (no tool calls)"


def _parse_content_as_tool_call(
    content: str, known_names: set[str]
) -> list[dict] | None:
    """Fallback for models that put tool calls in content instead of tool_calls.

    Some models (e.g. qwen2.5-coder:7b) respond with a JSON object like
    {"name": "read_config", "arguments": {}} in the content field rather than
    a proper tool_calls list.  If the content parses as a single tool call
    whose name is in the known registry, synthesise a tool_calls entry so the
    loop can dispatch it normally.  Returns None for ordinary text content.
    """
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    name = parsed.get("name", "")
    args = parsed.get("arguments")
    if name in known_names and isinstance(args, dict):
        return [{"function": {"name": name, "arguments": args}}]
    return None


_AGENT_LOOP_SYSTEM_PROMPT = """\
You are Pueo, an autonomous Home Assistant repair agent running in a tool-calling loop.

MANDATORY RULES — follow exactly:
1. Always respond by calling a tool. Never return plain text — that ends the session.
2. Always end the session by calling finish_repair. This is required every time.
3. After each tool result, immediately call the next tool. Keep going until you finish.
4. Only call apply_fix once per session and only after reading the relevant config or logs.
5. If no fix is needed, call finish_repair with action_taken='no_fix_needed'.

TYPICAL FLOW: read_config / read_logs → (apply_fix if broken) → finish_repair
"""

_CHAT_SYSTEM_PROMPT = """\
You are Pueo, a Home Assistant assistant. You have tools to check live HA
state, disk usage, logs, config, backups, and more. Use remember/recall to
store and retrieve context across sessions.

MANDATORY RULES — follow exactly:
1. Always respond by calling a tool — never return plain text.
2. Always end by calling finish_chat with a complete, helpful answer.
3. For questions about HA state (disk, backups, config, logs, updates, devices):
   use investigative tools FIRST, then finish_chat with a data-driven answer.
4. For general knowledge questions not requiring live HA data:
   call finish_chat directly with your answer.

DISK SPACE QUESTIONS:
  1. Call get_disk_usage to see the actual per-path breakdown.
  2. Optionally call run_ha_command("ha backups list") for individual backup details.
  3. Call finish_chat with specific, actionable advice based on the real numbers.

OTHER INVESTIGATION FLOWS:
  read_config / read_logs → finish_chat
  run_ha_command → finish_chat
  recall / remember → finish_chat
"""


class AgentLoop:
    """Iterative tool-calling loop with budget and wall-clock timeout.

    Parameters
    ----------
    llm_client:
        Any object implementing LLMClientProtocol (OllamaClient in production,
        FakeToolCallingLLMClient in tests).
    tool_executor:
        ToolExecutor instance pre-loaded with SSH clients / gate / notifier.
    tool_registry:
        ToolRegistry containing the tools to expose to the model.
    model:
        Ollama model name (defaults to OLLAMA_MODEL from config).
    system_prompt:
        Override the default system prompt.
    max_tool_calls:
        Hard cap on tool invocations per run (defaults to AGENT_MAX_TOOL_CALLS).
    max_wall_seconds:
        Wall-clock timeout in seconds (defaults to AGENT_MAX_WALL_SECONDS).
    """

    def __init__(
        self,
        llm_client: "LLMClientProtocol",
        tool_executor: "ToolExecutor",
        tool_registry: "ToolRegistry",
        model: str | object = _UNSET,
        system_prompt: str = _AGENT_LOOP_SYSTEM_PROMPT,
        max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
        max_wall_seconds: float = AGENT_MAX_WALL_SECONDS,
        terminal_tool_name: str = "finish_repair",
        step_callback: Optional[Callable[["AgentStep"], None]] = None,
        pre_step_callback: Optional[Callable[["ToolCall"], Awaitable[None]]] = None,
        timeline_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
        trigger: str = "manual",
        db_path: Optional[str] = None,
        escalated: bool = False,
    ) -> None:
        if model is _UNSET:
            from utils.llm_factory import _default_model_for_provider

            model = _default_model_for_provider()
        self._llm = llm_client
        self._executor = tool_executor
        self._registry = tool_registry
        self._model: str = model  # type: ignore[assignment]
        self._system_prompt = system_prompt
        self._max_tool_calls = max_tool_calls
        self._max_wall_seconds = max_wall_seconds
        self._terminal_tool_name = terminal_tool_name
        self._step_callback = step_callback
        self._pre_step_callback = pre_step_callback
        self._timeline_callback = timeline_callback
        self._trigger = trigger
        self._db_path = db_path
        self._escalated = escalated
        self._absolute_max = AGENT_MAX_TOTAL_CALLS

    def _build_episode(
        self,
        steps: list[AgentStep],
        episode_stub: Optional[dict],
        start_wall: float,
    ) -> dict[str, Any]:
        """Extract structured episode data from tool call history."""
        _DIAGNOSTIC_TOOLS = {"read_config", "read_logs", "read_file"}
        symptoms: list[str] = []
        fix_applied: Optional[str] = None
        verification_result = True

        for step in steps:
            name = step.tool_call.name
            if name in _DIAGNOSTIC_TOOLS and step.tool_result.success:
                out = (step.tool_result.output or "").strip()
                if out:
                    symptoms.append(out[:200])
            elif name == "apply_fix":
                fix_applied = step.tool_call.arguments.get("yaml_content")
            elif name == "verify_fix":
                verification_result = step.tool_result.success

        summary = (episode_stub or {}).get("summary", "")
        hypothesis_chain = [summary] if summary else []

        return {
            "symptoms": symptoms,
            "hypothesis_chain": hypothesis_chain,
            "fix_applied": fix_applied,
            "verification_result": verification_result,
            "duration_seconds": round(time.monotonic() - start_wall, 2),
        }

    def _record_episode(
        self,
        steps: list[AgentStep],
        episode_stub: Optional[dict],
        start_wall: float,
    ) -> str:
        """Serialize a RepairEpisode to SQLite and return its ID."""
        from utils.repair_episode import RepairEpisode, serialize_episode

        data = self._build_episode(steps, episode_stub, start_wall)
        episode = RepairEpisode(
            id=str(uuid.uuid4()),
            trigger=self._trigger,
            symptoms=data["symptoms"],
            tool_sequence=[step.tool_call for step in steps],
            tool_result_summaries=[
                (step.tool_result.output or step.tool_result.error or "")[:500]
                for step in steps
            ],
            hypothesis_chain=data["hypothesis_chain"],
            fix_applied=data["fix_applied"],
            verification_result=data["verification_result"],
            model_used=self._model,
            escalated=self._escalated,
            duration_seconds=data["duration_seconds"],
        )
        if self._db_path is None:
            return (
                episode.id
            )  # unreachable: _record_episode only called when db_path set
        serialize_episode(self._db_path, episode)
        log.info(
            "repair_episode_recorded",
            episode_id=episode.id,
            trigger=self._trigger,
            fix_applied=episode.fix_applied is not None,
        )
        return episode.id

    def _review_timeout(self, steps: list[AgentStep], elapsed: float) -> float:
        """Adaptive timeout for the limit-review call.

        Uses 3× the observed average per-step time, clamped to [30s, 300s].
        Falls back to 60s if no steps recorded yet.
        """
        if not steps:
            return 60.0
        avg = elapsed / len(steps)
        return max(30.0, min(3.0 * avg, 300.0))

    async def _review_limit(
        self,
        messages: list[dict],
        steps: list[AgentStep],
        reason: str,
        total_calls_used: int,
        elapsed: float,
    ) -> LimitReviewDecision:
        """Ask the LLM whether to extend the budget or give up gracefully."""
        step_trace = _format_step_trace(steps)
        review_prompt = (
            f"[LIMIT REVIEW] You have hit the {reason} limit "
            f"({total_calls_used} tool calls used, {elapsed:.0f}s elapsed). "
            f"Step trace:\n{step_trace}\n\n"
            "Assess: (1) Why did you hit this limit? "
            "(2) Would additional calls lead to a solution — if so, how many? "
            "(3) If you cannot make progress, summarize what you learned."
        )
        timeout = self._review_timeout(steps, elapsed)
        try:
            review_messages = messages + [{"role": "user", "content": review_prompt}]
            response = await asyncio.wait_for(
                self._llm.chat(
                    model=self._model,
                    messages=review_messages,
                    options={"temperature": 0.0},
                    format=LimitReviewDecision.model_json_schema(),
                ),
                timeout=timeout,
            )
            return LimitReviewDecision.model_validate_json(
                response["message"]["content"]
            )
        except Exception:
            log.warning("limit_review_failed", timeout_used=timeout)
            return LimitReviewDecision(
                reason_limit_hit="review failed",
                can_resolve_with_more=False,
                summary_if_giving_up="",
            )

    async def run(
        self,
        initial_context: str,
        initial_messages: Optional[list[dict]] = None,
    ) -> AgentLoopResult:
        """Run the agent loop and return a result describing what happened.

        The loop drives the model with the tool registry until one of:
        - finish_repair is called → outcome "success"
        - tool call budget exhausted → outcome "exhausted"
        - wall clock exceeded → outcome "timeout"
        - apply_fix returned failure → outcome "fix_failed"

        Parameters
        ----------
        initial_context:
            The current user message / task description.
        initial_messages:
            Optional prior conversation history as {"role", "content"} dicts.
            Inserted between the system prompt and the current user message so
            the model has proper multi-turn context.
        """
        self._executor.reset()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
        ]
        if initial_messages:
            messages.extend(initial_messages)
        messages.append({"role": "user", "content": initial_context})
        tools = self._registry.get_ollama_tools()
        steps: list[AgentStep] = []
        tool_call_count = 0
        start_time = time.monotonic()
        outcome: str = "exhausted"
        episode_stub: Optional[dict] = None

        try:
            result = await asyncio.wait_for(
                self._loop_body(messages, tools, steps, tool_call_count, start_time),
                timeout=self._max_wall_seconds,
            )
            outcome, episode_stub = result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            log.warning(
                "agent_loop_timeout",
                wall_seconds=self._max_wall_seconds,
                steps=len(steps),
            )
            decision = await self._review_limit(
                messages, steps, "timeout", len(steps), elapsed
            )
            if (
                decision.can_resolve_with_more
                and decision.additional_calls_requested > 0
                and len(steps) < self._absolute_max
            ):
                headroom = self._absolute_max - len(steps)
                grant = min(
                    decision.additional_calls_requested,
                    AGENT_MAX_EXTENSION_CALLS,
                    headroom,
                )
                if grant > 0:
                    self._max_tool_calls = len(steps) + grant
                    self._max_wall_seconds += 120.0
                    log.info(
                        "agent_loop_timeout_extension",
                        grant=grant,
                        total_cap=self._absolute_max,
                    )
                    try:
                        result = await asyncio.wait_for(
                            self._loop_body(
                                messages, tools, steps, len(steps), start_time
                            ),
                            timeout=self._max_wall_seconds,
                        )
                        outcome, episode_stub = result
                    except asyncio.TimeoutError:
                        outcome = "timeout"
                        if decision.summary_if_giving_up and episode_stub is None:
                            episode_stub = {"summary": decision.summary_if_giving_up}
                else:
                    outcome = "timeout"
                    if decision.summary_if_giving_up and episode_stub is None:
                        episode_stub = {"summary": decision.summary_if_giving_up}
            else:
                outcome = "timeout"
                if decision.summary_if_giving_up and episode_stub is None:
                    episode_stub = {"summary": decision.summary_if_giving_up}

        log.info(
            "agent_loop_complete",
            outcome=outcome,
            steps=len(steps),
            elapsed=round(time.monotonic() - start_time, 2),
        )

        episode_id: Optional[str] = None
        if outcome == "success" and self._db_path is not None:
            try:
                episode_id = self._record_episode(steps, episode_stub, start_time)
            except Exception as exc:  # nosec B110
                log.error("repair_episode_record_failed", error=str(exc))

        return AgentLoopResult(
            outcome=outcome,  # type: ignore[arg-type]
            steps=steps,
            episode_stub=episode_stub,
            episode_id=episode_id,
            capability_gap=bool((episode_stub or {}).get("capability_gap", False)),
            gap_description=(episode_stub or {}).get("gap_description", ""),
        )

    async def _maybe_extend_budget(
        self,
        messages: list[dict],
        steps: list[AgentStep],
        tool_call_count: int,
        start_time: float,
        reason: str,
        episode_stub: Optional[dict],
    ) -> tuple[bool, Optional[dict]]:
        """Ask the LLM whether to extend the budget. Returns (extended, episode_stub).

        If extended=True the caller should continue the loop (self._max_tool_calls
        has been raised). If False the caller should return "exhausted".
        """
        elapsed = time.monotonic() - start_time
        decision = await self._review_limit(
            messages, steps, reason, tool_call_count, elapsed
        )
        if decision.summary_if_giving_up and episode_stub is None:
            episode_stub = {"summary": decision.summary_if_giving_up}
        if decision.can_resolve_with_more and decision.additional_calls_requested > 0:
            headroom = self._absolute_max - tool_call_count
            grant = min(
                decision.additional_calls_requested,
                AGENT_MAX_EXTENSION_CALLS,
                headroom,
            )
            if grant > 0:
                self._max_tool_calls = tool_call_count + grant
                log.info(
                    "agent_loop_budget_extended",
                    reason=reason,
                    grant=grant,
                    total_cap=self._absolute_max,
                )
                return True, episode_stub
        return False, episode_stub

    async def _loop_body(
        self,
        messages: list[dict],
        tools: list[dict],
        steps: list[AgentStep],
        tool_call_count: int,
        start_time: float,
    ) -> tuple[str, Optional[dict]]:
        episode_stub: Optional[dict] = None
        no_tool_streak = 0  # consecutive plain-text responses with no tool calls
        last_plain_text: str = ""  # most recent plain-text content (fallback summary)

        while True:
            # Budget check at top of loop so extensions can continue without restart.
            if tool_call_count >= self._max_tool_calls:
                log.warning("agent_loop_budget_exhausted", count=tool_call_count)
                extended, episode_stub = await self._maybe_extend_budget(
                    messages, steps, tool_call_count, start_time, "budget", episode_stub
                )
                if extended:
                    continue
                return "exhausted", episode_stub

            response = await self._llm.chat_with_tools(
                model=self._model,
                messages=messages,
                tools=tools,
            )

            tool_calls_raw = response.get("tool_calls")
            if not tool_calls_raw:
                content = response.get("content", "")
                known = {t["function"]["name"] for t in tools}
                fallback = _parse_content_as_tool_call(content, known)
                if fallback:
                    # Model embedded the tool call in content instead of
                    # tool_calls (a known qwen2.5-coder quirk).  Patch the
                    # response so the rest of the loop works normally.
                    log.info(
                        "agent_loop_content_tool_call_fallback",
                        content=content[:120],
                    )
                    response["tool_calls"] = fallback
                    response["content"] = ""  # prevent raw JSON from confusing history
                    tool_calls_raw = fallback
                else:
                    no_tool_streak += 1
                    if content:
                        last_plain_text = content
                    if no_tool_streak >= 2:
                        log.info(
                            "agent_loop_no_tool_calls",
                            content=content[:120],
                        )
                        if last_plain_text and episode_stub is None:
                            episode_stub = {"summary": last_plain_text}
                        extended, episode_stub = await self._maybe_extend_budget(
                            messages,
                            steps,
                            tool_call_count,
                            start_time,
                            "no_tool_streak",
                            episode_stub,
                        )
                        if extended:
                            no_tool_streak = 0
                            continue
                        return "exhausted", episode_stub
                    # Single plain-text response — inject a recovery nudge and
                    # retry rather than giving up immediately.
                    log.info(
                        "agent_loop_no_tool_calls_nudge",
                        content=content[:120],
                    )
                    messages.append({"role": "assistant", "content": content or ""})
                    # When no tools have been called yet, nudging straight to
                    # the terminal tool causes the model to skip investigation
                    # and hallucinate answers.  Instead, demand any tool call
                    # and let the system prompt guide which one to use first.
                    if tool_call_count == 0:
                        nudge = (
                            "You must call a tool — do not return plain text. "
                            "For questions requiring live HA data (disk, config, "
                            "logs, backups), call the appropriate investigative tool "
                            f"first (e.g. get_disk_usage, read_config, read_logs). "
                            f"For general knowledge questions, call "
                            f"{self._terminal_tool_name} with your answer in the "
                            f'"summary" field.'
                        )
                    else:
                        nudge = (
                            f"Call {self._terminal_tool_name} NOW with your answer "
                            f'in the "summary" field. Do not return plain text.'
                        )
                    messages.append({"role": "user", "content": nudge})
                    continue

            no_tool_streak = 0  # reset: we received tool calls this iteration

            # Append the assistant message (with tool_calls) to history.
            messages.append(response)

            for tc_raw in tool_calls_raw:
                if tool_call_count >= self._max_tool_calls:
                    log.warning(
                        "agent_loop_budget_exhausted_mid_batch", count=tool_call_count
                    )
                    extended, episode_stub = await self._maybe_extend_budget(
                        messages,
                        steps,
                        tool_call_count,
                        start_time,
                        "budget",
                        episode_stub,
                    )
                    if not extended:
                        return "exhausted", episode_stub
                    # Extended — fall through and execute this tool call.

                fn = tc_raw.get("function", {})
                tool_call = ToolCall(
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments", {}),
                )
                ts = time.monotonic() - start_time
                if self._pre_step_callback is not None:
                    await self._pre_step_callback(tool_call)
                tool_result: ToolResult = await self._executor.execute(tool_call)
                tool_call_count += 1

                step = AgentStep(
                    step_number=tool_call_count,
                    tool_call=tool_call,
                    tool_result=tool_result,
                    timestamp=ts,
                )
                steps.append(step)
                if self._step_callback is not None:
                    self._step_callback(step)
                if self._timeline_callback is not None:
                    status = "OK" if tool_result.success else "ERR"
                    status_line = f"step {tool_call_count} — {tool_call.name}: {status}"
                    await self._timeline_callback(tool_call.name, status_line)

                # Feed tool result back to the conversation.
                result_text = (
                    tool_result.output
                    if tool_result.success
                    else f"Error: {tool_result.error}"
                )
                messages.append(
                    {
                        "role": "tool",
                        "content": result_text,
                        "name": tool_call.name,
                    }
                )

                log.info(
                    "agent_loop_step",
                    step=tool_call_count,
                    tool=tool_call.name,
                    success=tool_result.success,
                )

                if tool_call.name == self._terminal_tool_name:
                    episode_stub = {
                        "summary": tool_call.arguments.get("summary", ""),
                        "action_taken": tool_call.arguments.get("action_taken", ""),
                        "steps": tool_call_count,
                        "capability_gap": bool(
                            tool_call.arguments.get("capability_gap", False)
                        ),
                        "gap_description": tool_call.arguments.get(
                            "gap_description", ""
                        ),
                    }
                    return "success", episode_stub

                if tool_result.awaiting_approval:
                    return "awaiting_approval", episode_stub

                if tool_call.name == "apply_fix" and not tool_result.success:
                    return "fix_failed", episode_stub

            # All tool calls in this response processed; prompt the model to continue.
            messages.append(
                {"role": "user", "content": "Continue. Call the next tool."}
            )
