"""Silver -> gold rollups (design doc S5 layer 3, S9.3, S3.3).

Everything the map and the API read is materialized here by the pipeline.
S5 is explicit that this belongs upstream of the application: dynamic
aggregation over raw points does not scale to an interactive map with many
concurrent users, and it re-does identical work on every request.

The relative measure follows S3.3: a cell's percentile of reported-incident
*density* against other cells in the same city, over the same window and
offense category -- not an absolute, calibrated risk number.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from safety.h3grid import (
    RESOLUTIONS,
    cell_area_km2,
    cell_centroid,
    cell_polygon_geojson,
    cells_covering,
)

log = logging.getLogger(__name__)

# S5/S9.3: the windows and categories the product actually needs, precomputed.
TIME_WINDOWS = ("last_30d", "last_90d", "last_12m", "last_24m")
CATEGORIES = ("all", "violent", "property", "quality_of_life", "other")
FILTERED_CATEGORIES = tuple(c for c in CATEGORIES if c != "all")
OFFENSE_MIX_DEPTH = 8


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    start: date
    end: date


def resolve_windows(anchor: date) -> list[Window]:
    """Windows are anchored to the newest reported date, not to today.

    Anchoring to `now` would silently present a source's publication lag as an
    absence of crime. S12(b) wants "data as of" visible; this makes the windows
    themselves honest about it too.
    """
    return [
        Window("last_30d", anchor - timedelta(days=29), anchor),
        Window("last_90d", anchor - timedelta(days=89), anchor),
        Window("last_12m", _shift_years(anchor, 1) + timedelta(days=1), anchor),
        Window("last_24m", _shift_years(anchor, 2) + timedelta(days=1), anchor),
    ]


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:  # 29 February
        return value.replace(year=value.year - years, day=28)


def data_anchor(conn: psycopg.Connection, source_id: str) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(occurred_local_date) AS anchor FROM silver.incident WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    return row["anchor"] if row else None


# ---------------------------------------------------------------------------
# Cell universe
# ---------------------------------------------------------------------------


def build_cell_universe(conn: psycopg.Connection, source_id: str) -> dict[int, int]:
    """Materialize gold.cell_geometry for every resolution.

    The universe is the union of two sets:

    1. cells tiling the city boundary (h3shape_to_cells uses center
       containment, so this alone omits edge cells), and
    2. cells that incidents actually landed in.

    Without (2), an incident near the city edge would have no cell to be
    aggregated into. Without (1), a cell with zero reported incidents would
    vanish from the denominator and inflate every other cell's percentile.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_AsGeoJSON(geom) AS geojson FROM reference.city_boundary WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(
            f"no boundary stored for '{source_id}'; run the boundary pull first"
        )
    boundary = json.loads(row["geojson"])

    counts: dict[int, int] = {}
    for res in RESOLUTIONS:
        cells = set(cells_covering(boundary, res))
        column = _h3_column(res)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT {column} AS cell FROM silver.incident WHERE source_id = %s",
                (source_id,),
            )
            occupied = {r["cell"] for r in cur.fetchall()}
        edge_cells = occupied - cells
        if edge_cells:
            log.info(
                "res %s: %s occupied cell(s) sit outside the boundary fill; including them",
                res,
                len(edge_cells),
            )
        cells |= occupied

        payload = [
            (
                cell,
                source_id,
                res,
                cell_area_km2(cell),
                *cell_centroid(cell),
                json.dumps(cell_polygon_geojson(cell)),
            )
            for cell in sorted(cells)
        ]
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO gold.cell_geometry
                    (h3_index, source_id, h3_res, area_km2, centroid, boundary, built_at)
                VALUES (
                    %s, %s, %s, %s,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                    now()
                )
                ON CONFLICT (h3_index) DO UPDATE SET
                    area_km2 = EXCLUDED.area_km2,
                    centroid = EXCLUDED.centroid,
                    boundary = EXCLUDED.boundary,
                    built_at = EXCLUDED.built_at
                """,
                payload,
            )
        counts[res] = len(payload)
        log.info("cell universe res %s: %s cells", res, len(payload))

    conn.commit()
    return counts


def _h3_column(res: int) -> str:
    """Whitelist the resolution -> column mapping; never interpolate freely."""
    columns = {8: "h3_r8", 9: "h3_r9"}
    try:
        return columns[res]
    except KeyError:
        raise ValueError(f"resolution {res} is not stored on silver.incident") from None


# ---------------------------------------------------------------------------
# Cell activity: one pass per (resolution, window), all categories at once
# ---------------------------------------------------------------------------

_ACTIVITY_SQL = """
WITH counts AS (
    SELECT
        {h3_column} AS h3_index,
        count(*)                                                        AS c_all,
        count(*) FILTER (WHERE product_category = 'violent')            AS c_violent,
        count(*) FILTER (WHERE product_category = 'property')           AS c_property,
        count(*) FILTER (WHERE product_category = 'quality_of_life')    AS c_quality_of_life,
        count(*) FILTER (WHERE product_category = 'other')              AS c_other
    FROM silver.incident
    WHERE source_id = %(source_id)s
      AND occurred_local_date BETWEEN %(window_start)s AND %(window_end)s
    GROUP BY 1
),
universe AS (
    SELECT h3_index, area_km2
    FROM gold.cell_geometry
    WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
),
joined AS (
    SELECT
        u.h3_index,
        u.area_km2,
        COALESCE(c.c_all, 0)             AS c_all,
        COALESCE(c.c_violent, 0)         AS c_violent,
        COALESCE(c.c_property, 0)        AS c_property,
        COALESCE(c.c_quality_of_life, 0) AS c_quality_of_life,
        COALESCE(c.c_other, 0)           AS c_other
    FROM universe u
    LEFT JOIN counts c USING (h3_index)
),
unpivoted AS (
    SELECT h3_index, area_km2, category, n
    FROM joined
    CROSS JOIN LATERAL (VALUES
        ('all',             c_all),
        ('violent',         c_violent),
        ('property',        c_property),
        ('quality_of_life', c_quality_of_life),
        ('other',           c_other)
    ) AS v(category, n)
),
ranked AS (
    SELECT
        h3_index,
        category,
        n,
        n::double precision / area_km2 AS density,
        -- S3.3: position within this city's own distribution for this window
        -- and category. Ties (notably the block of zero-incident cells) all
        -- receive the same, lowest, percentile.
        percent_rank() OVER (PARTITION BY category ORDER BY n::double precision / area_km2) AS pr,
        rank()         OVER (PARTITION BY category ORDER BY n::double precision / area_km2 DESC) AS rnk,
        count(*)       OVER (PARTITION BY category) AS cell_total
    FROM unpivoted
)
INSERT INTO gold.cell_activity (
    source_id, h3_index, h3_res, time_window, category,
    window_start, window_end, incident_count, incidents_per_km2,
    city_rank, city_cell_total, percentile, activity_tier, refreshed_at
)
SELECT
    %(source_id)s, h3_index, %(h3_res)s, %(time_window)s, category,
    %(window_start)s, %(window_end)s, n, density,
    rnk, cell_total, pr,
    CASE
        -- Tier 0 is "nothing was reported here", which is a different
        -- statement from "this is the quietest fifth of the city" (S2).
        WHEN n = 0     THEN 0
        WHEN pr < 0.20 THEN 1
        WHEN pr < 0.40 THEN 2
        WHEN pr < 0.60 THEN 3
        WHEN pr < 0.80 THEN 4
        ELSE 5
    END,
    now()
