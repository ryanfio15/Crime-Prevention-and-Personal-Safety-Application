"""LAPD's legacy crime codes in the Los Angeles crosswalk (migration 019)."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from safety.etl.adapters import HISTORY_DATASETS
from safety.etl.adapters.los_angeles import LEGACY_CODE_PREFIX

CROSSWALK = Path(__file__).parent.parent / "reference" / "crosswalk" / "los_angeles_v1.csv"


def _rows():
    with CROSSWALK.open() as fh:
        return list(csv.DictReader(fh))


def test_legacy_codes_cannot_match_a_nibrs_row():
    rows = _rows()
    legacy = [r for r in rows if r["raw_offense_code"].startswith(LEGACY_CODE_PREFIX)]
    nibrs = [r for r in rows if not r["raw_offense_code"].startswith(LEGACY_CODE_PREFIX)]
    assert len(legacy) == 143
    assert not {r["raw_offense_code"] for r in legacy} & {r["raw_offense_code"] for r in nibrs}


def test_colliding_numbers_map_by_their_own_scheme():
    by_code = {
        (r["raw_offense_code"], r["effective_from"]): r["nibrs_code"] for r in _rows()
    }
    assert by_code[("CRM-510", "2010-01-01")] == "240"  # stolen vehicle
    assert by_code[("510", "2024-03-01")] == "510"  # bribery
    assert by_code[("CRM-220", "2010-01-01")] == "120"  # attempted robbery
    assert by_code[("220", "2024-03-01")] == "220"  # burglary


def test_legacy_rows_cover_every_legacy_dataset_year():
    oldest = min(start for _, start, _ in HISTORY_DATASETS["lax"])
    legacy = [r for r in _rows() if r["raw_offense_code"].startswith(LEGACY_CODE_PREFIX)]
    assert {r["effective_from"] for r in legacy} == {oldest.isoformat()}
    assert {r["effective_to"] for r in legacy} == {""}


def test_nibrs_rows_reach_back_to_the_nibrs_history_start():
    from safety.etl.adapters.los_angeles import NIBRS_HISTORY_START

    starts = {
        date.fromisoformat(r["effective_from"])
        for r in _rows()
        if r["raw_offense_code"] == "13A"
    }
    assert min(starts) <= NIBRS_HISTORY_START
