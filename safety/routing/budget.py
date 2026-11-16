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


class SharedDailyBudget(DailyBudget):
    """DailyBudget counted in Postgres, so every API replica draws on one allowance.

    The per-process DailyBudget above is right for one uvicorn on one host. Run
    N replicas behind a load balancer and each gets its own allowance -- N x 500
    Transitous plans a day against their "under 720" -- and every restart or
    rolling deploy hands a replica a fresh one. This keeps one row per provider
    per UTC day in public.provider_budget (migration 023) and charges it with a
    single atomic upsert, so concurrent replicas can never together exceed the
    limit.

    Same interface and clock as DailyBudget; `connection` is a context-manager
    factory yielding a psycopg connection (the API's pool.connection).

    Fails closed. If the counter cannot be reached, take() refuses the call as
    if the budget were spent: the database being down already takes the map
    with it, and the alternative -- spending a provider's allowance we cannot
    count -- is the one failure that is someone else's problem.

    Each charge is one primary-key upsert, a fraction of a millisecond, made
    only on a provider cache miss (cache hits are free, as before); it runs on
    the event loop like the in-memory lock it replaces.
    """

    _TAKE_SQL = """
        INSERT INTO public.provider_budget AS b (provider, day, used)
        VALUES (%(provider)s, %(day)s, 1)
        ON CONFLICT (provider, day) DO UPDATE SET used = b.used + 1
        WHERE b.used < %(limit)s
        RETURNING b.used
    """
    _REFUND_SQL = """
        UPDATE public.provider_budget SET used = greatest(used - 1, 0)
        WHERE provider = %(provider)s AND day = %(day)s
    """
    _USED_SQL = """
        SELECT used FROM public.provider_budget
        WHERE provider = %(provider)s AND day = %(day)s
    """

    def __init__(
        self,
        limit: int,
        provider: str,
        connection: Callable,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        super().__init__(limit, provider, clock)
        self._connection = connection

    def _params(self) -> dict:
        return {"provider": self.provider, "day": self._today(), "limit": self.limit}

    def _execute(self, sql: str) -> list:
        with self._connection() as conn:
            cur = conn.execute(sql, self._params())
            # The refund UPDATE returns no result set; fetchall() would raise.
            rows = cur.fetchall() if cur.description is not None else []
            conn.commit()
            return rows

    def remaining(self) -> int:
        try:
            rows = self._execute(self._USED_SQL)
        except Exception:
            return 0
        used = _used(rows[0]) if rows else 0
        return max(0, self.limit - used)

    def take(self) -> None:
        """Charge one call, or raise BudgetExhausted without charging."""
        # A zero limit refuses without a row: the upsert's INSERT arm would
        # otherwise charge the first call of the day before the WHERE applies.
        if self.limit <= 0:
            raise BudgetExhausted(provider=self.provider, retry_after=self.seconds_until_reset())
        try:
            rows = self._execute(self._TAKE_SQL)
        except Exception:
            rows = []
        if not rows:
            raise BudgetExhausted(provider=self.provider, retry_after=self.seconds_until_reset())

    def refund(self) -> None:
        """Give back a charge for a call that was never made."""
        try:
            self._execute(self._REFUND_SQL)
        except Exception:
            # Losing a refund costs one call of allowance, nothing more.
            pass


def _used(row) -> int:
    """`used` from a tuple row or a dict row (the API pool uses dict_row)."""
    return int(row["used"] if isinstance(row, dict) else row[0])
