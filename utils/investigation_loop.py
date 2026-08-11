"""General-purpose investigation loop for Pueo.

Encodes the pattern: gather evidence → consult knowledge → identify root causes →
rank remediation options by safety/reversibility/impact → classify as auto/HITL/manual → report.

Usage:
    report = await run_investigation(
        topic="HA disk space critically low",
        goal="Identify what is consuming the most disk space and recommend recovery steps",
        context="Disk is at 82% usage, 1.9 GB free, threshold is 3.0 GB",
        llm_client=make_llm_client(),
        knowledge_store=knowledge_store,
    )
    # report.auto_actions are safe to execute immediately
    # report.hitl_actions go to a HITL card for user approval

The disk recovery implementation in utils/disk_recovery.py is a hardcoded instantiation of
this pattern for the disk-critical domain. run_investigation() makes the pattern available for
any future domain without bespoke code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from typing import Any

from interfaces import (
    KnowledgeStoreClientProtocol,
    LLMClientProtocol,
    SSHClientProtocol,
)
from utils.logging import get_logger
from utils.tool_registry import (
    GET_DISK_USAGE,
    QUERY_KNOWLEDGE,
    READ_FILE,
    READ_LOGS,
    RECALL,
    RUN_HA_COMMAND,
    ToolDefinition,
    ToolRegistry,
)

log = get_logger("investigation_loop")

# ---------------------------------------------------------------------------
# Structured output types
# ---------------------------------------------------------------------------


@dataclass
class InvestigationAction:
    """A single remediation step ranked by the investigation agent."""

    name: str
    description: str
    estimated_impact: str  # e.g. "frees ~2 GB"
    reversible: bool
    risk_level: str  # LOW | MEDIUM | HIGH | CRITICAL
    action_key: str  # opaque key for dashboard dispatch


@dataclass
class InvestigationReport:
    """Structured output from a completed investigation loop."""

    topic: str
    summary: str  # plain-English diagnosis
    root_causes: list[str] = field(default_factory=list)
    auto_actions: list[InvestigationAction] = field(default_factory=list)
    hitl_actions: list[InvestigationAction] = field(default_factory=list)
    manual_only: list[str] = field(default_factory=list)
    knowledge_sources: list[str] = field(default_factory=list)
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# finish_investigation terminal tool
# ---------------------------------------------------------------------------

FINISH_INVESTIGATION = ToolDefinition(
    name="finish_investigation",
    description=(
        "Signal that the investigation is complete. "
        "Call with a structured summary of root causes and ranked remediation options."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Plain-English diagnosis of the root cause(s)",
            },
            "root_causes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of identified root causes (not symptoms)",
            },
            "auto_actions": {
                "type": "array",
                "description": "Safe steps that can be executed without human approval",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "estimated_impact": {"type": "string"},
                        "reversible": {"type": "boolean"},
                        "risk_level": {
                            "type": "string",
                            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                        },
                        "action_key": {"type": "string"},
                    },
                    "required": [
                        "name",
                        "description",
                        "estimated_impact",
                        "reversible",
                        "risk_level",
                        "action_key",
                    ],
                },
            },
            "hitl_actions": {
                "type": "array",
                "description": "Steps requiring human approval before execution",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "estimated_impact": {"type": "string"},
                        "reversible": {"type": "boolean"},
                        "risk_level": {
                            "type": "string",
                            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                        },
                        "action_key": {"type": "string"},
                    },
                    "required": [
                        "name",
                        "description",
                        "estimated_impact",
                        "reversible",
                        "risk_level",
                        "action_key",
                    ],
                },
            },
            "manual_only": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Items requiring human judgment that cannot be automated",
            },
            "knowledge_sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "RAG chunks or KB references consulted during investigation",
            },
            "confidence": {
                "type": "number",
                "description": "0.0–1.0 confidence in the diagnosis and recommendations",
            },
        },
        "required": ["summary", "root_causes", "auto_actions", "hitl_actions"],
    },
)


# ---------------------------------------------------------------------------
# Investigation system prompt template
# ---------------------------------------------------------------------------

INVESTIGATION_SYSTEM_PROMPT_TEMPLATE = """\
You are Pueo's domain investigator. Your task is to investigate the following situation:

TOPIC: {topic}
GOAL: {investigation_goal}
CONTEXT: {context}

## Investigative Process

Follow these steps in order:

1. **Gather evidence** — Use the available read tools to collect current state.
   - Call get_disk_usage, read_logs, run_ha_command, or read_file as appropriate.
   - Do not skip evidence gathering to go straight to recommendations.

2. **Consult knowledge** — Call query_knowledge with relevant search terms.
   - Use terms like the specific problem area, component names, and "best practices".
   - If query_knowledge returns no results, note this in knowledge_sources and lower your confidence.
   - Never guess at best practices — if knowledge is unavailable, say so.

