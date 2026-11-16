"""safety.bootstrap: migrate, enable the configured cities, converge under the ETL lock."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from safety import bootstrap, joblock, migrate, ops
from safety.etl import run


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(bootstrap, "wait_for_db", lambda: None)
    monkeypatch.setattr(migrate, "main", lambda argv: log.append(("migrate",)) or 0)

    def enable(argv):
        log.append(tuple(argv))
        return 1 if argv[-1] == "aus" else 0

    monkeypatch.setattr(run, "main", enable)

    @contextmanager
    def lock(wait=None):
        log.append(("lock",))
        yield None
        log.append(("unlock",))

    monkeypatch.setattr(joblock, "etl_lock", lock)
    monkeypatch.setattr(ops, "main", lambda argv: log.append(("ops",)) or 0)
    return log


def test_parse_cities_dedupes_and_normalises():
    assert bootstrap.parse_cities(" PHL, chi,,phl ,sea") == ["phl", "chi", "sea"]


def test_order_and_a_city_that_is_not_ready(calls):
    assert bootstrap.main(["--cities", "phl,aus"]) == 0
    assert calls == [
        ("migrate",),
        ("enable", "--city", "phl"),
        ("enable", "--city", "aus"),  # refused by its readiness check: reported, not fatal
        ("lock",),
        ("ops",),
        ("unlock",),
    ]


def test_default_is_all_six_cities(calls):
    bootstrap.main([])
    enabled = [c[-1] for c in calls if c[0] == "enable"]
    assert enabled == ["phl", "chi", "sea", "lax", "dc", "aus"]


def test_failed_migrate_stops_before_anything_else(calls, monkeypatch):
    monkeypatch.setattr(migrate, "main", lambda argv: calls.append(("migrate",)) or 1)
    assert bootstrap.main([]) == 1
    assert calls == [("migrate",)]


def test_unfinished_convergence_fails_the_job(calls, monkeypatch):
    monkeypatch.setattr(ops, "main", lambda argv: 1)
    assert bootstrap.main(["--skip-enable"]) == 1
