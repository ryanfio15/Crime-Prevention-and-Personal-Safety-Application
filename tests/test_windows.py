"""Reporting windows (gold.resolve_windows) and fetch windows (etl.windows)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from safety.etl import gold, windows


def _by_name(anchor, floor=None):
    return {w.name: w for w in gold.resolve_windows(anchor, floor)}


def _names(anchor, floor=None):
    return [w.name for w in gold.resolve_windows(anchor, floor)]


def test_leap_day_anchor():
    anchor = date(2024, 2, 29)
    w = _by_name(anchor)
    # _shift_years maps 29 Feb to 28 Feb, then the window starts the day after.
    assert w["last_1y"].start == date(2023, 3, 1)
    assert w["last_2y"].start == date(2022, 3, 1)
    assert w["last_30d"].start == anchor - timedelta(days=29)
    assert w["last_3m"].start == date(2023, 11, 30)


def test_month_windows_clamp_to_short_months():
    # 31 May less three months is "31 February", clamped to the 28th, and the
    # window starts the day after.
    w = _by_name(date(2025, 5, 31))
    assert w["last_3m"].start == date(2025, 3, 1)
    assert w["last_6m"].start == date(2024, 12, 1)
    assert w["last_9m"].start == date(2024, 9, 1)


def test_one_and_two_years_match_the_old_12_and_24_months():
    anchor = date(2025, 6, 30)
    w = _by_name(anchor)
    assert w["last_1y"].start == date(2024, 7, 1)
    assert w["last_2y"].start == date(2023, 7, 1)


def test_no_floor_gives_the_two_year_list():
    assert _names(date(2025, 6, 30)) == [
        "last_30d", "last_3m", "last_6m", "last_9m", "last_1y", "last_2y"
    ]


def test_every_window_ends_on_the_anchor_and_they_nest():
    anchor = date(2025, 6, 30)
    built = gold.resolve_windows(anchor, date(2001, 1, 1))
    assert all(w.end == anchor for w in built)
    starts = [w.start for w in built]
    assert all(later < earlier for earlier, later in zip(starts, starts[1:]))


def test_years_run_back_to_the_floor():
    anchor = date(2026, 10, 7)
    names = _names(anchor, date(2001, 1, 1))
    assert names[:6] == ["last_30d", "last_3m", "last_6m", "last_9m", "last_1y", "last_2y"]
    assert names[-1] == "last_26y"
    oldest = gold.resolve_windows(anchor, date(2001, 1, 1))[-1]
    assert oldest.partial and oldest.data_start == date(2001, 1, 1)
    assert all(not w.partial for w in gold.resolve_windows(anchor, date(2001, 1, 1))[:-1])


def test_a_sliver_past_the_last_full_year_adds_no_window():
    # A 24-month backfill starts on the first of a month, a week before
    # "last 2 years" begins; that week is not worth a "last 3 years".
    assert _names(date(2026, 10, 7), date(2024, 10, 1))[-1] == "last_2y"
    # Austin's services begin on 2021-09-23: two weeks short of a sixth year.
    assert _names(date(2026, 10, 7), date(2021, 9, 23))[-1] == "last_5y"


def test_a_real_partial_year_is_offered():
    w = gold.resolve_windows(date(2026, 10, 7), date(2024, 1, 1))
    assert w[-1].name == "last_3y" and w[-1].partial
    assert w[-1].data_start == date(2024, 1, 1)


def test_short_history_stops_early_and_marks_partial():
    w = gold.resolve_windows(date(2026, 10, 7), date(2026, 8, 20))
    assert [x.name for x in w] == ["last_30d", "last_3m"]
    assert w[1].partial and w[1].data_start == date(2026, 8, 20)
    # Less than thirty days of data: only the 30-day window.
    assert _names(date(2026, 10, 7), date(2026, 9, 20)) == ["last_30d"]


def test_window_names_match_the_pattern():
    for w in gold.resolve_windows(date(2026, 10, 7), date(1930, 1, 1)):
        assert gold.WINDOW_PATTERN.match(w.name), w.name
    assert len(gold.resolve_windows(date(2026, 10, 7), date(1900, 1, 1))) == 4 + gold.MAX_WINDOW_YEARS


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
