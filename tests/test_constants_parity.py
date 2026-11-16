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


def test_served_resolutions_are_the_built_ones():
    assert repo.VALID_RESOLUTIONS == gold.RESOLUTIONS


def test_the_one_window_matches():
    assert repo.DEFAULT_WINDOW == gold.WINDOW
    assert gold.WINDOW_PATTERN.match(repo.DEFAULT_WINDOW)
    assert repo.HOURLY_WINDOWS == (gold.WINDOW,)


@pytest.mark.parametrize("floor", [None, date(2024, 1, 1), date(2025, 3, 1)])
def test_gold_builds_exactly_the_served_window(floor):
    built = gold.resolve_windows(date(2025, 6, 30), floor)
    assert [w.name for w in built] == [repo.DEFAULT_WINDOW]
    assert repo.window_label(built[0].name) == "Last 12 months"


def test_match_window_finds_only_the_one_there_is():
    windows = [{"id": "last_1y"}]
    assert repo.match_window("last_1y", windows)["id"] == "last_1y"
    assert repo.match_window("last_2y", windows) is None
    assert repo.match_window("last_1y", []) is None
