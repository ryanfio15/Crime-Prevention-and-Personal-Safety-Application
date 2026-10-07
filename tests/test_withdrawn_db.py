"""Records withdrawn upstream (F13) against a real PostGIS: delete, guard,
report, restore, basis, dataset filter and retention.

Each test runs in one transaction on source `sea` and ends in rollback, so
nothing persists and no other module is affected. Runs in CI as safety_app,
after migration 017 was applied as safety_app."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from safety import PIPELINE_VERSION
from safety.db import ensure_partitions
from safety.etl import withdrawn
from safety.h3grid import cells_for_point

pytestmark = pytest.mark.db

SOURCE = "sea"
DATASET = "test-withdrawn"
SINCE = datetime(2025, 3, 1, tzinfo=timezone.utc)
UNTIL = datetime(2025, 4, 1, tzinfo=timezone.utc)
NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)  # domain: 2025-03-02 .. 2025-03-31


class _Adapter:
    def __init__(self, basis="occurred", stratum=None):
        self.reconcile_basis = basis
        self.reconcile_stratum = stratum


def _pull(conn) -> int:
    return conn.execute(
        """
        INSERT INTO etl.pull_run (source_id, dataset, mode, status, pipeline_version)
        VALUES (%s, %s, 'incremental', 'succeeded', %s) RETURNING pull_id
        """,
        (SOURCE, DATASET, PIPELINE_VERSION),
    ).fetchone()["pull_id"]


def _insert(conn, pull_id, key_suffix, local_date, *, dataset=DATASET, reported=None):
    lat, lng = 47.61 + (hash(key_suffix) % 100) / 10000, -122.33
    cells = cells_for_point(lat, lng)
    at = datetime(local_date.year, local_date.month, local_date.day, 12, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO silver.incident (
            source_id, occurred_year, incident_key, source_incident_id, source_dataset,
            source_pull_id, occurred_at, occurred_local_date, occurred_precision,
            occurred_basis, reported_at, latitude, longitude, geom, h3_r8, h3_r9, h3_r10,
            raw_offense_code, raw_offense_text, severity_bucket, product_category,
            mapping_confidence, crosswalk_version, pipeline_version, occurred_local_hour
        ) VALUES (
            %(sid)s, %(year)s, %(key)s, %(id)s, %(dataset)s, %(pull)s, %(at)s, %(date)s, 'exact',
            'occurrence', %(reported)s, %(lat)s, %(lng)s, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
            %(r8)s, %(r9)s, %(r10)s, 'X', 'test offense', 'low', 'property',
            'exact', 'sea_v1', %(pipeline)s, 12
        )
        """,
        {
            "sid": SOURCE, "year": local_date.year, "key": f"{SOURCE}:{key_suffix}", "id": key_suffix,
            "dataset": dataset, "pull": pull_id, "at": at, "date": local_date,
            "reported": at if reported is None else reported, "lat": lat, "lng": lng,
            "r8": cells[8], "r9": cells[9], "r10": cells[10], "pipeline": PIPELINE_VERSION,
        },
    )
    return key_suffix


def _setup(conn, n_in=100, n_out=5):
    ensure_partitions(conn, SOURCE, [2025])
    conn.execute("DELETE FROM silver.incident WHERE source_id = %s AND source_dataset = %s", (SOURCE, DATASET))
    pull_id = _pull(conn)
    inside = [_insert(conn, pull_id, f"w{i}", date(2025, 3, 5) + timedelta(days=i % 20)) for i in range(n_in)]
    outside = [_insert(conn, pull_id, f"o{i}", date(2025, 2, 10)) for i in range(n_out)]
    return pull_id, inside, outside


def _count(conn, sql, params=()):
    return next(iter(conn.execute(sql, params).fetchone().values()))


def _assess(conn, seen, adapter=None, since=SINCE):
    return withdrawn.assess(conn, adapter or _Adapter(), SOURCE, DATASET, set(seen), since, UNTIL, NOW)


def _issue(conn, pull_id):
    return conn.execute(
        "SELECT check_name, severity, occurrences, detail FROM etl.validation_issue "
        "WHERE pull_id = %s AND check_name = 'withdrawn_upstream'",
        (pull_id,),
    ).fetchall()


def test_delete_archives_and_records(db_conn):
    try:
        pull_id, inside, outside = _setup(db_conn)
        a = _assess(db_conn, inside[5:])
        assert (a.prior, len(a.absent_keys), a.decision) == (100, 5, "delete")
        out = withdrawn.apply(db_conn, pull_id, a, "delete")
        assert out["deleted"] == 5
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE source_id=%s AND source_dataset=%s",
                      (SOURCE, DATASET)) == 100
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE incident_key = ANY(%s)",
                      ([f"{SOURCE}:{k}" for k in outside],)) == 5
        arch = db_conn.execute(
            "SELECT row ? 'incident_key' AS has_key, row ? 'geom' AS has_geom FROM etl.withdrawn_incident WHERE pull_id=%s",
            (pull_id,),
        ).fetchall()
        assert len(arch) == 5 and all(r["has_key"] and not r["has_geom"] for r in arch)
        (issue,) = _issue(db_conn, pull_id)
        assert (issue["severity"], issue["occurrences"], issue["detail"]["action"]) == ("warn", 5, "deleted")
    finally:
        db_conn.rollback()