3. **Identify root causes** — Reason from the evidence to underlying causes, not symptoms.
   - A symptom: "disk is full". A root cause: "recorder DB has grown to 8 GB due to high-frequency entity polling".
   - List each root cause separately in root_causes.

4. **Rank remediation options** — For each option, assess:
   - **Impact**: how much space/performance/reliability it recovers
   - **Reversibility**: can the action be undone? (deleting history cannot; compacting a DB can be redone)
   - **Risk level**: LOW (read-only or easily reversible), MEDIUM (modifies data, reversible by backup),
     HIGH (irreversible data loss, service interruption), CRITICAL (production outage risk)
   - **Autonomy classification**:
     - auto_actions: LOW risk, reversible-or-acceptable-loss, no service interruption
     - hitl_actions: MEDIUM/HIGH risk, or any action with meaningful data loss or downtime
     - manual_only: requires physical access, hypervisor changes, or human judgment on content

5. **Report** — Call finish_investigation with your structured findings.

## Rules

- Always call at least one evidence-gathering tool before forming conclusions.
- Always call query_knowledge at least once.
- Never call apply_fix, trigger_backup, propose_patch, sandbox_code, or add_tool.
- You MUST terminate by calling finish_investigation — never return plain text.
- If evidence is ambiguous, reflect that in confidence (< 0.7) and explain in summary.
"""


# ---------------------------------------------------------------------------
# Tool registry factory
# ---------------------------------------------------------------------------


def build_investigation_tool_registry() -> ToolRegistry:
    """Read-only tool registry for investigation loops.

    Includes evidence-gathering and knowledge tools; excludes all write operations.
    """
    reg = ToolRegistry()
    for tool in (
        GET_DISK_USAGE,
        RUN_HA_COMMAND,
        READ_LOGS,
        READ_FILE,
        QUERY_KNOWLEDGE,
        RECALL,
        FINISH_INVESTIGATION,
    ):
        reg.register(tool)
    return reg


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


async def run_investigation(
    topic: str,
    investigation_goal: str,
    context: str,
    llm_client: LLMClientProtocol,
    ha_ssh_client: SSHClientProtocol,
    gate: Any,  # AutonomyGate — Any avoids circular import
    notifier: Any,  # NotifierProtocol — Any avoids circular import
    knowledge_store: Optional[KnowledgeStoreClientProtocol] = None,
    max_tool_calls: int = 15,
    max_wall_seconds: float = 90.0,
) -> InvestigationReport:
    """Run a read-only investigation agent loop and return a structured report.

    The agent gathers evidence, consults the knowledge store, identifies root causes,
    and ranks remediation options. It terminates by calling finish_investigation.
    """
    from utils.agent_loop import AgentLoop
    from utils.tool_executor import ToolExecutor

    system_prompt = INVESTIGATION_SYSTEM_PROMPT_TEMPLATE.format(
        topic=topic,
        investigation_goal=investigation_goal,
        context=context,
    )

    registry = build_investigation_tool_registry()
    executor = ToolExecutor(
        ha_ssh_client=ha_ssh_client,
        gate=gate,
        notifier=notifier,
        knowledge_store=knowledge_store,
    )

    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=registry,
        tool_executor=executor,
        max_tool_calls=max_tool_calls,
        max_wall_seconds=max_wall_seconds,
        system_prompt=system_prompt,
        terminal_tool_name="finish_investigation",
        trigger="investigation",
    )

    result = await loop.run(initial_context=topic)

    # Parse finish_investigation arguments from the last finish_investigation step
    finish_args: dict = {}
    for step in result.steps:
        if step.tool_call.name == "finish_investigation":
            finish_args = step.tool_call.arguments
            break

    def _parse_actions(raw: list[dict]) -> list[InvestigationAction]:
        actions = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            actions.append(
                InvestigationAction(
                    name=item.get("name", ""),
                    description=item.get("description", ""),
                    estimated_impact=item.get("estimated_impact", "unknown"),
                    reversible=bool(item.get("reversible", False)),
                    risk_level=item.get("risk_level", "MEDIUM"),
                    action_key=item.get("action_key", ""),
                )
            )
        return actions

    report = InvestigationReport(
        topic=topic,
        summary=finish_args.get("summary", result.outcome),
        root_causes=finish_args.get("root_causes", []),
        auto_actions=_parse_actions(finish_args.get("auto_actions", [])),
        hitl_actions=_parse_actions(finish_args.get("hitl_actions", [])),
        manual_only=finish_args.get("manual_only", []),
        knowledge_sources=finish_args.get("knowledge_sources", []),
        confidence=float(finish_args.get("confidence", 0.0)),
    )

    log.info(
        "investigation_complete",
        topic=topic,
        outcome=result.outcome,
        root_causes=len(report.root_causes),
        auto_actions=len(report.auto_actions),
        hitl_actions=len(report.hitl_actions),
        confidence=report.confidence,
    )
    return report
