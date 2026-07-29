"""LoopSupervisor — wraps monitoring coroutines with health tracking and backoff restart."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from utils.logging import get_logger

log = get_logger("supervisor")

_BACKOFF_START = 2.0
_BACKOFF_CAP = 300.0

# Shared event bus; supervisor writes, SSE endpoint reads.
event_bus: asyncio.Queue = asyncio.Queue(maxsize=1000)


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
            if status.paused:
                await asyncio.sleep(1.0)
                continue
            status.status = "running"
            status.last_run = time.time()
            self._emit(name)
            try:
                await coro_factory()
                break  # Clean return — daemon loops normally run forever
            except asyncio.CancelledError:
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
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._backoff_cap)

    def _emit(self, name: str) -> None:
        status = self._handles[name]
        try:
            self._bus.put_nowait(
                {
                    "event_type": "loop_status",
                    "loop": name,
                    "status": status.status,
                    "error_count": status.error_count,
                    "last_error": status.last_error,
                    "last_run": status.last_run,
                    "next_run": status.next_run,
                    "paused": status.paused,
                }
            )
        except asyncio.QueueFull:
            pass  # Drop non-critical status event rather than block

    def cancel_all(self) -> None:
        """Cancel all supervised tasks (called on SIGTERM/SIGINT)."""
        for task in self._tasks.values():
            task.cancel()

    def get_statuses(self) -> list[LoopStatus]:
        return list(self._handles.values())
