"""The serving layer duplicates ETL constants on purpose, so it keeps no import
dependency on the ETL package (safety/api/repository.py, "Mirrors
safety.etl.gold..."). Nothing checked that the copies agree; these do."""

from __future__ import annotations

from datetime import date

import pytest

from safety.api import repository as repo
from safety.etl import gold


def test_hourly_scope_matches():
    assert repo.HOURLY_RESOLUTIONS == gold.HOURLY_RESOLUTIONS
    assert repo.HOURLY_WINDOWS == gold.HOURLY_WINDOWS


def test_safety_resolutions_match():
    assert repo.SAFETY_RESOLUTIONS == gold.SAFETY_RESOLUTIONS


def test_activity_narrowing_matches():
    assert repo.ACTIVITY_WINDOWS == gold.ACTIVITY_WINDOWS
    assert repo.ACTIVITY_CATEGORIES == gold.ACTIVITY_CATEGORIES


def test_window_pattern_and_aliases_match():
    assert repo.WINDOW_PATTERN.pattern == gold.WINDOW_PATTERN.pattern
    assert repo.LEGACY_WINDOWS == gold.LEGACY_WINDOWS


def test_every_window_gold_builds_is_accepted_and_labelled():
    for w in gold.resolve_windows(date(2025, 6, 30), date(1990, 1, 1)):
        assert repo.canonical_window(w.name) == w.name
        assert repo.window_label(w.name).startswith("Last ")


def test_default_and_hourly_window_are_built_for_a_two_year_city():
    built = {w.name for w in gold.resolve_windows(date(2025, 6, 30))}
    assert repo.DEFAULT_WINDOW in built
    assert set(repo.HOURLY_WINDOWS) <= built
    assert set(repo.ACTIVITY_WINDOWS[10]) <= built


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("last_3m", "Last 3 months"),
        ("last_1y", "Last 12 months"),
        ("last_2y", "Last 2 years"),
        ("last_12m", "Last 12 months"),
    ],
)
def test_window_labels(name, label):
    assert repo.window_label(name) == label


def test_aliases_resolve_both_ways():
    new = [{"id": n} for n in ("last_3m", "last_1y", "last_2y")]
    old = [{"id": n} for n in ("last_90d", "last_12m", "last_24m")]
    assert repo.match_window("last_12m", new)["id"] == "last_1y"
    assert repo.match_window("last_1y", old)["id"] == "last_12m"
    assert repo.match_window("last_5y", new) is None
    # The retired 30-day window: old links open on 3 months, in either list.
    assert repo.match_window("last_30d", new)["id"] == "last_3m"
    assert repo.canonical_window("last_30d") == "last_3m"


@pytest.mark.parametrize("res", [8, 9, 10])
def test_activity_scope_matches(res):
    assert repo.activity_scope(res) == gold.activity_scope(res)
