"""Shared test setup.

Two kinds of test live here:

* Pure tests, which need nothing but the package. They run everywhere.
* `@pytest.mark.db` tests, which need the PostGIS database. CI provides one and
  sets SAFETY_TEST_DB=1; anywhere else they are skipped rather than left to fail
  on a connection error.

API tests use `TestClient(app)` *without* `with`: entering the client runs the
app's lifespan, which opens a real connection pool and waits for a database
that does not exist outside CI. Override `main.get_conn` through
`app.dependency_overrides` instead.
"""

from __future__ import annotations

import os

import pytest


def _db_enabled() -> bool:
    return os.environ.get("SAFETY_TEST_DB") == "1"


def pytest_collection_modifyitems(config, items):
    if _db_enabled():
        return
    skip = pytest.mark.skip(reason="needs the database; set SAFETY_TEST_DB=1 (CI does)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="module")
def db_conn():
    """A live connection, for db-marked tests only. Module-scoped, so a module's
    expensive fixture (a gold refresh) is built once and read by every test."""
    from safety.db import connect, wait_for_db

    wait_for_db()
    with connect() as conn:
        yield conn


def _reset_api_state() -> None:
    from safety.api import main, ratelimit

    ratelimit._buckets.clear()
    main._cache.clear()
    for key in main._cache_stats:
        main._cache_stats[key] = 0
    inflight = getattr(main, "_inflight", None)
    if inflight is not None:
        inflight.clear()
    main.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_api_state():
    """Keep rate-limit buckets, the response cache and dependency overrides from
    leaking between tests."""
    _reset_api_state()
    yield
    _reset_api_state()
