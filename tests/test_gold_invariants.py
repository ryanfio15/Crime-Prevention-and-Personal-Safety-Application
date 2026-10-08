"""Gold-layer invariants against a real PostGIS, on a synthetic city (F5).

Runs in CI only (SAFETY_TEST_DB=1), as the non-superuser app role the CI job
switches to after transferring ownership -- so it also proves partition
creation, temp tables and the gold DELETE/INSERT work for an owner that is not
a superuser.

The fixture replaces phl's boundary with a ~3 km x 3 km square, loads ~400
deterministic incidents into it, and runs the real `gold.refresh_all`. Each test
checks one property the serving layer and the methodology text rely on; the
comment beside each cites the SQL in safety/etl/gold.py that defines it.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

from safety import PIPELINE_VERSION
from safety.db import ensure_partitions
from safety.etl import gold
from safety.h3grid import cells_for_point

pytestmark = pytest.mark.db

SOURCE = "phl"
ANCHOR = date(2025, 6, 30)
SPAN_DAYS = 800
N_ROWS = 400
BOX = (-75.19, 39.94, -75.15, 39.97)  # west, south, east, north
CATEGORIES = ("violent", "property", "quality_of_life", "other")
AREA_SCHEME = "nscs_v1"


def _crosswalk_rows(conn) -> dict[str, list[dict]]:
    """Real phl crosswalk rows per product category, so the safety ranking
    resolves real severity weights."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.raw_offense_code, c.raw_offense_text, c.nibrs_code,
                   c.severity_bucket, c.product_category, c.mapping_confidence,
                   c.crosswalk_version
            FROM reference.offense_crosswalk c
            JOIN reference.source_registry r
              ON r.source_id = c.source_id AND r.crosswalk_version = c.crosswalk_version
            WHERE c.source_id = %s
            ORDER BY c.raw_offense_code, c.raw_offense_text
            """,
            (SOURCE,),
        )
        rows = cur.fetchall()
    by_category: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for row in rows:
        by_category[row["product_category"]].append(row)
    missing = [c for c, v in by_category.items() if not v]
    assert not missing, f"phl crosswalk has no rows for {missing}"
    return by_category


def _synthetic_rows(crosswalk: dict[str, list[dict]]) -> list[dict]:
    rng = random.Random(20261007)
    west, south, east, north = BOX
    rows = []
    for i in range(N_ROWS):
        # Row 0 sits exactly on the anchor, so gold.data_anchor() (max date) is
        # ANCHOR; no row is later than it.
        local_date = ANCHOR - timedelta(days=0 if i == 0 else rng.randrange(SPAN_DAYS))
        lat = rng.uniform(south + 0.001, north - 0.001)
        lng = rng.uniform(west + 0.001, east - 0.001)
        category = CATEGORIES[i % 4]
        xw = crosswalk[category][rng.randrange(len(crosswalk[category]))]
        hour = rng.randrange(24) if rng.random() < 0.8 else None
        cells = cells_for_point(lat, lng)
        rows.append(
            {
                "key": f"{SOURCE}:test-{i}",
                "id": f"test-{i}",
                "date": local_date,
                "year": local_date.year,
                "at": datetime(
                    local_date.year, local_date.month, local_date.day, 12 if hour is None else hour,
                    tzinfo=timezone.utc,
                ),
                "precision": "exact" if hour is not None else "date",
                "hour": hour,
                "lat": lat,
                "lng": lng,
                "r8": cells[8],
                "r9": cells[9],
                "r10": cells[10],
                "xw": xw,
            }
        )
    return rows


@pytest.fixture(scope="module")
def built(db_conn):
    conn = db_conn
    crosswalk = _crosswalk_rows(conn)
    rows = _synthetic_rows(crosswalk)

    with conn.cursor() as cur:
        # Re-runnable: clear what a previous run left (the CI DB is throwaway).
        cur.execute("DELETE FROM silver.incident WHERE source_id = %s", (SOURCE,))
        cur.execute("DELETE FROM reference.city_boundary WHERE source_id = %s", (SOURCE,))
        cur.execute(
            """
            INSERT INTO reference.city_boundary (source_id, boundary_kind, geom, area_km2, source_note)
            SELECT %s, 'city_limits', g, ST_Area(g::geography) / 1e6, 'test fixture'
            FROM (SELECT ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4326)) AS g) b
            """,
            (SOURCE, *BOX),
        )
        # silver.incident.source_pull_id is NOT NULL (004_silver.sql).
        cur.execute(
            """
            INSERT INTO etl.pull_run (source_id, dataset, mode, status, pipeline_version)
            VALUES (%s, 'test-fixture', 'backfill', 'succeeded', %s)
            RETURNING pull_id
            """,
            (SOURCE, PIPELINE_VERSION),
        )
        pull_id = cur.fetchone()["pull_id"]

    ensure_partitions(conn, SOURCE, {r["year"] for r in rows})

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO silver.incident (
                source_id, occurred_year, incident_key, source_incident_id, source_dataset,
                source_pull_id, occurred_at, occurred_local_date, occurred_precision,
                occurred_basis, latitude, longitude, geom, h3_r8, h3_r9, h3_r10,
                raw_offense_code, raw_offense_text, nibrs_code, severity_bucket,
                product_category, mapping_confidence, crosswalk_version, pipeline_version,
                occurred_local_hour
            ) VALUES (
                %(source_id)s, %(year)s, %(key)s, %(id)s, 'test-fixture',
                %(pull_id)s, %(at)s, %(date)s, %(precision)s,
                'dispatch', %(lat)s, %(lng)s, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                %(r8)s, %(r9)s, %(r10)s,
                %(code)s, %(text)s, %(nibrs)s, %(bucket)s,
                %(category)s, %(confidence)s, %(crosswalk)s, %(pipeline)s,
                %(hour)s
            )
            """,
            [
                {
                    **r,
                    "source_id": SOURCE,
                    "pull_id": pull_id,
                    "code": r["xw"]["raw_offense_code"],
                    "text": r["xw"]["raw_offense_text"],
                    "nibrs": r["xw"]["nibrs_code"],
                    "bucket": r["xw"]["severity_bucket"],
                    "category": r["xw"]["product_category"],
                    "confidence": r["xw"]["mapping_confidence"],
                    "crosswalk": r["xw"]["crosswalk_version"],
                    "pipeline": PIPELINE_VERSION,
                }
                for r in rows
            ],
        )
    conn.commit()

    assert gold.data_anchor(conn, SOURCE) == ANCHOR
    result = gold.refresh_all(conn, SOURCE, PIPELINE_VERSION, include_hourly=True)
    windows = gold.city_windows(conn, SOURCE, ANCHOR)
    assert [w.name for w in windows] == result["windows"]
    # The only *enabled* scheme is per-capita (reference/severity/schemes.csv),
    # and with no census blocks loaded refresh_all logs it and skips it, so
    # nothing above writes gold.cell_safety. Build the area-denominated scheme
    # explicitly so the safety ranking's invariants are exercised too.
    safety_rows, _ = gold.refresh_safety_layer(conn, SOURCE, windows, scheme_version=AREA_SCHEME)
    gold.write_legacy_windows(conn, SOURCE, ("cell_safety",))
    conn.commit()
    assert safety_rows > 0
    return {"conn": conn, "rows": rows, "windows": windows, "result": result}


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.rollback()
    return next(iter(row.values()))


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        out = cur.fetchall()
    conn.rollback()
    return out


