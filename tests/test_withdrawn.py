"""Records withdrawn upstream (F13): the pure parts -- window, guard, mode,
adapter wiring, and the never-raise wrappers around the database steps."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from safety.etl import run, withdrawn
from safety.etl.adapters import ADAPTERS, get_adapter
from safety.etl.withdrawn import Stratum, decide, reconcile_window, resolve_mode
from tests.test_adapters import _config


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ------------------------------------------------------------------ window


def test_window_trims_one_day_at_each_end():
    assert reconcile_window(_utc(2026, 9, 1, 5), _utc(2026, 10, 8), _utc(2026, 10, 7, 12)) == (
        date(2026, 9, 2),
        date(2026, 10, 6),
    )


def test_window_ends_before_now_when_until_is_in_the_future():
    # cmd_incremental asks for until = now + 1 day; the domain still ends yesterday.
    lo, hi = reconcile_window(_utc(2026, 9, 1), _utc(2026, 10, 9), _utc(2026, 10, 7, 23))
    assert (lo, hi) == (date(2026, 9, 2), date(2026, 10, 6))


def test_window_uses_until_when_it_is_earlier_than_now():
    assert reconcile_window(_utc(2026, 9, 1), _utc(2026, 9, 10), _utc(2026, 10, 7))[1] == date(2026, 9, 9)


def test_empty_window_is_none():
    assert reconcile_window(_utc(2026, 10, 6), _utc(2026, 10, 7), _utc(2026, 10, 7)) is None


def test_one_day_window_survives_the_margins():
    assert reconcile_window(_utc(2026, 10, 1), _utc(2026, 10, 4), _utc(2026, 10, 7)) == (
        date(2026, 10, 2),
        date(2026, 10, 3),
    )


# ------------------------------------------------------------------ guard


def test_nothing_absent_is_none():
    assert decide([Stratum("2026-09", 100, 0)]) == ("none", None)


def test_prior_zero_is_none():
    assert decide([]) == ("none", None)


def test_five_percent_absent_deletes():
    assert decide([Stratum("2026-09", 100, 5)])[0] == "delete"


def test_eleven_percent_overall_skips():
    decision, reason = decide([Stratum("2026-08", 50, 6), Stratum("2026-09", 50, 5)])
    assert decision == "skip"
    assert "89%" in reason


def test_one_partial_large_stratum_skips_even_if_overall_is_fine():
    decision, reason = decide([Stratum("2026-08", 200, 30), Stratum("2026-09", 2000, 0)])
    assert decision == "skip"
    assert "2026-08" in reason


def test_small_stratum_partly_absent_deletes():
    assert decide([Stratum("2026-08", 10, 4), Stratum("2026-09", 1000, 0)])[0] == "delete"


def test_whole_stratum_absent_skips():
    decision, reason = decide([Stratum("2026-08 Assault", 5, 5), Stratum("2026-09", 1000, 0)])
    assert decision == "skip"
    assert "empty" in reason


def test_tiny_whole_stratum_absent_still_deletes():
    # Below WHOLE_STRATUM_MIN_ROWS a vanished stratum is just withdrawals.
    assert decide([Stratum("2026-08", 2, 2), Stratum("2026-09", 1000, 0)])[0] == "delete"


# ------------------------------------------------------------------ mode


@pytest.mark.parametrize(
    "value, mode",
    [("bogus", "report"), (" Delete ", "delete"), ("", "report"), (None, "report"),
     ("OFF", "off"), ("report", "report")],
)
def test_resolve_mode(value, mode):
    assert resolve_mode(value) == mode


def test_defaults_are_report_and_ninety_days(monkeypatch):
    # User decision 2026-10-07: the code default stays report; prod starts there.
    from safety.config import Settings

    monkeypatch.delenv("WITHDRAWN_RECONCILE", raising=False)
    monkeypatch.delenv("WITHDRAWN_RETENTION_DAYS", raising=False)
    s = Settings(_env_file=None)
    assert s.withdrawn_reconcile == "report"
    assert s.withdrawn_retention_days == 90


# ------------------------------------------------------------------ adapters

EXPECTED = {
    "phl": ("occurred", None),
    "chi": ("occurred", None),
    "sea": ("occurred", None),
    "lax": ("occurred", None),
    "dc": ("reported", None),
    "aus": ("occurred", "raw_source_category"),
}


def test_every_adapter_has_a_deliberate_reconcile_basis():
    # A new adapter must be added here on purpose, with its filter field in view.
    assert {sid: (cls.reconcile_basis, cls.reconcile_stratum) for sid, cls in ADAPTERS.items()} == EXPECTED


@pytest.mark.parametrize("source_id", sorted(ADAPTERS))
def test_incident_key_matches_the_staging_expression(source_id):
    # withdrawn.assess builds seen keys as staging does (transform.stage_records).
    adapter = get_adapter(_config(source_id))
    assert adapter.incident_key("X") == f"{source_id}:X"


# ------------------------------------------------------------------ never raise


class _Boom:
    def __init__(self):
        self.rolled_back = False
        self.committed = False

    def execute(self, *a, **k):
        raise RuntimeError("database is gone")

    def cursor(self, *a, **k):
        raise RuntimeError("database is gone")

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


def _assessment(decision="delete"):
    return withdrawn.Assessment(
        source_id="sea", dataset="test", basis="occurred", lo=date(2026, 9, 2), hi=date(2026, 10, 6),
        prior=100, absent_keys=["sea:1"], strata=[Stratum("2026-09", 100, 1)],
        decision=decision, reason=None,
    )


def test_reconcile_failure_rolls_back_and_does_not_raise():
    conn = _Boom()
    assert run._reconcile_safely(conn, 1, _assessment(), "delete") == {"action": "error"}
    assert conn.rolled_back and not conn.committed


def test_prune_failure_rolls_back_and_does_not_raise():
    conn = _Boom()
    assert run._prune_safely(conn, "sea", 90) is None
    assert conn.rolled_back and not conn.committed


def test_none_decision_writes_nothing():
    # No cursor is opened (the fake would raise) when nothing is absent.
    assert withdrawn.apply(_Boom(), 1, _assessment("none"), "delete")["action"] == "none"