FROM ranked
"""


def refresh_cell_activity(
    conn: psycopg.Connection, source_id: str, windows: list[Window]
) -> int:
    """Rebuild gold.cell_activity for every resolution/window/category."""
    written = 0
    with conn.cursor() as cur:
        for res in RESOLUTIONS:
            h3_column = _h3_column(res)
            for window in windows:
                # Delete-then-insert inside the caller's transaction: readers
                # keep seeing the previous rollup until commit, so the map
                # never renders a half-built layer.
                cur.execute(
                    """
                    DELETE FROM gold.cell_activity
                    WHERE source_id = %s AND h3_res = %s AND time_window = %s
                    """,
                    (source_id, res, window.name),
                )
                cur.execute(
                    _ACTIVITY_SQL.format(h3_column=h3_column),
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                    },
                )
                written += cur.rowcount
                log.info(
                    "cell_activity res=%s window=%s -> %s rows", res, window.name, cur.rowcount
                )
    return written


# ---------------------------------------------------------------------------
# Monthly series (sparse) and offense mix, for the cell detail panel
# ---------------------------------------------------------------------------

_MONTHLY_SQL = """
INSERT INTO gold.cell_monthly
    (source_id, h3_index, h3_res, month_start, category, incident_count, refreshed_at)
SELECT
    %(source_id)s,
    {h3_column},
    %(h3_res)s,
    date_trunc('month', occurred_local_date)::date,
    COALESCE(product_category, 'all'),
    count(*),
    now()
