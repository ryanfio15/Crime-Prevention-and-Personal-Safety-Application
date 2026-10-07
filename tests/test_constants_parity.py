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


def test_valid_windows_are_the_windows_gold_builds():
    built = tuple(w.name for w in gold.resolve_windows(date(2025, 6, 30)))
    assert repo.VALID_WINDOWS == built


@pytest.mark.parametrize("res", [8, 9, 10])
def test_activity_scope_matches(res):
    assert repo.activity_scope(res) == gold.activity_scope(res)
