"""SharedDailyBudget: one provider allowance for every API replica (migration 023)."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from safety.routing import service
from safety.routing.budget import DailyBudget, SharedDailyBudget
from safety.routing.errors import BudgetExhausted


class WallClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------- no database


def test_zero_limit_refuses_without_touching_the_database():
    def boom():
        raise AssertionError("must not connect")

    with pytest.raises(BudgetExhausted):
        SharedDailyBudget(0, "photon", boom).take()


def test_unreachable_counter_fails_closed():
    @contextmanager
    def down():
        raise OSError("database unreachable")
        yield  # pragma: no cover

    budget = SharedDailyBudget(10, "transitous", down)
    with pytest.raises(BudgetExhausted):
        budget.take()
    assert budget.remaining() == 0
    budget.refund()  # swallowed: a lost refund costs one call, nothing more


class _Pool:
    def connection(self):  # pragma: no cover - never opened here
        raise AssertionError


def test_backend_selection(monkeypatch):
    monkeypatch.setattr(service.settings, "routing_budget_backend", "db")
    assert isinstance(service._budget(5, "photon", _Pool()), SharedDailyBudget)
    # No pool (a test building Routing alone): the in-memory counter.
    assert type(service._budget(5, "photon", None)) is DailyBudget
    monkeypatch.setattr(service.settings, "routing_budget_backend", "memory")
    assert type(service._budget(5, "photon", _Pool())) is DailyBudget


# ---------------------------------------------------------------- database


@pytest.fixture
def connection(db_conn):
    """A connection factory over fresh connections, and a provider name of its own."""
    from safety.db import connect

    provider = f"test-{uuid.uuid4().hex[:8]}"

    @contextmanager
    def factory():
        with connect() as conn:
            yield conn

    yield factory, provider
    db_conn.execute("DELETE FROM public.provider_budget WHERE provider = %s", (provider,))
    db_conn.commit()


@pytest.mark.db
def test_shared_budget_runs_out_and_refunds(connection):
    factory, provider = connection
    clock = WallClock(datetime(2026, 11, 2, 23, 0, tzinfo=UTC))
    budget = SharedDailyBudget(2, provider, factory, clock=clock)
    budget.take()
    budget.take()
    assert budget.remaining() == 0
    with pytest.raises(BudgetExhausted) as info:
        budget.take()
    assert info.value.retry_after == 3600
    budget.refund()
    assert budget.remaining() == 1


@pytest.mark.db
def test_two_replicas_share_one_allowance(connection):
    factory, provider = connection
    clock = WallClock(datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    a = SharedDailyBudget(3, provider, factory, clock=clock)
    b = SharedDailyBudget(3, provider, factory, clock=clock)
    a.take()
    b.take()
    a.take()
    with pytest.raises(BudgetExhausted):
        b.take()
    assert a.remaining() == b.remaining() == 0


@pytest.mark.db
def test_shared_budget_resets_at_utc_midnight(connection):
    factory, provider = connection
    clock = WallClock(datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC))
    budget = SharedDailyBudget(1, provider, factory, clock=clock)
    budget.take()
    assert budget.remaining() == 0
    clock.now += timedelta(seconds=1)
    assert budget.remaining() == 1
    budget.take()


@pytest.mark.db
def test_concurrent_takes_never_exceed_the_limit(connection):
    factory, provider = connection
    budget = SharedDailyBudget(5, provider, factory)
    granted = []
    barrier = threading.Barrier(12)

    def worker():
        barrier.wait()
        try:
            budget.take()
            granted.append(1)
        except BudgetExhausted:
            pass

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(granted) == 5
    assert budget.remaining() == 0
