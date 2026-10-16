"""macOS idle-sleep guard for the Pueo supervisor.

On macOS, asyncio monotonic time (time.monotonic) does NOT advance during
system sleep.  This means poll intervals are stretched by the sleep duration
and events that fire during sleep are silently lost.  The SleepGuard class
optionally holds a `caffeinate -i` assertion to prevent idle sleep while Pueo
is running, so monitoring, log tailing, and the HA event stream run
continuously.

host_sleep_seconds() is a pure helper that estimates how long the host was
asleep between two wall/mono sample pairs — useful for catch-up-wake logic in
the supervisor's event dispatcher.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 — /usr/bin/caffeinate is a fixed macOS system binary
import sys
from pathlib import Path
from typing import Optional

from utils.core.logging import get_logger

_log = get_logger("sleep_guard")

HOST_SLEEP_THRESHOLD_SECONDS: float = 30.0
_CAFFEINATE = Path("/usr/bin/caffeinate")


def host_sleep_seconds(
    wall_prev: float,
    mono_prev: float,
    wall_now: float,
    mono_now: float,
) -> float:
    """Return estimated host sleep duration in seconds between two sample pairs.

    macOS monotonic time excludes sleep; wall time does not.  The gap between
    wall elapsed and mono elapsed is the time spent asleep.  Returns 0.0 if
    the difference is negative (clock jitter, NTP step, first sample).
    """
    return max(0.0, (wall_now - wall_prev) - (mono_now - mono_prev))


class SleepGuard:
    """Hold a caffeinate assertion that blocks idle sleep while Pueo runs.

    Uses `caffeinate -i -w <pid>` so the assertion is:
      - `-i`: idle-sleep only (display may still sleep).
      - `-w <pid>`: automatically released when this process exits, even on
        crash, so Pueo can never accidentally hold a permanent assertion.

    On non-macOS hosts, or when /usr/bin/caffeinate is absent, all methods
    are no-ops so callers need no platform guards.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[bytes]] = None  # type: ignore[type-arg]

    @staticmethod
    def supported() -> bool:
        return sys.platform == "darwin" and _CAFFEINATE.exists()

    def start(self) -> None:
        """Spawn caffeinate.  Idempotent — safe to call when already active."""
        if not self.supported():
            _log.info("sleep_guard_unsupported", platform=sys.platform)
            return
        if self.is_active():
            return
        pid = os.getpid()
        self._proc = (
            subprocess.Popen(  # nosec B603 — fixed absolute path, no user input
                [str(_CAFFEINATE), "-i", "-w", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        _log.info("sleep_guard_started", pid=pid, caffeinate_pid=self._proc.pid)

    def stop(self) -> None:
        """Terminate caffeinate.  Idempotent — safe to call when not active."""
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:  # nosec B110
                proc.kill()
        _log.info("sleep_guard_stopped")

    def is_active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# Module singleton — main.py calls apply_sleep_policy(); other callers use
# get_sleep_guard() when they need to check is_active().
_guard: SleepGuard = SleepGuard()


def get_sleep_guard() -> SleepGuard:
    return _guard


def apply_sleep_policy(enabled: bool) -> None:
    """Start or stop the sleep guard according to the `prevent_idle_sleep` setting."""
    if enabled:
        _guard.start()
    else:
        _guard.stop()
