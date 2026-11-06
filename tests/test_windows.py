"""The reporting window (gold.resolve_windows) and fetch windows (etl.windows)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from safety.etl import gold, windows


def _by_name(anchor, floor=None):
    return {w.name: w for w in gold.resolve_windows(anchor, floor)}


def _names(anchor, floor=None):
    return [w.name for w in gold.resolve_windows(anchor, floor)]


def test_one_window_the_last_year():
    (w,) = gold.resolve_windows(date(2025, 6, 30))
    assert w.name == "last_1y"
    assert (w.start, w.end) == (date(2024, 7, 1), date(2025, 6, 30))
    assert not w.partial and w.data_start == w.start


def test_leap_day_anchor():
    (w,) = gold.resolve_windows(date(2024, 2, 29))
    # _shift_years maps 29 Feb to 28 Feb, then the window starts the day after.
    assert w.start == date(2023, 3, 1)


def test_a_floor_inside_the_year_marks_it_partial():
    # A source that lags by weeks: its year reaches back past the retention cutoff.
    (w,) = gold.resolve_windows(date(2025, 6, 30), date(2024, 8, 15))
    assert w.partial and w.data_start == date(2024, 8, 15)
    assert w.start == date(2024, 7, 1)


def test_a_floor_before_the_year_changes_nothing():
    (w,) = gold.resolve_windows(date(2025, 6, 30), date(2001, 1, 1))
    assert not w.partial and w.data_start == date(2024, 7, 1)


def test_window_name_matches_the_pattern():
    (w,) = gold.resolve_windows(date(2026, 10, 7), date(1930, 1, 1))
    assert gold.WINDOW_PATTERN.match(w.name)
    assert not gold.WINDOW_PATTERN.match("last_2y")


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


def test_backfill_never_reaches_past_the_retention_cutoff(monkeypatch):
    monkeypatch.setattr(windows, "datetime", _FrozenDatetime)
    since, _ = windows.backfill_window(24)
    # 20 May 2025, less 365 days, less the one-day pad.
    assert since == datetime(2024, 5, 19, tzinfo=timezone.utc)


def _bucket_dates(anchor, floor):
    windows = gold.resolve_windows(anchor, floor)
    rows = gold.period_rows(anchor, min(w.start for w in windows))
    return windows, rows


def _check_buckets(anchor, floor):
    windows, rows = _bucket_dates(anchor, floor)
    for w in windows:
        limit = gold.window_bucket_limit(w.name)
        picked = {d for d, bucket in rows if bucket < limit}
        expected = {w.start + timedelta(days=i) for i in range((w.end - w.start).days + 1)}
        assert picked == expected, (anchor, w.name)


def test_the_window_is_exactly_its_buckets_on_every_kind_of_anchor():
    # Month ends, 29 February, the 30th and 31st, and ordinary days: the
    # pre-aggregated gold path sums buckets < limit instead of filtering silver
    # by BETWEEN start AND end, so the two have to select the same dates.
    anchors = [
        date(2024, 2, 29), date(2025, 2, 28), date(2025, 3, 1), date(2025, 3, 30),
        date(2025, 3, 31), date(2025, 5, 31), date(2025, 6, 30), date(2025, 12, 31),
        date(2026, 1, 1), date(2026, 10, 7), date(2028, 2, 29),
    ]
    for anchor in anchors:
        _check_buckets(anchor, None)


def test_every_day_of_a_leap_year_as_anchor():
    start = date(2024, 1, 1)
    for i in range(366):
        _check_buckets(start + timedelta(days=i), None)


def test_bucket_limit():
    assert gold.window_bucket_limit("last_1y") == 12