def test_activity_tier_range_and_zero_tier(built):
    # gold.py _ACTIVITY_SQL: tier CASE -- 0 exactly when n = 0, else 1..5 from pr.
    bad = _scalar(
        built["conn"],
        """
        SELECT count(*) FROM gold.cell_activity
        WHERE source_id = %s
          AND (activity_tier NOT BETWEEN 0 AND 5
               OR (activity_tier = 0) <> (incident_count = 0))
        """,
        (SOURCE,),
    )
    assert bad == 0
    assert _scalar(built["conn"], "SELECT count(*) FROM gold.cell_activity WHERE source_id = %s", (SOURCE,)) > 0


def test_city_cell_total_is_the_layer_size_and_the_universe(built):
    # gold.py _ACTIVITY_SQL: cell_total = count(*) OVER (PARTITION BY category)
    # over `universe` (gold.cell_geometry for the resolution), LEFT JOINed to counts.
    bad = _rows(
        built["conn"],
        """
        WITH layer AS (
            SELECT h3_res, time_window, category,
                   min(city_cell_total) AS lo, max(city_cell_total) AS hi, count(*) AS n
            FROM gold.cell_activity WHERE source_id = %(s)s
            GROUP BY 1, 2, 3
        ), geo AS (
            SELECT h3_res, count(*) AS n FROM gold.cell_geometry WHERE source_id = %(s)s GROUP BY 1
        )
        SELECT l.* FROM layer l JOIN geo g USING (h3_res)
        WHERE l.lo <> l.hi OR l.lo <> l.n OR l.n <> g.n
        """,
        {"s": SOURCE},
    )
    assert bad == []


