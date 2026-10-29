"""CircuitBreaker: stop calling a provider that has just failed three times.

Without it, a provider that is down costs every request the full
routing_timeout_seconds before it answers 503, and keeps hammering a service
that is already struggling. After `threshold` consecutive failures the breaker
opens: calls fail at once with ProviderUnavailable and /directions/status
reports the mode unavailable, for `cooldown` seconds. Then one trial call is
let through (half open); success closes the breaker, failure reopens it for
another cooldown.

What counts as a failure is the provider being unreachable or broken --
transport errors, timeouts, 5xx, an unreadable body. A provider that answers
"no route" or "bad request" is up and working, and that is a success here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = 3,
        cooldown: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_out = False

    @property
    def available(self) -> bool:
        """For status reporting: True unless open and still cooling down. Does
        not claim the half-open trial."""
        with self._lock:
            return self._opened_at is None or self._clock() - self._opened_at >= self.cooldown

    def allow(self) -> bool:
        """Whether a call may go out now. In the half-open state exactly one
        caller gets True until that trial reports back."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at < self.cooldown or self._trial_out:
                return False
            self._trial_out = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_out = False

    def release(self) -> None:
        """A claimed call ended without an answer either way (cancelled, or
        never sent): hand the half-open trial back without counting it."""
        with self._lock:
            self._trial_out = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._trial_out = False
            if self._opened_at is not None or self._failures >= self.threshold:
                self._opened_at = self._clock()
