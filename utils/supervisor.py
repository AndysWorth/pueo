"""LoopSupervisor — wraps monitoring coroutines with health tracking and backoff restart."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from utils.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("supervisor")

_BACKOFF_START = 2.0
_BACKOFF_CAP = 300.0

# Shared event bus — kept for backward compatibility; SSE subscribers use the fan-out below.
event_bus: asyncio.Queue = asyncio.Queue(maxsize=1000)

# SSE fan-out: per-connection subscriber queues
_sse_subscribers: list[asyncio.Queue] = []

# Running supervisor instance (set by main.py supervisor_main; None in standalone dashboard mode)
_supervisor_instance: "LoopSupervisor | None" = None


def publish_event(event: dict) -> None:
    """Publish an event to the shared bus and all SSE subscriber queues."""
    try:
        event_bus.put_nowait(event)
    except asyncio.QueueFull:
        pass
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscribe() -> asyncio.Queue:
    """Create and register a new SSE subscriber queue. Call unsubscribe() in a finally block."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove a subscriber queue from the fan-out list."""
    try:
        _sse_subscribers.remove(q)
    except ValueError:
        pass


def set_supervisor_instance(sv: "LoopSupervisor") -> None:
    """Register the running supervisor so the dashboard can query loop statuses."""
    global _supervisor_instance
    _supervisor_instance = sv


def get_supervisor_instance() -> "LoopSupervisor | None":
    """Return the running LoopSupervisor, or None if not in supervisor mode."""
    return _supervisor_instance


@dataclass
class LoopStatus:
    name: str
    status: str = (
        "starting"  # starting | running | paused | error | restarting | disabled
    )
    error_count: int = 0
    last_error: str = ""
    last_run: float | None = None
    next_run: float | None = None
    paused: bool = False
    run_now_pending: bool = False


class LoopSupervisor:
    """Manages named monitoring loops with exception-catching restart and status tracking."""

    def __init__(
        self,
        bus: "asyncio.Queue | None" = None,
        backoff_start: float = _BACKOFF_START,
        backoff_cap: float = _BACKOFF_CAP,
    ) -> None:
        self._bus = bus if bus is not None else event_bus
        self._backoff_start = backoff_start
        self._backoff_cap = backoff_cap
        self._handles: dict[str, LoopStatus] = {}
        self._tasks: dict[str, "asyncio.Task[None]"] = {}

    def start(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        """Register and start a named loop. Call from inside a running event loop."""
        status = LoopStatus(name=name)
        self._handles[name] = status
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_with_restart(name, coro_factory),
            name=f"pueo_loop_{name}",
        )
        self._tasks[name] = task

    async def _run_with_restart(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        status = self._handles[name]
        delay = self._backoff_start
        while True:
            # --- Paused state: sleep 1 s per tick until resumed ---
            if status.paused:
                if status.status != "paused":
                    status.status = "paused"
                    self._emit(name)
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    if status.paused:
                        raise  # shutdown cancel; paused still True → propagate to exit
                    # run_now() cleared paused before cancelling; fall through
                continue  # re-check status.paused

            # --- Running state ---
            status.status = "running"
            status.last_run = time.time()
            self._emit(name)
            try:
                await coro_factory()
                break  # Clean return — daemon loops normally run forever
            except asyncio.CancelledError:
                if status.paused:
                    continue  # pause() called while running → enter paused state
                if status.run_now_pending:
                    status.run_now_pending = False
                    continue  # run_now() called while running → restart immediately
                status.status = "disabled"
                self._emit(name)
                return
            except Exception as exc:
                status.error_count += 1
                status.last_error = str(exc)
                status.status = "error"
                status.next_run = time.time() + delay
                self._emit(name)
                log.error("loop_crashed", loop=name, error=str(exc), restart_in=delay)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    if status.paused:
                        continue  # pause() during backoff → paused state
                    if status.run_now_pending:
                        status.run_now_pending = False
                        continue  # run_now() during backoff → restart immediately
                    return  # clean cancel during backoff
                delay = min(delay * 2, self._backoff_cap)

    def _emit(self, name: str) -> None:
        status = self._handles[name]
        event = {
            "event_type": "loop_status",
            "loop": name,
            "status": status.status,
            "error_count": status.error_count,
            "last_error": status.last_error,
            "last_run": status.last_run,
            "next_run": status.next_run,
            "paused": status.paused,
        }
        # Write to self._bus (may be a test-injected queue distinct from event_bus)
        if self._bus is not event_bus:
            try:
                self._bus.put_nowait(event)
            except asyncio.QueueFull:
                pass
        # publish_event handles the module-level bus + all SSE subscriber queues
        publish_event(event)

    def pause(self, name: str) -> None:
        """Pause a loop by name. The running coro is cancelled; the restart loop re-enters
        the paused-sleep branch instead of restarting the coro."""
        status = self._handles[name]  # raises KeyError if unknown
        if status.paused:
            return
        status.paused = True
        status.status = "paused"
        self._emit(name)
        task = self._tasks.get(name)
        if task and not task.done():
            task.cancel()

    def resume(self, name: str) -> None:
        """Resume a paused loop. The next paused-sleep tick will detect paused=False
        and proceed to run the coro."""
        status = self._handles[name]  # raises KeyError if unknown
        if not status.paused:
            return
        status.paused = False
        self._emit(name)

    def run_now(self, name: str) -> None:
        """Run a loop's next iteration immediately, interrupting any sleep or pause.
        If the loop is paused it is also resumed."""
        status = self._handles[name]  # raises KeyError if unknown
        status.paused = False
        status.run_now_pending = True
        task = self._tasks.get(name)
        if task and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        """Cancel all supervised tasks (called on SIGTERM/SIGINT)."""
        for task in self._tasks.values():
            task.cancel()

    def get_statuses(self) -> list[LoopStatus]:
        return list(self._handles.values())
