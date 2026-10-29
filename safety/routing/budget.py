"""DailyBudget: how many upstream calls a provider may take per UTC day.

Transitous asks for under 720 plans a day from us in total, across prod and
dev (settings.transit_daily_budget: 500 on prod, 100 in dev's .env). Photon
states no cap; geocode_daily_budget is a politeness bound. Counted per process,
like the rate limiter and the caches: one uvicorn per instance is the topology
(safety/api/ratelimit.py), and a restart handing back a fresh day's allowance
costs at most one extra day's worth, which the numbers above leave room for.

Only real upstream calls are charged -- cache hits are free -- and a call is
charged when it is made, not when it succeeds: a timed-out plan may well have
been computed upstream, and counting it is what keeps us under their number.

The day turns over at UTC midnight, because that is the only midnight two
instances and a provider can agree on. The clock is injectable for tests.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from safety.routing.errors import BudgetExhausted


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DailyBudget:
    def __init__(
        self, limit: int, provider: str, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self.limit = max(0, int(limit))
        self.provider = provider
        self._clock = clock
        self._lock = threading.Lock()
        self._day: date = self._today()
        self._used = 0

    def _today(self) -> date:
        # astimezone: a clock handing back local time must still turn the day
        # over at UTC midnight, not local midnight.
        return self._clock().astimezone(UTC).date()

    def _roll(self) -> None:
        """Start a new day if the clock has passed midnight. Caller holds the lock."""
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = 0

    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.limit - self._used)

    def seconds_until_reset(self) -> int:
        now = self._clock().astimezone(UTC)
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        return max(1, int((midnight - now).total_seconds()))

    def take(self) -> None:
        """Charge one call, or raise BudgetExhausted without charging."""
        with self._lock:
            self._roll()
            if self._used >= self.limit:
                exhausted = True
            else:
                self._used += 1
                exhausted = False
        if exhausted:
            raise BudgetExhausted(provider=self.provider, retry_after=self.seconds_until_reset())

    def refund(self) -> None:
        """Give back a charge for a call that was never made."""
        with self._lock:
            self._roll()
            self._used = max(0, self._used - 1)
