"""In-memory rate limiting and failure throttling.

# PROD: replace with Redis-backed counters (or a gateway/CDN) so limits are
# shared across replicas instead of per-process.
"""

import time


class FixedWindowCounter:
    """Counts events per key in a fixed time window, resetting after it elapses."""

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._counts: dict[str, tuple[int, float]] = {}

    def increment(self, key: str) -> int:
        now = time.monotonic()
        count, window_start = self._counts.get(key, (0, now))
        if now - window_start >= self._window:
            count, window_start = 0, now
        count += 1
        self._counts[key] = (count, window_start)
        return count

    def reset(self, key: str) -> None:
        self._counts.pop(key, None)

    def clear(self) -> None:
        self._counts.clear()

    def peek(self, key: str) -> int:
        now = time.monotonic()
        count, window_start = self._counts.get(key, (0, now))
        if now - window_start >= self._window:
            return 0
        return count

    def retry_after(self, key: str) -> int | None:
        entry = self._counts.get(key)
        if entry is None:
            return None
        _, window_start = entry
        remaining = self._window - (time.monotonic() - window_start)
        return max(1, int(remaining)) if remaining > 0 else None


class LoginThrottle:
    """Per (email, ip) failure counter with a reset on success."""

    def __init__(self, max_failures: int, window_seconds: float = 15 * 60) -> None:
        self._max_failures = max_failures
        self._counter = FixedWindowCounter(window_seconds)

    @staticmethod
    def _key(email: str, ip: str) -> str:
        return f"{email.lower()}:{ip}"

    def record_failure(self, email: str, ip: str) -> None:
        self._counter.increment(self._key(email, ip))

    def reset(self, email: str, ip: str) -> None:
        self._counter.reset(self._key(email, ip))

    def retry_after(self, email: str, ip: str) -> int | None:
        key = self._key(email, ip)
        if self._counter.peek(key) >= self._max_failures:
            return self._counter.retry_after(key) or 1
        return None
