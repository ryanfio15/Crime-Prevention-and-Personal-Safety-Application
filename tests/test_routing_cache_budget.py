"""The provider cache (TTL, LRU, single flight), the daily budget and the
circuit breaker, all on injected clocks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from safety.routing.breaker import CircuitBreaker
from safety.routing.budget import DailyBudget
from safety.routing.errors import BudgetExhausted
from safety.routing.schema import Point
from safety.routing.ttlcache import TTLCache


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------- TTL and LRU


def test_entry_lives_for_its_ttl_and_no_longer():
    clock = Clock()
    cache = TTLCache(10, ttl=60, clock=clock)
    cache.set("k", "v")
    clock.now = 59.9
    assert cache.get("k") == "v"
    clock.now = 60.0
    assert cache.get("k") is None
    assert len(cache) == 0  # dropped on the way past


def test_lru_evicts_one_least_recently_used_entry():
    cache = TTLCache(2, ttl=60, clock=Clock())
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # a is now the most recent
    cache.set("c", 3)
    assert "a" in cache and "c" in cache and "b" not in cache
    assert cache.stats["evictions"] == 1


def test_overwrite_does_not_evict():
    cache = TTLCache(2, ttl=60, clock=Clock())
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 3)
    assert cache.get("a") == 3 and cache.get("b") == 2


def test_key_rounding_shares_an_entry():
    # 4 dp is ~10 m: two clicks on the same doorstep are one cache entry.
    assert Point(39.95241, -75.16361).rounded(4) == Point(39.95244, -75.16359).rounded(4)
    assert Point(39.9524, -75.1636).rounded(4) != Point(39.9534, -75.1636).rounded(4)


# ---------------------------------------------------------------- single flight


def test_concurrent_misses_make_one_call():
    cache = TTLCache(10, ttl=60)
    calls = 0

    async def main():
        gate = asyncio.Event()

        async def produce():
            nonlocal calls
            calls += 1
            await gate.wait()
            return {"answer": 42}

        waiters = [asyncio.ensure_future(cache.get_or_create("k", produce)) for _ in range(5)]
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(*waiters)

    results = asyncio.run(main())
    assert calls == 1
    assert all(r == {"answer": 42} for r in results)
    assert cache.get("k") == {"answer": 42}


def test_none_is_cached():
    cache = TTLCache(10, ttl=60)
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return None

    async def main():
        await cache.get_or_create("k", produce)
        return await cache.get_or_create("k", produce)

    assert asyncio.run(main()) is None
    assert calls == 1


def test_failures_are_shared_but_not_cached():
    cache = TTLCache(10, ttl=60)
    calls = 0

    async def main():
        gate = asyncio.Event()

        async def produce():
            nonlocal calls
            calls += 1
            await gate.wait()
            raise RuntimeError("upstream down")

        waiters = [asyncio.ensure_future(cache.get_or_create("k", produce)) for _ in range(3)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(r, RuntimeError) for r in results)
        assert calls == 1

        async def ok():
            nonlocal calls
            calls += 1
            return "fine"

        return await cache.get_or_create("k", ok)

    assert asyncio.run(main()) == "fine"
    assert calls == 2


def test_one_waiter_cancelling_does_not_cancel_the_others():
    cache = TTLCache(10, ttl=60)

    async def main():
        gate = asyncio.Event()

        async def produce():
            await gate.wait()
            return "done"

        first = asyncio.ensure_future(cache.get_or_create("k", produce))
        second = asyncio.ensure_future(cache.get_or_create("k", produce))
        await asyncio.sleep(0)
        first.cancel()  # the client that started it disconnected
        await asyncio.sleep(0)
        gate.set()
        return await second

    assert asyncio.run(main()) == "done"
    assert cache.get("k") == "done"


# ---------------------------------------------------------------- daily budget


class WallClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


def test_budget_runs_out_and_says_when_it_returns():
    clock = WallClock(datetime(2026, 11, 2, 23, 0, tzinfo=UTC))
    budget = DailyBudget(2, "transitous", clock=clock)
    budget.take()
    budget.take()
    assert budget.remaining() == 0
    with pytest.raises(BudgetExhausted) as info:
        budget.take()
    assert info.value.status == 503
    assert info.value.retry_after == 3600
    assert budget.remaining() == 0  # a refused call is not charged


def test_budget_resets_at_utc_midnight():
    clock = WallClock(datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC))
    budget = DailyBudget(3, "photon", clock=clock)
    for _ in range(3):
        budget.take()
    assert budget.remaining() == 0
    clock.now += timedelta(seconds=1)
    assert budget.remaining() == 3


def test_midnight_is_utc_not_local():
    # 19:30 in New York on 2 Nov is already 3 Nov in UTC: the day has turned.
    clock = WallClock(datetime(2026, 11, 2, 23, 30, tzinfo=UTC))
    budget = DailyBudget(1, "transitous", clock=clock)
    budget.take()
    clock.now = datetime(2026, 11, 2, 19, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert budget.remaining() == 1


def test_zero_budget_refuses_at_once():
    with pytest.raises(BudgetExhausted):
        DailyBudget(0, "photon").take()


# ---------------------------------------------------------------- circuit breaker


def test_breaker_opens_after_three_failures_and_half_opens_after_cooldown():
    clock = Clock()
    breaker = CircuitBreaker(threshold=3, cooldown=120, clock=clock)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.allow() and breaker.available
    breaker.record_failure()
    assert not breaker.allow() and not breaker.available

    clock.now = 120
    assert breaker.available
    assert breaker.allow()  # the one trial
    assert not breaker.allow()  # everyone else waits for it
    breaker.record_failure()  # trial failed: open for another cooldown
    assert not breaker.available
    clock.now = 240
    assert breaker.allow()
    breaker.record_success()
    assert breaker.allow() and breaker.allow()


def test_a_success_resets_the_count():
    breaker = CircuitBreaker(threshold=3, clock=Clock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow()