def test_guard_skips_a_partial_pull(db_conn):
    try:
        pull_id, inside, _ = _setup(db_conn)
        a = _assess(db_conn, inside[20:])
        assert a.decision == "skip" and "80%" in a.reason
        assert withdrawn.apply(db_conn, pull_id, a, "delete")["deleted"] == 0
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE source_id=%s AND source_dataset=%s",
                      (SOURCE, DATASET)) == 105
        (issue,) = _issue(db_conn, pull_id)
        assert issue["detail"]["action"] == "skipped" and "80%" in issue["detail"]["reason"]
    finally:
        db_conn.rollback()


def test_report_mode_deletes_nothing(db_conn):
    try:
        pull_id, inside, _ = _setup(db_conn)
        out = withdrawn.apply(db_conn, pull_id, _assess(db_conn, inside[5:]), "report")
        assert (out["action"], out["deleted"]) == ("report", 0)
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE source_id=%s AND source_dataset=%s",
                      (SOURCE, DATASET)) == 105
        assert _issue(db_conn, pull_id)[0]["detail"]["action"] == "report"
    finally:
        db_conn.rollback()


def test_a_vanished_month_skips(db_conn):
    try:
        # Window widened to February: the 5 February rows form a stratum that came back empty.
        pull_id, inside, outside = _setup(db_conn)
        a = _assess(db_conn, inside, since=datetime(2025, 2, 1, tzinfo=timezone.utc))
        assert a.prior == 105 and a.decision == "skip" and "2025-02" in a.reason
        assert withdrawn.apply(db_conn, pull_id, a, "delete")["action"] == "skipped"
    finally:
        db_conn.rollback()


def test_restore_round_trip(db_conn):
    try:
        pull_id, inside, _ = _setup(db_conn)
        gone = [f"{SOURCE}:{k}" for k in inside[:5]]
        snapshot = """
            SELECT incident_key, to_jsonb(i) - 'geom' - 'ingested_at' AS j, ST_AsEWKB(geom) AS g
            FROM silver.incident i WHERE incident_key = ANY(%s) ORDER BY incident_key
        """
        before = db_conn.execute(snapshot, (gone,)).fetchall()
        withdrawn.apply(db_conn, pull_id, _assess(db_conn, inside[5:]), "delete")
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE incident_key = ANY(%s)", (gone,)) == 0
        # The documented procedure: partitions for the archived years first.
        years = [r["y"] for r in db_conn.execute(
            "SELECT DISTINCT extract(year FROM occurred_local_date)::int AS y FROM etl.withdrawn_incident WHERE pull_id=%s",
            (pull_id,))]
        ensure_partitions(db_conn, SOURCE, years)
        db_conn.execute(withdrawn.RESTORE_SQL, {"pull_id": pull_id})
        after = db_conn.execute(snapshot, (gone,)).fetchall()
        assert [(r["incident_key"], r["j"]) for r in after] == [(r["incident_key"], r["j"]) for r in before]
        for b, a in zip(before, after):
            assert _count(db_conn, "SELECT ST_Equals(ST_GeomFromEWKB(%s), ST_GeomFromEWKB(%s))", (b["g"], a["g"]))
    finally:
        db_conn.rollback()


def test_reported_basis_ignores_rows_reported_before_the_window(db_conn):
    try:
        pull_id, inside, _ = _setup(db_conn)
        # Occurred inside the window, reported before it: DC's pull (REPORT_DAT) would not return it.
        late = _insert(db_conn, pull_id, "late", date(2025, 3, 10),
                       reported=datetime(2025, 2, 20, 12, tzinfo=timezone.utc))
        a = _assess(db_conn, inside, adapter=_Adapter(basis="reported"))
        assert a.prior == 100 and a.decision == "none"
        withdrawn.apply(db_conn, pull_id, a, "delete")
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE incident_key=%s", (f"{SOURCE}:{late}",)) == 1
    finally:
        db_conn.rollback()


def test_rows_of_another_dataset_are_never_counted(db_conn):
    try:
        pull_id, inside, _ = _setup(db_conn)
        other = _insert(db_conn, pull_id, "otherds", date(2025, 3, 10), dataset="previous-dataset-id")
        a = _assess(db_conn, inside[5:])
        assert a.prior == 100 and f"{SOURCE}:{other}" not in a.absent_keys
        assert withdrawn.apply(db_conn, pull_id, a, "delete")["deleted"] == 5
        assert _count(db_conn, "SELECT count(*) FROM silver.incident WHERE incident_key=%s", (f"{SOURCE}:{other}",)) == 1
    finally:
        db_conn.rollback()


def test_prune_keeps_ninety_days(db_conn):
    try:
        pull_id = _pull(db_conn)
        for key, source, age in (("sea:old", "sea", 91), ("sea:new", "sea", 89), ("phl:old", "phl", 91)):
            db_conn.execute(
                "INSERT INTO etl.withdrawn_incident (pull_id, source_id, incident_key, occurred_local_date, withdrawn_at, row) "
                "VALUES (%s, %s, %s, '2025-03-05', now() - make_interval(days => %s), '{}')",
                (pull_id, source, key, age),
            )
        assert withdrawn.prune(db_conn, "sea", 90) == 1
        left = {r["incident_key"] for r in db_conn.execute(
            "SELECT incident_key FROM etl.withdrawn_incident WHERE pull_id=%s", (pull_id,))}
        assert left == {"sea:new", "phl:old"}
    finally:
        db_conn.rollback()
