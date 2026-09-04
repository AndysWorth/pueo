"""Tool result guardrail — detect failure modes after executor.execute() and communicate
them transparently to the LLM by prefixing the tool output with a [SYSTEM NOTICE] block.

The LLM receives the notice and decides how to continue; nothing is silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from utils.core.context import estimate_tokens, truncate_to_budget

# Tools expected to return non-empty content; empty output is notable.
_CONTENT_TOOLS = {
    "read_config",
    "read_logs",
    "read_file",
    "read_source",
    "fetch_ha_docs",
    "run_ha_command",
    "query_knowledge",
    "read_pueo_log",
    "search_log",
    "fetch_url",
}

# Action tools — seeing these breaks a circular investigation.
_ACTION_TOOLS = {
    "apply_fix",
    "save_runbook",
    "save_runbook_candidate",
    "add_memory",
    "request_escalation",
}

# Diagnostic tools that trigger circular-investigation detection.
_DIAGNOSTIC_TOOLS = {"read_config", "read_logs"}

# Error patterns that indicate something went wrong in the remote system.
_ERROR_PATTERNS = re.compile(
    r"(?i)(Traceback \(most recent call last\)|"
    r"ConnectionRefusedError|Connection refused|"
    r"command not found|No such file or directory|"
    r"HTTP [5]\d{2}|Internal Server Error|Bad Gateway|Service Unavailable|"
    r"Permission denied|Access denied|"
    r"Exception:|Error:|RuntimeError|ValueError|KeyError)",
)


@dataclass
class GuardrailCheck:
    triggered: bool
    rule: str = ""
    observation: str = ""
    severity: str = "notice"  # "notice" | "warning" | "critical"


@dataclass
class _GuardrailState:
    """Mutable per-session state threaded through _loop_body."""

    # (tool_name, args_key) of the immediately previous tool call.
    prev_tool: Optional[tuple[str, str]] = None
    # Running count of consecutive success=False results.
    consecutive_failures: int = 0
    # Count of diagnostic reads since the last action tool.
    diagnostic_reads_since_action: int = 0
    # Files that have been read then had a fix applied to them.
    patched_files: set[str] = field(default_factory=set)
    # All tool names available in the registry (populated by AgentLoop).
    known_tool_names: set[str] = field(default_factory=set)


class ToolResultGuardrail:
    """Stateful guardrail that inspects each tool result and returns a message.

    Usage::

        guardrail = ToolResultGuardrail(max_result_tokens=4000, max_tool_calls=30)
        result_text = guardrail.process(
            tool_name=tool_call.name,
            tool_args=tool_call.arguments,
            raw_output=result_text,
            success=tool_result.success,
            remaining_budget=remaining,
        )
        # result_text is either unchanged or prefixed with [SYSTEM NOTICE ...].
    """

    def __init__(
        self,
        max_result_tokens: int = 4000,
        max_tool_calls: int = 30,
        known_tool_names: Optional[set[str]] = None,
    ) -> None:
        self._max_result_tokens = max_result_tokens
        self._max_tool_calls = max_tool_calls
        self._state = _GuardrailState(known_tool_names=known_tool_names or set())

    def process(
        self,
        tool_name: str,
        tool_args: dict,
        raw_output: str,
        success: bool,
        remaining_budget: int,
    ) -> str:
        """Inspect one tool result and return the (possibly annotated) text."""
        checks: list[GuardrailCheck] = [
            self._check_hallucinated_tool(tool_name),
            self._check_output_too_large(raw_output),
            self._check_error_pattern(raw_output, success),
            self._check_repetition(tool_name, tool_args),
            self._check_cascade_failure(success),
            self._check_empty_output(tool_name, raw_output, success),
            self._check_budget_pressure(remaining_budget),
            self._check_circular_investigation(tool_name),
            self._check_stale_re_read(tool_name, tool_args, success),
        ]

        # Update state after checks that depend on previous state.
        self._update_state(tool_name, tool_args, success)

        triggered = [c for c in checks if c.triggered]
        if not triggered:
            return raw_output

        # Build a combined notice.  Pick the highest-severity one for the header.
        severity_order = {"critical": 0, "warning": 1, "notice": 2}
        primary = min(triggered, key=lambda c: severity_order.get(c.severity, 99))
        all_observations = "\n".join(f"• {c.observation}" for c in triggered)

        # Truncate raw_output if output_too_large was triggered.
        display_output = raw_output
        for c in triggered:
            if c.rule == "output_too_large":
                display_output = truncate_to_budget(
                    raw_output, self._max_result_tokens, "smart"
                )
                break

        notice = (
            f"[SYSTEM NOTICE — {primary.rule}]\n"
            f"{all_observations}\n"
            f"How would you like to continue?\n"
            f"— tool output follows —\n"
            f"{display_output}"
        )
        return notice

    # ------------------------------------------------------------------
    # Individual rule checks
    # ------------------------------------------------------------------

    def _check_hallucinated_tool(self, tool_name: str) -> GuardrailCheck:
        if (
            self._state.known_tool_names
            and tool_name not in self._state.known_tool_names
        ):
            return GuardrailCheck(
                triggered=True,
                rule="hallucinated_tool",
                observation=(
                    f"The tool '{tool_name}' is not in the registered tool list. "
                    "This call was executed but may not have worked as expected. "
                    "Check available tools via read_source('utils/agent/tool_registry.py')."
                ),
                severity="warning",
            )
        return GuardrailCheck(triggered=False)

    def _check_output_too_large(self, output: str) -> GuardrailCheck:
        tokens = estimate_tokens(output)
        if tokens > self._max_result_tokens:
            return GuardrailCheck(
                triggered=True,
                rule="output_too_large",
                observation=(
                    f"The tool returned {len(output):,} characters (~{tokens:,} tokens), "
                    f"exceeding the {self._max_result_tokens:,}-token threshold. "
                    "Output truncated using 'smart' strategy (first 25% + last 75%). "
                    "You may be missing content from the middle."
                ),
                severity="notice",
            )
        return GuardrailCheck(triggered=False)

    def _check_error_pattern(self, output: str, success: bool) -> GuardrailCheck:
        if not success:
            return GuardrailCheck(triggered=False)
        if _ERROR_PATTERNS.search(output):
            return GuardrailCheck(
                triggered=True,
                rule="error_pattern",
                observation=(
                    "The tool reported success but the output contains error indicators "
                    "(exception trace, HTTP 5xx, permission denied, or similar). "
                    "The system may be in a partially failed state."
                ),
                severity="warning",
            )
        return GuardrailCheck(triggered=False)

    def _check_repetition(self, tool_name: str, tool_args: dict) -> GuardrailCheck:
        args_key = str(sorted(tool_args.items()))
        current = (tool_name, args_key)
        if self._state.prev_tool == current:
            return GuardrailCheck(
                triggered=True,
                rule="repetition",
                observation=(
                    f"This is an exact repeat of the immediately previous call to '{tool_name}' "
                    "with identical arguments. The result will be the same. "
                    "Consider a different tool or modified arguments."
                ),
                severity="warning",
            )
        return GuardrailCheck(triggered=False)

    def _check_cascade_failure(self, success: bool) -> GuardrailCheck:
        # Use the *updated* count (including this call).
        count = self._state.consecutive_failures + (0 if success else 1)
        if count >= 3:
            return GuardrailCheck(
                triggered=True,
                rule="cascade_failure",
                observation=(
                    f"{count} consecutive tool calls have failed. "
                    "This may indicate a systemic issue (SSH unreachable, HA down, "
                    "permission error). Consider calling finish_repair with outcome=failed "
                    "or request_escalation if you cannot resolve the underlying problem."
                ),
                severity="critical",
            )
        return GuardrailCheck(triggered=False)

    def _check_empty_output(
        self, tool_name: str, output: str, success: bool
    ) -> GuardrailCheck:
        if success and tool_name in _CONTENT_TOOLS and not output.strip():
            return GuardrailCheck(
                triggered=True,
                rule="empty_output",
                observation=(
                    f"'{tool_name}' succeeded but returned empty output. "
                    "The resource may be empty, the path may be wrong, or the query "
                    "returned no results."
                ),
                severity="notice",
            )
        return GuardrailCheck(triggered=False)

    def _check_budget_pressure(self, remaining: int) -> GuardrailCheck:
        if 0 < remaining <= 5:
            return GuardrailCheck(
                triggered=True,
                rule="budget_pressure",
                observation=(
                    f"Only {remaining} tool call(s) remaining in this session's budget. "
                    "Prioritise finishing the investigation or call request_escalation "
                    "if you need more budget."
                ),
                severity="warning",
            )
        return GuardrailCheck(triggered=False)

    def _check_circular_investigation(self, tool_name: str) -> GuardrailCheck:
        # Count is updated *before* this check in update_state, so we use the
        # pre-update value by checking the threshold against current+1.
        reads_after = self._state.diagnostic_reads_since_action + (
            1 if tool_name in _DIAGNOSTIC_TOOLS else 0
        )
        if reads_after >= 4:
            return GuardrailCheck(
                triggered=True,
                rule="circular_investigation",
                observation=(
                    f"'{tool_name}' has been called {reads_after} time(s) without an "
                    "action tool (apply_fix, save_runbook, request_escalation) in between. "
                    "You may be in an investigative loop. State your current hypothesis "
                    "explicitly and decide whether to act or escalate."
                ),
                severity="warning",
            )
        return GuardrailCheck(triggered=False)

    def _check_stale_re_read(
        self, tool_name: str, tool_args: dict, success: bool
    ) -> GuardrailCheck:
        if tool_name not in {"read_file", "read_source", "read_config"}:
            return GuardrailCheck(triggered=False)
        file_arg = (
            tool_args.get("path")
            or tool_args.get("filename")
            or tool_args.get("file_path")
        )
        if file_arg and file_arg in self._state.patched_files:
            return GuardrailCheck(
                triggered=True,
                rule="stale_re_read",
                observation=(
                    f"'{file_arg}' was already read, a fix was applied to it, and you are "
                    "reading it again. Verify the fix was applied correctly — if so, proceed "
                    "to the next investigation step or call finish_repair."
                ),
                severity="notice",
            )
        return GuardrailCheck(triggered=False)

    # ------------------------------------------------------------------
    # State mutation
    # ------------------------------------------------------------------

    def _update_state(self, tool_name: str, tool_args: dict, success: bool) -> None:
        args_key = str(sorted(tool_args.items()))
        self._state.prev_tool = (tool_name, args_key)

        if success:
            self._state.consecutive_failures = 0
        else:
            self._state.consecutive_failures += 1

        if tool_name in _DIAGNOSTIC_TOOLS:
            self._state.diagnostic_reads_since_action += 1
        elif tool_name in _ACTION_TOOLS:
            self._state.diagnostic_reads_since_action = 0

        # Track files that have had a fix applied so stale_re_read can fire.
        if tool_name == "apply_fix" and success:
            file_arg = tool_args.get("path") or tool_args.get("file_path")
            if file_arg:
                self._state.patched_files.add(file_arg)
