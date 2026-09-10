"""PueoWorkQueue — serialized execution of all LLM/HA operations.

A single consumer processes one item at a time. Items are ordered by
(priority, submitted_at). Dedup prevents duplicate work for the same
key. Suppression holds an item until all activity types it depends on
have finished.
"""

import asyncio
import bisect
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from utils.core.logging import get_logger

log = get_logger("work_queue")

# Priority constants (lower = higher priority)
PRIORITY_CRITICAL = 10  # repair triggered by live log event
PRIORITY_HIGH = 20  # triage, disk recovery, notification analysis
PRIORITY_NORMAL = 30  # update analysis, lovelace investigation, config analysis, chat
PRIORITY_LOW = 40  # background / routine


@dataclass
class WorkItem:
    """A unit of work submitted to PueoWorkQueue."""

    priority: int
    activity_type: str
    description: str
    dedup_key: str  # "" = no dedup
    suppress_while_running: frozenset  # frozenset[str] of activity_type values
    coro_factory: Callable[[], Coroutine[Any, Any, Any]]
    submitted_at: float = field(default_factory=time.time)

    def __lt__(self, other: "WorkItem") -> bool:
        return (self.priority, self.submitted_at) < (other.priority, other.submitted_at)

    def __le__(self, other: "WorkItem") -> bool:
        return (self.priority, self.submitted_at) <= (
            other.priority,
            other.submitted_at,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkItem):
            return NotImplemented
        return (self.priority, self.submitted_at) == (
            other.priority,
            other.submitted_at,
        )

    def __gt__(self, other: "WorkItem") -> bool:
        return (self.priority, self.submitted_at) > (other.priority, other.submitted_at)

    def __ge__(self, other: "WorkItem") -> bool:
        return (self.priority, self.submitted_at) >= (
            other.priority,
            other.submitted_at,
        )


_PRIORITY_LABELS = {
    PRIORITY_CRITICAL: "critical",
    PRIORITY_HIGH: "high",
    PRIORITY_NORMAL: "normal",
    PRIORITY_LOW: "low",
}


class PueoWorkQueue:
    """Serialized work queue for all LLM/HA operations.

    One item runs at a time globally. Items are ordered by (priority, submitted_at).
    Dedup prevents two items with the same non-empty dedup_key from both being
    pending/running. Suppression holds an item until its declared
    suppress_while_running activity types have all finished.
    """

    def __init__(self) -> None:
        self._pending: list[WorkItem] = []  # bisect-sorted
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._running: WorkItem | None = None
        self._active_types: set[str] = set()
        self._consumer_task: asyncio.Task | None = None  # type: ignore[type-arg]

    def start(self) -> None:
        """Start the background consumer loop. Call once after the event loop is running."""
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.create_task(self._consume())

    async def submit(self, item: WorkItem) -> bool:
        """Submit a work item.

        Returns True if accepted, False if dropped (dedup match found in running
        or pending items with the same non-empty dedup_key).
        """
        async with self._lock:
            if item.dedup_key:
                # Check running item
                if (
                    self._running is not None
                    and self._running.dedup_key == item.dedup_key
                ):
                    log.debug(
                        "work_queue_dedup_running",
                        key=item.dedup_key,
                        activity=item.activity_type,
                    )
                    return False
                # Check pending items
                for pending in self._pending:
                    if pending.dedup_key == item.dedup_key:
                        log.debug(
                            "work_queue_dedup_pending",
                            key=item.dedup_key,
                            activity=item.activity_type,
                        )
                        return False

            bisect.insort(self._pending, item)
            log.info(
                "work_queue_submitted",
                activity=item.activity_type,
                priority=_PRIORITY_LABELS.get(item.priority, str(item.priority)),
                pending=len(self._pending),
                description=item.description,
            )
            self._event.set()
            self._publish_queue_update()
            return True

    async def _consume(self) -> None:
        """Single consumer loop — runs one item at a time."""
        while True:
            await self._event.wait()
            async with self._lock:
                # Find first pending item not suppressed by currently active types
                chosen_idx: int | None = None
                for i, candidate in enumerate(self._pending):
                    if not (candidate.suppress_while_running & self._active_types):
                        chosen_idx = i
                        break

                if chosen_idx is None:
                    # All pending items are suppressed — wait for an active type to clear
                    self._event.clear()
                    continue

                item = self._pending.pop(chosen_idx)
                self._running = item
                self._active_types.add(item.activity_type)
                if not self._pending:
                    self._event.clear()
                self._publish_queue_update()

            log.info(
                "work_queue_running",
                activity=item.activity_type,
                description=item.description,
            )
            try:
                await item.coro_factory()
            except Exception as exc:
                log.error(
                    "work_queue_item_failed",
                    activity=item.activity_type,
                    error=str(exc),
                )
            finally:
                async with self._lock:
                    self._running = None
                    self._active_types.discard(item.activity_type)
                    # Wake up the consumer in case suppressed items can now run
                    if self._pending:
                        self._event.set()
                    self._publish_queue_update()

                log.info(
                    "work_queue_done",
                    activity=item.activity_type,
                    description=item.description,
                )

    def snapshot(self) -> list[dict]:
        """Return a list of pending items for dashboard display (not thread-safe; best-effort)."""
        return [
            {
                "activity_type": item.activity_type,
                "description": item.description,
                "priority": _PRIORITY_LABELS.get(item.priority, str(item.priority)),
                "priority_value": item.priority,
                "submitted_at": item.submitted_at,
            }
            for item in self._pending
        ]

    def running_snapshot(self) -> dict | None:
        """Return the currently running item, or None."""
        r = self._running
        if r is None:
            return None
        return {
            "activity_type": r.activity_type,
            "description": r.description,
            "priority": _PRIORITY_LABELS.get(r.priority, str(r.priority)),
        }

    def _publish_queue_update(self) -> None:
        try:
            from utils.agent.supervisor import publish_event

            publish_event(
                {
                    "event_type": "queue_update",
                    "pending": self.snapshot(),
                    "pending_count": len(self._pending),
                    "running": self.running_snapshot(),
                }
            )
        except Exception:  # nosec B110 — best-effort SSE
            pass


# Module-level singleton
_work_queue: PueoWorkQueue | None = None


def init_work_queue() -> PueoWorkQueue:
    """Create and register the module-level work queue singleton."""
    global _work_queue
    _work_queue = PueoWorkQueue()
    return _work_queue


def get_work_queue() -> PueoWorkQueue:
    """Return the running work queue. Raises if not initialised."""
    if _work_queue is None:
        raise RuntimeError(
            "PueoWorkQueue not initialised — call init_work_queue() first"
        )
    return _work_queue


def get_work_queue_or_none() -> PueoWorkQueue | None:
    """Return the work queue if initialised, else None (standalone / test mode)."""
    return _work_queue
