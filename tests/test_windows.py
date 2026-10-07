"""Reporting windows (gold.resolve_windows) and fetch windows (etl.windows)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from safety.etl import gold, windows


def _by_name(anchor):
    return {w.name: w for w in gold.resolve_windows(anchor)}


def test_leap_day_anchor():
    anchor = date(2024, 2, 29)
    w = _by_name(anchor)
    # _shift_years maps 29 Feb to 28 Feb, then the window starts the day after.
    assert w["last_12m"].start == date(2023, 3, 1)
    assert w["last_24m"].start == date(2022, 3, 1)
    assert w["last_30d"].start == anchor - timedelta(days=29)
    assert w["last_90d"].start == anchor - timedelta(days=89)


def test_every_window_ends_on_the_anchor_and_they_nest():
    anchor = date(2025, 6, 30)
    built = gold.resolve_windows(anchor)
    assert all(w.end == anchor for w in built)
    starts = [w.start for w in built]
    assert all(later <= earlier for earlier, later in zip(starts, starts[1:]))


def test_months_before():
    assert windows.months_before(datetime(2024, 3, 15, tzinfo=timezone.utc), 14) == datetime(
        2023, 1, 1, tzinfo=timezone.utc
    )
    assert windows.months_before(datetime(2024, 1, 31, tzinfo=timezone.utc), 1) == datetime(
        2023, 12, 1, tzinfo=timezone.utc
    )
    assert windows.months_before(datetime(2024, 12, 5, tzinfo=timezone.utc), 12) == datetime(
        2023, 12, 1, tzinfo=timezone.utc
    )


class _FrozenDatetime(datetime):
    """datetime with a fixed now(); backfill_window reads datetime.now()."""

    @classmethod
    def now(cls, tz=None):
        return cls(2025, 5, 20, 12, 0, tzinfo=timezone.utc)


def test_backfill_window_pads_a_day_each_side(monkeypatch):
    monkeypatch.setattr(windows, "datetime", _FrozenDatetime)
    since, until = windows.backfill_window(6)
    assert until == datetime(2025, 5, 21, 12, 0, tzinfo=timezone.utc)
    # Six months before May 2025 is 1 Nov 2024, less the one-day pad.
    assert since == datetime(2024, 10, 31, tzinfo=timezone.utc)


def test_backfill_floor_clamps_since(monkeypatch):
    monkeypatch.setattr(windows, "datetime", _FrozenDatetime)
    config = SimpleNamespace(source_id="sea", backfill_start_date=date(2025, 1, 15))
    since, until = windows.backfill_window(24, config)
    assert since == datetime(2025, 1, 15, tzinfo=timezone.utc)
    assert until == datetime(2025, 5, 21, 12, 0, tzinfo=timezone.utc)


def test_backfill_floor_older_than_window_is_ignored(monkeypatch):
    monkeypatch.setattr(windows, "datetime", _FrozenDatetime)
    config = SimpleNamespace(source_id="sea", backfill_start_date=date(2019, 5, 1))
    since, _ = windows.backfill_window(6, config)
    assert since == datetime(2024, 10, 31, tzinfo=timezone.utc)
