import time


class RateLimitExceeded(Exception):
    pass


class Debouncer:
    """Leading-edge debounce: fires on the first event, suppresses repeats within the window."""

    def __init__(self, window_seconds: float):
        self._window = window_seconds
        self._last_trigger: float | None = None

    def record(self) -> bool:
        """Record an event. Returns True if action should trigger (first in window)."""
        now = time.monotonic()
        if self._last_trigger is None or now - self._last_trigger >= self._window:
            self._last_trigger = now
            return True
        return False


class RateLimiter:
    """Sliding-window rate limiter. Raises RateLimitExceeded when over budget."""

    def __init__(self, max_calls: int, period_seconds: float):
        self._max = max_calls
        self._period = period_seconds
        self._timestamps: list[float] = []

    def check(self) -> None:
        """Consume one token or raise RateLimitExceeded."""
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < self._period]
        if len(self._timestamps) >= self._max:
            raise RateLimitExceeded(
                f"Rate limit of {self._max} calls per {self._period}s exceeded"
            )
        self._timestamps.append(now)