def test_percentile_strictly_monotone_in_density(built):
    # gold.py _ACTIVITY_SQL: pr = percent_rank() OVER (PARTITION BY category ORDER BY
    # n / area_km2), so a strictly lower density always has a strictly lower percentile.
    bad = _scalar(
        built["conn"],
        """
        SELECT count(*)
        FROM gold.cell_activity a
        JOIN gold.cell_activity b
          ON b.source_id = a.source_id AND b.h3_res = a.h3_res
         AND b.time_window = a.time_window AND b.category = a.category
        WHERE a.source_id = %s
          AND a.incidents_per_km2 < b.incidents_per_km2
          AND a.percentile >= b.percentile
        """,
        (SOURCE,),
    )
    assert bad == 0


def test_categories_add_up_to_all(built):
    # gold.py _ACTIVITY_SQL: c_all = count(*); the four category counts FILTER on
    # product_category, which silver constrains to exactly those four values.
    bad = _rows(
        built["conn"],
        """
        SELECT h3_index, h3_res, time_window
        FROM gold.cell_activity
        WHERE source_id = %s
        GROUP BY 1, 2, 3
        HAVING count(*) = 5
           AND sum(incident_count) FILTER (WHERE category <> 'all')
               <> sum(incident_count) FILTER (WHERE category = 'all')
        """,
        (SOURCE,),
    )
    assert bad == []


def test_incidents_are_conserved_per_window(built):
    # gold.py _ACTIVITY_SQL counts silver rows with occurred_local_date BETWEEN
    # window_start AND window_end; every occupied cell is in the universe
    # (build_cell_universe unions occupied cells in), so none are dropped.
    conn, rows = built["conn"], built["rows"]
    for res in gold.RESOLUTIONS:
        scope_windows, _ = gold.activity_scope(res)
        for window in built["windows"]:
            if window.name not in scope_windows:
                continue
            expected = sum(1 for r in rows if window.start <= r["date"] <= window.end)
            got = _scalar(
                conn,
                """
                SELECT COALESCE(sum(incident_count), 0) FROM gold.cell_activity
                WHERE source_id = %s AND h3_res = %s AND time_window = %s AND category = 'all'
                """,
                (SOURCE, res, window.name),
            )
            assert got == expected, (res, window.name)


def test_untouched_cells_are_tier_zero_everywhere(built):
    # gold.py _ACTIVITY_SQL: COALESCE(c.c_all, 0) for universe cells with no
    # incidents, and tier 0 WHEN n = 0.
    conn, rows = built["conn"], built["rows"]
    occupied = {r[k] for r in rows for k in ("r8", "r9", "r10")}
    tiers = _rows(
        conn,
        "SELECT h3_index, activity_tier FROM gold.cell_activity WHERE source_id = %s",
        (SOURCE,),
    )
    empty = [t for t in tiers if t["h3_index"] not in occupied]
    assert empty, "the fixture should leave some universe cells empty"
    assert all(t["activity_tier"] == 0 for t in empty)