FROM silver.incident
WHERE source_id = %(source_id)s
  AND occurred_local_date BETWEEN %(window_start)s AND %(window_end)s
GROUP BY GROUPING SETS (
    ({h3_column}, date_trunc('month', occurred_local_date), product_category),
    ({h3_column}, date_trunc('month', occurred_local_date))
)
"""

_OFFENSE_MIX_SQL = """
WITH ranked AS (
    SELECT
        {h3_column}          AS h3_index,
        raw_offense_text,
        min(nibrs_code)      AS nibrs_code,
        min(product_category) AS product_category,
        count(*)             AS n,
        row_number() OVER (
            PARTITION BY {h3_column}
            ORDER BY count(*) DESC, raw_offense_text
        ) AS rn
    FROM silver.incident
    WHERE source_id = %(source_id)s
      AND occurred_local_date BETWEEN %(window_start)s AND %(window_end)s
      AND raw_offense_text IS NOT NULL
    GROUP BY 1, 2
)
INSERT INTO gold.cell_offense_mix (
    source_id, h3_index, h3_res, time_window, rank,
    raw_offense_text, nibrs_code, product_category, incident_count, refreshed_at
)
SELECT
    %(source_id)s, h3_index, %(h3_res)s, %(time_window)s, rn,
    raw_offense_text, nibrs_code, product_category, n, now()
FROM ranked
WHERE rn <= %(depth)s
"""


def refresh_cell_detail(
    conn: psycopg.Connection, source_id: str, windows: list[Window]
) -> tuple[int, int]:
    """Rebuild the monthly series and per-cell offense mix."""
    widest = max(windows, key=lambda w: (w.end - w.start).days)
    monthly_rows = 0
    mix_rows = 0

    with conn.cursor() as cur:
        for res in RESOLUTIONS:
            h3_column = _h3_column(res)

            cur.execute(
                "DELETE FROM gold.cell_monthly WHERE source_id = %s AND h3_res = %s",
                (source_id, res),
            )
            cur.execute(
                _MONTHLY_SQL.format(h3_column=h3_column),
                {
                    "source_id": source_id,
                    "h3_res": res,
                    "window_start": widest.start,
                    "window_end": widest.end,
                },
            )
            monthly_rows += cur.rowcount

            for window in windows:
                cur.execute(
                    """
                    DELETE FROM gold.cell_offense_mix
                    WHERE source_id = %s AND h3_res = %s AND time_window = %s
                    """,
                    (source_id, res, window.name),
                )
                cur.execute(
                    _OFFENSE_MIX_SQL.format(h3_column=h3_column),
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                        "depth": OFFENSE_MIX_DEPTH,
                    },
                )
                mix_rows += cur.rowcount

    log.info("cell_monthly -> %s rows, cell_offense_mix -> %s rows", monthly_rows, mix_rows)
    return monthly_rows, mix_rows


# ---------------------------------------------------------------------------
# City snapshot (design doc S12b)
# ---------------------------------------------------------------------------

_SNAPSHOT_SQL = """
INSERT INTO gold.city_snapshot (
    source_id, city_name, agency_name, data_as_of, last_refreshed_at,
    expected_cadence, publication_lag_days, freshness_note,
    incident_count, coverage_start, coverage_end,
    cell_count_r8, cell_count_r9,
    center_lat, center_lng, bbox_west, bbox_south, bbox_east, bbox_north,
    crosswalk_version, pipeline_version, attribution_text, terms_url,
    unmapped_offense_count, rejected_record_count
)
SELECT
    r.source_id, r.city_name, r.agency_name,
    stats.data_as_of, now(),
    r.expected_cadence, r.publication_lag_days, r.freshness_note,
    stats.incident_count, stats.coverage_start, stats.coverage_end,
    cells.r8, cells.r9,
    ST_Y(ST_Centroid(b.geom)), ST_X(ST_Centroid(b.geom)),
    ST_XMin(b.geom::box2d), ST_YMin(b.geom::box2d),
    ST_XMax(b.geom::box2d), ST_YMax(b.geom::box2d),
    r.crosswalk_version, %(pipeline_version)s, r.attribution_text, r.terms_url,
    COALESCE(quality.unmapped, 0), COALESCE(quality.rejected, 0)
