"""safety.schemawait: an API pod waits for its release's migrations."""

from __future__ import annotations

import pytest

from safety import schemawait


def test_returns_once_nothing_is_missing(monkeypatch):
    answers = iter([["023_provider_budget.sql"], []])
    monkeypatch.setattr(schemawait, "wait_for_db", lambda attempts=30: None)
    monkeypatch.setattr(schemawait, "missing_migrations", lambda: next(answers))
    monkeypatch.setattr(schemawait.time, "sleep", lambda s: None)
    assert schemawait.main(["--timeout", "60"]) == 0


def test_times_out(monkeypatch):
    monkeypatch.setattr(schemawait, "wait_for_db", lambda attempts=30: None)
    monkeypatch.setattr(schemawait, "missing_migrations", lambda: ["023_provider_budget.sql"])
    monkeypatch.setattr(schemawait.time, "sleep", lambda s: None)
    assert schemawait.main(["--timeout", "0"]) == 1


@pytest.mark.db
def test_a_migrated_database_has_nothing_missing(db_conn):
    assert schemawait.missing_migrations() == []