def test_safety_tiers_and_percentile_order(built):
    # gold.py _SAFETY_SQL: tier 0 WHEN n = 0; pct is the Hazen midrank over
    # rank() OVER (PARTITION BY track ORDER BY smoothed DESC), so 1.0 is safest
    # and a strictly lower smoothed rate always has a strictly higher percentile.
    conn = built["conn"]
    assert _scalar(conn, "SELECT count(*) FROM gold.cell_safety WHERE source_id = %s", (SOURCE,)) > 0
    bad_tier = _scalar(
        conn,
        """
        SELECT count(*) FROM gold.cell_safety
        WHERE source_id = %s AND (safety_tier = 0) <> (incident_count = 0)
        """,
        (SOURCE,),
    )
    assert bad_tier == 0
    bad_order = _scalar(
        conn,
        """
        SELECT count(*)
        FROM gold.cell_safety a
        JOIN gold.cell_safety b
          ON b.source_id = a.source_id AND b.h3_res = a.h3_res
         AND b.time_window = a.time_window AND b.track = a.track
         AND b.scheme_version = a.scheme_version
        WHERE a.source_id = %s
          AND a.smoothed_per_km2 < b.smoothed_per_km2
          AND a.safety_percentile <= b.safety_percentile
        """,
        (SOURCE,),
    )
    assert bad_order == 0


def test_hour_profile_matches_rows_with_a_known_hour(built):
    # gold.py _HOUR_PROFILE_SQL: GROUPING SETS ((cell, hour, category), (cell, hour))
    # over rows with occurred_local_hour IS NOT NULL in the window; the grouping
    # set without category is written as category 'all'.
    conn, rows = built["conn"], built["rows"]
    windows = {w.name: w for w in built["windows"]}
    for res in gold.HOURLY_RESOLUTIONS:
        key = {8: "r8", 9: "r9"}[res]
        for name in gold.HOURLY_WINDOWS:
            w = windows[name]
            in_window = [r for r in rows if w.start <= r["date"] <= w.end]
            known = Counter(r[key] for r in in_window if r["hour"] is not None)
            profile = {
                r["h3_index"]: r["n"]
                for r in _rows(
                    conn,
                    """
                    SELECT h3_index, sum(incident_count) AS n FROM gold.cell_hour_profile
                    WHERE source_id = %s AND h3_res = %s AND time_window = %s AND category = 'all'
                    GROUP BY 1
                    """,
                    (SOURCE, res, name),
                )
            }
            assert profile == dict(known), (res, name)
            all_hours = {
                r["h3_index"]: r["incident_count"]
                for r in _rows(
                    conn,
                    """
                    SELECT h3_index, incident_count FROM gold.cell_activity
                    WHERE source_id = %s AND h3_res = %s AND time_window = %s AND category = 'all'
                    """,
                    (SOURCE, res, name),
                )
            }
            assert all(n <= all_hours[cell] for cell, n in profile.items()), (res, name)


def test_refresh_is_idempotent(built):
    # refresh_all deletes and rebuilds every layer in one transaction
    # (refresh_cell_activity's DELETE-then-INSERT), so a second run over the same
    # silver rows reproduces the same layer exactly.
    conn = built["conn"]
    query = """
        SELECT h3_index, h3_res, time_window, category, incident_count, percentile, activity_tier
        FROM gold.cell_activity WHERE source_id = %s
    """
    before = {tuple(r.values()) for r in _rows(conn, query, (SOURCE,))}
    gold.refresh_all(conn, SOURCE, PIPELINE_VERSION, include_hourly=True)
    after = {tuple(r.values()) for r in _rows(conn, query, (SOURCE,))}
    assert before == after