FROM reference.source_registry r
JOIN reference.city_boundary b ON b.source_id = r.source_id
CROSS JOIN LATERAL (
    SELECT
        max(occurred_at)         AS data_as_of,
        count(*)                 AS incident_count,
        min(occurred_local_date) AS coverage_start,
        max(occurred_local_date) AS coverage_end
    FROM silver.incident WHERE source_id = r.source_id
) stats
CROSS JOIN LATERAL (
    SELECT
        count(*) FILTER (WHERE h3_res = 8)::int AS r8,
        count(*) FILTER (WHERE h3_res = 9)::int AS r9
    FROM gold.cell_geometry WHERE source_id = r.source_id
) cells
CROSS JOIN LATERAL (
    SELECT
        (SELECT count(*) FROM silver.incident
          WHERE source_id = r.source_id AND mapping_confidence = 'unmapped')::int AS unmapped,
        (SELECT COALESCE(sum(records_rejected), 0) FROM etl.pull_run
          WHERE source_id = r.source_id AND status = 'succeeded')::int AS rejected
) quality
WHERE r.source_id = %(source_id)s
ON CONFLICT (source_id) DO UPDATE SET
    data_as_of             = EXCLUDED.data_as_of,
    last_refreshed_at      = EXCLUDED.last_refreshed_at,
    expected_cadence       = EXCLUDED.expected_cadence,
    publication_lag_days   = EXCLUDED.publication_lag_days,
    freshness_note         = EXCLUDED.freshness_note,
    incident_count         = EXCLUDED.incident_count,
    coverage_start         = EXCLUDED.coverage_start,
    coverage_end           = EXCLUDED.coverage_end,
    cell_count_r8          = EXCLUDED.cell_count_r8,
    cell_count_r9          = EXCLUDED.cell_count_r9,
    center_lat             = EXCLUDED.center_lat,
    center_lng             = EXCLUDED.center_lng,
    bbox_west              = EXCLUDED.bbox_west,
    bbox_south             = EXCLUDED.bbox_south,
    bbox_east              = EXCLUDED.bbox_east,
    bbox_north             = EXCLUDED.bbox_north,
    crosswalk_version      = EXCLUDED.crosswalk_version,
    pipeline_version       = EXCLUDED.pipeline_version,
    attribution_text       = EXCLUDED.attribution_text,
    terms_url              = EXCLUDED.terms_url,
    unmapped_offense_count = EXCLUDED.unmapped_offense_count,
    rejected_record_count  = EXCLUDED.rejected_record_count
"""


def refresh_city_snapshot(
    conn: psycopg.Connection, source_id: str, pipeline_version: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _SNAPSHOT_SQL, {"source_id": source_id, "pipeline_version": pipeline_version}
        )


def refresh_all(
    conn: psycopg.Connection, source_id: str, pipeline_version: str
) -> dict[str, int]:
    """Full gold refresh for one city. Runs as a single transaction."""
    anchor = data_anchor(conn, source_id)
    if anchor is None:
        raise LookupError(f"no silver rows for '{source_id}'; nothing to roll up")

    cells = build_cell_universe(conn, source_id)
    windows = resolve_windows(anchor)
    log.info(
        "gold anchor date %s; windows: %s",
        anchor,
        ", ".join(f"{w.name}[{w.start}..{w.end}]" for w in windows),
    )

    activity_rows = refresh_cell_activity(conn, source_id, windows)
    monthly_rows, mix_rows = refresh_cell_detail(conn, source_id, windows)
    refresh_city_snapshot(conn, source_id, pipeline_version)
    conn.commit()

    return {
        "cells_r8": cells.get(8, 0),
        "cells_r9": cells.get(9, 0),
        "cell_activity_rows": activity_rows,
        "cell_monthly_rows": monthly_rows,
        "cell_offense_mix_rows": mix_rows,
    }
