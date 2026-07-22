"""Unified autonomy gate — single ask/skip decision point for all Pueo actions.

Levels:
  1 REPORT_ONLY  — observe and report; never execute or notify
  2 SUGGEST      — propose every action; require explicit HITL approval for all
  3 GUIDED       — auto-execute LOW-risk; pause for MEDIUM / HIGH / CRITICAL
  4 AUTONOMOUS   — auto-execute LOW / MEDIUM / HIGH; pause only for CRITICAL

Risk taxonomy:
  LOW      — read-only calls, name locks
  MEDIUM   — non-production config writes (e.g., NetAlertX app.conf, sandbox path)
  HIGH     — production HA config write, add-on restart, ha core restart
  CRITICAL — removing top-level config block, bulk irreversible ops, no backup slug
"""

import uuid
from enum import IntEnum
from typing import TYPE_CHECKING

from utils.logging import get_logger

if TYPE_CHECKING:
    from utils.notify import NotifierProtocol

log = get_logger("autonomy")


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class AutonomyLevel(IntEnum):
    REPORT_ONLY = 1
    SUGGEST = 2
    GUIDED = 3
    AUTONOMOUS = 4


class AutonomyGate:
    """Decision point imported by all Pueo modules to ask or skip an action."""

    def __init__(self, level: int = 2) -> None:
        self._level = AutonomyLevel(level)

    def should_auto_execute(self, risk: RiskLevel) -> bool:
        """True if the current level permits executing at ``risk`` without asking."""
        if self._level == AutonomyLevel.REPORT_ONLY:
            return False
        if self._level == AutonomyLevel.SUGGEST:
            return False
        if self._level == AutonomyLevel.GUIDED:
            return risk == RiskLevel.LOW
        # AUTONOMOUS: auto for LOW / MEDIUM / HIGH
        return risk != RiskLevel.CRITICAL

    def should_ask_preference(self, context: str) -> bool:
        """True if a preference question is appropriate at the current level."""
        return self._level != AutonomyLevel.AUTONOMOUS

    async def require_approval(
        self,
        subject: str,
        body: str,
        payload: dict,
        notifier: "NotifierProtocol",
        risk: RiskLevel,
    ) -> bool:
        """Request human approval if required by the current level and risk.

        Returns True (proceed) or False (skip/rejected).  At level 1 returns
        False without notifying.  At level 4 short-circuits to True for risks
        below CRITICAL without notifying.  All other cases send a notification
        and poll for approval up to ``timeout_minutes``.
        """
        if self._level == AutonomyLevel.REPORT_ONLY:
            return False
        if self._level == AutonomyLevel.AUTONOMOUS and risk != RiskLevel.CRITICAL:
            return True
        if self._level == AutonomyLevel.GUIDED and risk == RiskLevel.LOW:
            return True
        # Send HITL notification and poll indefinitely for response
        nid = payload.get("notification_id", str(uuid.uuid4()))
        log.info("hitl_waiting_for_approval", subject=subject, risk=risk.name)
        await notifier.send(subject, body, payload)
        approved = await notifier.wait_for_approval(nid)
        log.info("hitl_approval_received", approved=approved, subject=subject)
        return approved


class FakeAutonomyGate:
    """Test double: configurable auto-execute and approval behaviour.

    ``auto_execute_result=True`` mimics a gate that always proceeds without
    notifying.  ``auto_execute_result=False`` mimics a gate that always asks,
    sends a notification via the notifier, and returns ``approval_result``.
    Call counts are exposed for assertions.
    """

    def __init__(
        self,
        auto_execute_result: bool = True,
        approval_result: bool = True,
    ) -> None:
        self._auto_execute = auto_execute_result
        self._approval = approval_result
        self.should_auto_execute_calls: list[RiskLevel] = []
        self.require_approval_calls: list[dict] = []

    def should_auto_execute(self, risk: RiskLevel) -> bool:
        self.should_auto_execute_calls.append(risk)
        return self._auto_execute

    def should_ask_preference(self, context: str) -> bool:
        return not self._auto_execute

    async def require_approval(
        self,
        subject: str,
        body: str,
        payload: dict,
        notifier: "NotifierProtocol",
        risk: RiskLevel,
    ) -> bool:
        self.require_approval_calls.append(
            {"subject": subject, "risk": risk, "body": body}
        )
        if self._auto_execute:
            return True
        nid = payload.get("notification_id", str(uuid.uuid4()))
        await notifier.send(subject, body, payload)
        return await notifier.wait_for_approval(nid)
