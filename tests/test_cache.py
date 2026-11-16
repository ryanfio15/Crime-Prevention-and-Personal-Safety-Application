"""The stamped response cache (F8): thread-safe, single-flight, byte-accurate."""

from __future__ import annotations

import random
import threading
import time

import pytest

from safety.api import main


@pytest.fixture(autouse=True)
def fixed_stamp(monkeypatch):
    stamp = {"value": "s1"}
    monkeypatch.setattr(main, "_refresh_stamp", lambda conn, source_id=None: stamp["value"])
    return stamp


def _run_threads(n, target):
    barrier = threading.Barrier(n)
    results, errors = [None] * n, [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = target(i)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results, errors


def test_concurrent_misses_run_the_producer_once():
    calls = []
    lock = threading.Lock()

    def slow_producer():
        with lock:
            calls.append(1)
        time.sleep(0.2)
        return "layer"

    results, errors = _run_threads(16, lambda i: main.cached(None, ("k",), slow_producer))
    assert errors == [None] * 16
    assert results == ["layer"] * 16
    assert len(calls) == 1
    assert main._inflight == {}


def test_a_failing_producer_fails_everyone_then_retries():
    calls = []

    def failing():
        calls.append(1)
        time.sleep(0.2)
        raise RuntimeError("boom")

    results, errors = _run_threads(8, lambda i: main.cached(None, ("k",), failing))
    assert all(isinstance(e, RuntimeError) for e in errors)
    assert len(calls) == 1
    assert main._inflight == {}
    assert main.cached(None, ("k",), lambda: (calls.append(1), "ok")[1]) == "ok"
    assert len(calls) == 2


def test_byte_accounting_stays_exact_under_concurrency(fixed_stamp, monkeypatch):
    monkeypatch.setattr(main.settings, "cache_max_entries", 20)
    keys = [("k", i) for i in range(12)]

    def workload(i):
        rng = random.Random(i)
        for _ in range(25):
            key = rng.choice(keys)
            if rng.random() < 0.3:
                fixed_stamp["value"] = f"s{rng.randrange(3)}"  # forces replacements
            size = rng.randrange(1, 200)
            main.cached(None, key, lambda size=size: "x" * size)

    _, errors = _run_threads(8, workload)
    assert errors == [None] * 8
    assert main._cache_stats["bytes"] == sum(len(v[1]) for v in main._cache.values())
    assert main._inflight == {}


def test_stamp_change_replaces_the_entry(fixed_stamp):
    assert main.cached(None, ("k",), lambda: "old") == "old"
    fixed_stamp["value"] = "s2"
    assert main.cached(None, ("k",), lambda: "newer") == "newer"
    assert main._cache[("k",)] == ("s2", "newer")
    assert main._cache_stats["bytes"] == len("newer")


def test_eviction_respects_max_entries(monkeypatch):
    monkeypatch.setattr(main.settings, "cache_max_entries", 3)
    for i in range(5):
        main.cached(None, ("k", i), lambda i=i: f"v{i}")
    assert list(main._cache) == [("k", 2), ("k", 3), ("k", 4)]
    assert main._cache_stats["evictions"] == 2


def test_health_reads_stats_under_the_lock():
    snapshot = main._cache_stats_snapshot()
    assert set(snapshot) == {"hits", "misses", "bytes", "evictions"}


# ---------------------------------------------------------------- stamp memo


def test_stamp_is_checked_on_every_lookup_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main, "_refresh_stamp", lambda conn, source_id=None: calls.append(source_id) or "s1"
    )
    for _ in range(3):
        main.cached(None, ("k",), lambda: "v", source_id="phl")
    assert calls == ["phl", "phl", "phl"]


def test_stamp_ttl_spares_the_database_and_still_expires(monkeypatch):
    calls = []
    stamp = {"value": "s1"}

    def refresh(conn, source_id=None):
        calls.append(source_id)
        return stamp["value"]

    clock = {"now": 1000.0}
    monkeypatch.setattr(main, "_refresh_stamp", refresh)
    monkeypatch.setattr(main.settings, "cache_stamp_ttl_seconds", 5.0)
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["now"])

    assert main.cached(None, ("k",), lambda: "old", source_id="phl") == "old"
    stamp["value"] = "s2"  # an ETL refresh lands
    # Inside the TTL the memoised stamp is trusted: no query, the old layer.
    assert main.cached(None, ("k",), lambda: "new", source_id="phl") == "old"
    assert calls == ["phl"]
    # Other cities have their own memo.
    main.cached(None, ("k2",), lambda: "x", source_id="chi")
    assert calls == ["phl", "chi"]
    # Past the TTL the stamp is re-read and the new snapshot served.
    clock["now"] += 5.1
    assert main.cached(None, ("k",), lambda: "new", source_id="phl") == "new"
    assert calls == ["phl", "chi", "phl"]