def test_hourly_per_capita_scheme_skipped_without_exposure(built):
    # gold.py refresh_cell_hour_safety: a per-capita scheme calls _require_exposure
    # over HOURLY_RESOLUTIONS before its DELETE, and refresh_hourly_layer skips the
    # scheme on LookupError (P1). The fixture loads no census, so the per-capita
    # scheme (the only enabled one) must have no hourly ranking at all, rather
    # than one built from the prior alone with every cell tied.
    n = _scalar(
        built["conn"],
        """
        SELECT count(*) FROM gold.cell_hour_safety h
        JOIN reference.severity_scheme s USING (scheme_version)
        WHERE h.source_id = %s AND s.exposure_kind = 'ambient_population'
        """,
        (SOURCE,),
    )
    assert n == 0


# ---------------------------------------------------------------- windows (018)


def test_fixture_spans_the_two_year_window_list(built):
    # 800 days of history: the four short windows, one and two years, and no
    # third year (70 days past last_2y is under PARTIAL_WINDOW_MIN_SHARE).
    assert [w.name for w in built["windows"]] == [
        "last_30d", "last_3m", "last_6m", "last_9m", "last_1y", "last_2y"
    ]


def test_city_window_matches_what_was_built(built):
    # gold.write_city_windows writes one row per window, in order, with the
    # flags the layer refreshes used.
    rows = _rows(
        built["conn"],
        "SELECT * FROM gold.city_window WHERE source_id = %s ORDER BY sort_order",
        (SOURCE,),
    )
    assert [r["time_window"] for r in rows] == [w.name for w in built["windows"]]
    for row, w in zip(rows, built["windows"]):
        assert (row["window_start"], row["window_end"]) == (w.start, w.end)
        assert row["hourly_built"] == (w.name in gold.HOURLY_WINDOWS)
        assert row["res10_built"] == gold.activity_builds(10, w.name)
        assert row["safety_built"] is True
        n = _scalar(
            built["conn"],
            """
            SELECT count(*) FROM gold.cell_activity
            WHERE source_id = %s AND time_window = %s AND h3_res = 8
            """,
            (SOURCE, w.name),
        )
        assert n > 0, w.name


def test_no_layer_holds_a_window_outside_the_city_list(built):
    # Every refresh deletes all windows for the city before rebuilding, so a
    # window the list no longer has cannot linger. Legacy names are the one
    # exception while settings.gold_legacy_windows is on.
    allowed = {w.name for w in built["windows"]} | set(gold.LEGACY_WINDOWS)
    for table in gold.ALL_HOURS_TABLES + gold.HOURLY_TABLES:
        names = {
            r["time_window"]
            for r in _rows(
                built["conn"],
                f"SELECT DISTINCT time_window FROM gold.{table} WHERE source_id = %s",
                (SOURCE,),
            )
        }
        assert names <= allowed, (table, names - allowed)


@pytest.mark.parametrize("table", ["cell_activity", "cell_safety", "cell_offense_mix",
                                   "cell_hour_profile"])
def test_legacy_names_are_exact_copies(built, table):
    # gold.write_legacy_windows: last_12m / last_24m / last_90d are the rows of
    # last_1y / last_2y / last_3m under the old name, so the previous release
    # reads the same numbers the new one serves.
    def fingerprint(name):
        (row,) = _rows(
            built["conn"],
            f"""
            SELECT count(*) AS n, md5(string_agg(j, ',' ORDER BY j)) AS digest
            FROM (
                SELECT (to_jsonb(t) - 'time_window')::text AS j
                FROM gold.{table} t
                WHERE source_id = %s AND time_window = %s
            ) rows
            """,
            (SOURCE, name),
        )
        return row["n"], row["digest"]

    for legacy, current in gold.LEGACY_WINDOWS.items():
        if table == "cell_hour_profile" and current not in gold.HOURLY_WINDOWS:
            continue
        old_n, old_digest = fingerprint(legacy)
        new_n, new_digest = fingerprint(current)
        assert new_n > 0, (table, current)
        assert (old_n, old_digest) == (new_n, new_digest), (table, legacy)
