"""One rolling year per city (safety/etl/retention.py): the cutoff, the silver
and bronze prunes, and the promote-time filter.

The DB tests use source `chi`, which no other DB test module touches, and
clean up after themselves."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from safety import PIPELINE_VERSION
from safety.db import ensure_partitions
from safety.etl import pending, retention, windows
from safety.h3grid import cells_for_point

UTC = timezone.utc


# ---------------------------------------------------------------- the cutoff


def test_cutoff_keeps_today_and_the_365_days_before_it():
    now = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    assert retention.cutoff("UTC", now) == date(2025, 11, 6)
    # 366 days old is the first day deleted; 365 is kept.
    assert (date(2026, 11, 6) - retention.cutoff("UTC", now)).days == 365


def test_cutoff_follows_the_city_clock():
    # 02:00 UTC on the 6th is still the 5th in Los Angeles.
    now = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    assert retention.cutoff("America/Los_Angeles", now) == date(2025, 11, 5)
    assert retention.cutoff("America/New_York", now) == date(2025, 11, 5)
    assert retention.cutoff("UTC", now) == date(2025, 11, 6)


def test_unknown_zone_falls_back_to_utc():
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert retention.cutoff("Not/AZone", now) == retention.cutoff("UTC", now)
    assert retention.cutoff(None, now) == retention.cutoff("UTC", now)


def test_cutoff_over_a_leap_day():
    now = datetime(2028, 2, 29, 12, 0, tzinfo=UTC)
    assert retention.cutoff("UTC", now) == date(2027, 3, 1)


def test_retention_days_is_a_setting(monkeypatch):
    monkeypatch.setattr(retention.settings, "retention_days", 30)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    assert retention.cutoff("UTC", now) == date(2026, 10, 7)


def test_fetch_floor_is_the_cutoff_less_a_day():
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    config = SimpleNamespace(timezone="UTC")
    assert windows.retention_floor(config, now) == datetime(2025, 11, 5, tzinfo=UTC)


# ---------------------------------------------------------------- bronze


def _pull_dir(root, city, dataset, day):
    path = root / f"source_id={city}" / f"dataset={dataset}" / f"pull_date={day}" / "pull_000001"
    path.mkdir(parents=True)
    (path / "2025-01.csv.gz").write_bytes(b"x")
    return path.parent


def test_bronze_pulls_before_the_cutoff_are_removed(tmp_path):
    keep_from = date(2025, 11, 6)
    old = _pull_dir(tmp_path, "phl", "incidents", "2025-11-05")
    older = _pull_dir(tmp_path, "phl", "tabblock20", "2024-01-01")
    kept = _pull_dir(tmp_path, "phl", "incidents", "2025-11-06")
    other_city = _pull_dir(tmp_path, "chi", "incidents", "2020-01-01")
    removed = retention.prune_bronze("phl", keep_from, root=tmp_path)
    assert sorted(removed) == sorted([old, older])
    assert not old.exists() and not older.exists()
    assert kept.exists()
    assert other_city.exists()


def test_bronze_prune_ignores_what_it_does_not_recognise(tmp_path):
    stray = tmp_path / "source_id=phl" / "dataset=incidents" / "notes"
    stray.mkdir(parents=True)
    bad = tmp_path / "source_id=phl" / "dataset=incidents" / "pull_date=2020-13-45"
    bad.mkdir()
    assert retention.prune_bronze("phl", date(2025, 1, 1), root=tmp_path) == []
    assert stray.exists() and bad.exists()
    assert retention.prune_bronze("nowhere", date(2025, 1, 1), root=tmp_path) == []


# ---------------------------------------------------------------- silver (DB)

SOURCE = "chi"
DATASET = "test-retention"


def _pull(conn) -> int:
    return conn.execute(
        """
        INSERT INTO etl.pull_run (source_id, dataset, mode, status, pipeline_version)
        VALUES (%s, %s, 'backfill', 'succeeded', %s) RETURNING pull_id
        """,
        (SOURCE, DATASET, PIPELINE_VERSION),
    ).fetchone()["pull_id"]


def _insert(conn, pull_id, key, local_date):
    lat, lng = 41.88, -87.63
    cells = cells_for_point(lat, lng)
    at = datetime(local_date.year, local_date.month, local_date.day, 12, tzinfo=UTC)
    conn.execute(
        """
        INSERT INTO silver.incident (
            source_id, occurred_year, incident_key, source_incident_id, source_dataset,
            source_pull_id, occurred_at, occurred_local_date, occurred_precision,
            occurred_basis, latitude, longitude, geom, h3_r8, h3_r9, h3_r10,
            raw_offense_code, raw_offense_text, severity_bucket, product_category,
            mapping_confidence, crosswalk_version, pipeline_version, occurred_local_hour
        ) VALUES (
            %(sid)s, %(year)s, %(key)s, %(id)s, %(dataset)s, %(pull)s, %(at)s, %(date)s,
            'exact', 'occurrence', %(lat)s, %(lng)s,
            ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
            %(r8)s, %(r9)s, %(r10)s, 'X', 'test offense', 'low', 'property',
            'exact', 'chi_v1', %(pipeline)s, 12
        )
        """,
        {
            "sid": SOURCE, "year": local_date.year, "key": f"{SOURCE}:{key}", "id": key,
            "dataset": DATASET, "pull": pull_id, "at": at, "date": local_date,
            "lat": lat, "lng": lng, "r8": cells[8], "r9": cells[9], "r10": cells[10],
            "pipeline": PIPELINE_VERSION,
        },
    )


def _keys(conn):
    return {
        r["source_incident_id"]
        for r in conn.execute(
            "SELECT source_incident_id FROM silver.incident WHERE source_id = %s", (SOURCE,)
        ).fetchall()
    }


def _partitions(conn):
    return {name for name, _ in retention._year_partitions(conn, SOURCE)}


@pytest.fixture
def chi(db_conn):
    conn = db_conn
    conn.rollback()
    yield conn
    conn.rollback()
    # Tests commit (DROP TABLE is not something to leave half-done): clear up.
    conn.execute("DELETE FROM silver.incident WHERE source_id = %s", (SOURCE,))
    conn.execute("DELETE FROM etl.pull_run WHERE source_id = %s AND dataset = %s", (SOURCE, DATASET))
    conn.execute("DELETE FROM etl.gold_pending WHERE source_id = %s", (SOURCE,))
    conn.commit()


@pytest.mark.db
def test_prune_drops_old_years_and_trims_the_cutoff_year(chi):
    conn = chi
    keep_from = date(2025, 11, 6)
    ensure_partitions(conn, SOURCE, [2023, 2024, 2025, 2026])
    pull_id = _pull(conn)
    _insert(conn, pull_id, "y2023", date(2023, 6, 1))
    _insert(conn, pull_id, "y2024", date(2024, 12, 31))
    _insert(conn, pull_id, "old2025", date(2025, 11, 5))
    _insert(conn, pull_id, "edge2025", date(2025, 11, 6))
    _insert(conn, pull_id, "y2026", date(2026, 3, 1))
    conn.execute("DELETE FROM etl.gold_pending WHERE source_id = %s", (SOURCE,))
    conn.commit()

    outcome = retention.prune(conn, SOURCE, keep_from)
    conn.commit()

    assert _keys(conn) == {"edge2025", "y2026"}
    assert outcome["rows_deleted"] == 1
    assert sorted(outcome["partitions_dropped"]) == ["incident_chi_2023", "incident_chi_2024"]
    assert _partitions(conn) == {"incident_chi_2025", "incident_chi_2026"}
    # Gold has to be rebuilt in full over the shorter history.
    assert pending.get(conn, SOURCE).full


@pytest.mark.db
def test_prune_with_nothing_old_changes_nothing(chi):
    conn = chi
    ensure_partitions(conn, SOURCE, [2026])
    pull_id = _pull(conn)
    _insert(conn, pull_id, "recent", date(2026, 3, 1))
    conn.execute("DELETE FROM etl.gold_pending WHERE source_id = %s", (SOURCE,))
    conn.commit()
    outcome = retention.prune(conn, SOURCE, date(2025, 11, 6))
    conn.commit()
    assert outcome["rows_deleted"] == 0 and outcome["partitions_dropped"] == []
    assert _keys(conn) == {"recent"}
    assert pending.get(conn, SOURCE) is None


@pytest.mark.db
def test_promote_skips_records_older_than_the_cutoff(chi, monkeypatch):
    from safety.etl import transform

    conn = chi
    keep_from = date(2025, 11, 6)
    monkeypatch.setattr(retention, "city_cutoff", lambda conn, source_id, now=None: keep_from)
    pull_id = _pull(conn)
    conn.commit()
    cells = cells_for_point(41.88, -87.63)
    for key, day in (("cold", date(1999, 5, 1)), ("old", date(2025, 11, 5)), ("new", date(2026, 1, 2))):
        conn.execute(
            """
            INSERT INTO etl.staging_incident (
                pull_id, source_id, source_dataset, incident_key, source_incident_id,
                occurred_at, occurred_local_date, occurred_precision, occurred_basis,
                latitude, longitude, coordinate_source, h3_r8, h3_r9, h3_r10,
                raw_offense_code, raw_offense_text, occurred_local_hour,
                location_type, location_block, district
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'exact', 'occurrence', 41.88, -87.63,
                      'published_wgs84', %s, %s, %s, 'X', 'test', 12, 'STREET', '100 W TEST', '1')
            """,
            (
                pull_id, SOURCE, DATASET, f"{SOURCE}:{key}", key,
                datetime(day.year, day.month, day.day, 12, tzinfo=UTC), day,
                cells[8], cells[9], cells[10],
            ),
        )
    transform.promote_to_silver(
        conn,
        pull_id=pull_id,
        source_id=SOURCE,
        crosswalk_version="chi_v1",
        pipeline_version=PIPELINE_VERSION,
    )
    years = {
        r["y"]
        for r in conn.execute(
            "SELECT DISTINCT occurred_year AS y FROM silver.incident WHERE source_id = %s",
            (SOURCE,),
        ).fetchall()
    }
    assert years == {2026}
    assert _keys(conn) == {"new"}
    conn.execute("DELETE FROM etl.staging_incident WHERE pull_id = %s", (pull_id,))
    conn.commit()
