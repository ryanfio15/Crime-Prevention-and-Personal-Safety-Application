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
from typing import Any

import psycopg

from safety.h3grid import (
    RESOLUTIONS,
    cell_area_km2,
    cell_centroid,
    cell_polygon_geojson,
    cells_covering,
    grid_disk,
)

log = logging.getLogger(__name__)

# S5/S9.3: the windows and categories the product actually needs, precomputed.
TIME_WINDOWS = ("last_30d", "last_90d", "last_12m", "last_24m")
CATEGORIES = ("all", "violent", "property", "quality_of_life", "other")
FILTERED_CATEGORIES = tuple(c for c in CATEGORIES if c != "all")
OFFENSE_MIX_DEPTH = 8

# The safety ranking splits the city two ways rather than five, and publishes no
# combined figure. That follows the FBI, which discontinued its own combined
# Crime Index in 2004 -- an unweighted total is dominated by whichever offense is
# most numerous, normally larceny-theft -- and has reported violent and property
# separately ever since.
TRACKS = ("violent", "non_violent")
SAFETY_TIERS = 4

# Time of day. Block h covers [h:00, h+1:00) local; 23 is 23:00-24:00.
HOUR_BLOCKS = 24

# The hourly layer is built narrower than the all-hours one, and the reason is
# statistical before it is about disk. Splitting a window across 24 buckets
# leaves each one with a twenty-fourth of the evidence, and 30 days at
# resolution 10 puts the median cell-hour at zero reported incidents -- there is
# no distribution there to rank. The two widest windows at the two coarser
# resolutions are where the counts still support the statistic.
#
# Widening this is a one-line change; the tables accept every window and
# resolution the all-hours layer does.
HOURLY_RESOLUTIONS = (8, 9)
HOURLY_WINDOWS = ("last_12m", "last_24m")

# Below this many incidents across the whole window, a cell's hour-to-hour
# ratio is noise dressed as a measurement, and hour_index is left NULL rather
# than published. Twelve is half an incident per hour block on average.
MIN_HOUR_EVIDENCE = 12


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
                f"SELECT DISTINCT {column} AS cell FROM silver.incident "
                f"WHERE source_id = %s AND {column} IS NOT NULL",
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

        _build_cell_neighbors(conn, source_id, res, cells)

    conn.commit()
    return counts


def _build_cell_neighbors(
    conn: psycopg.Connection, source_id: str, res: int, cells: set[str]
) -> int:
    """Materialize ring-1 adjacency for the smoothing in gold.cell_safety.

    Restricted to pairs where both cells are in the universe, so a cell on the
    city edge averages over the neighbours it actually has rather than being
    dragged toward zero by ones that do not exist.
    """
    payload = [
        (source_id, res, cell, neighbor)
        for cell in sorted(cells)
        for neighbor in grid_disk(cell, 1)
        if neighbor != cell and neighbor in cells
    ]
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM gold.cell_neighbor WHERE source_id = %s AND h3_res = %s",
            (source_id, res),
        )
        cur.executemany(
            """
            INSERT INTO gold.cell_neighbor (source_id, h3_res, h3_index, neighbor_h3)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            payload,
        )
    log.info("cell adjacency res %s: %s pairs", res, len(payload))
    return len(payload)


def _h3_column(res: int) -> str:
    """Whitelist the resolution -> column mapping; never interpolate freely."""
    columns = {8: "h3_r8", 9: "h3_r9", 10: "h3_r10"}
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
# Safety ranking: severity-weighted, violent and non-violent ranked separately
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scheme:
    version: str
    eb_prior_km2: float
    self_weight: float


def enabled_schemes(conn: psycopg.Connection) -> list[Scheme]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scheme_version, eb_prior_km2, self_weight
            FROM reference.severity_scheme
            WHERE enabled
            ORDER BY scheme_version
            """
        )
        return [
            Scheme(r["scheme_version"], r["eb_prior_km2"], r["self_weight"])
            for r in cur.fetchall()
        ]


def get_scheme(conn: psycopg.Connection, version: str) -> Scheme:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scheme_version, eb_prior_km2, self_weight
            FROM reference.severity_scheme WHERE scheme_version = %s
            """,
            (version,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no severity scheme '{version}'; load reference/severity first")
    return Scheme(row["scheme_version"], row["eb_prior_km2"], row["self_weight"])


def active_scheme(conn: psycopg.Connection, source_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT severity_scheme_version FROM reference.source_registry WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    return row["severity_scheme_version"] if row else None


# Resolve one incident's severity weight. Precedence is most specific first:
# the source's own offense text (Philadelphia distinguishes armed from unarmed
# robbery and assault, which the severity literature scores very differently),
# then the standardized NIBRS code, then the coarse UCR bucket. The track is
# part of the lookup because the two tracks are weighted independently.
_WEIGHT_LOOKUP = """
    LEFT JOIN LATERAL (
        SELECT sw.weight, sw.sourced
        FROM reference.offense_severity_weight sw
        WHERE sw.scheme_version = %(scheme)s
          AND sw.track = CASE WHEN i.product_category = 'violent'
                              THEN 'violent' ELSE 'non_violent' END
          AND (
               (sw.key_type = 'raw_offense_text_key'
                    AND sw.key_value = upper(btrim(i.raw_offense_text)))
            OR (sw.key_type = 'nibrs_code'      AND sw.key_value = i.nibrs_code)
            OR (sw.key_type = 'severity_bucket' AND sw.key_value = i.severity_bucket)
          )
        ORDER BY CASE sw.key_type
                     WHEN 'raw_offense_text_key' THEN 1
                     WHEN 'nibrs_code'           THEN 2
                     ELSE 3
                 END
        LIMIT 1
    ) w ON true
"""

_SAFETY_SQL = """
WITH weighted AS (
    SELECT
        i.{h3_column} AS h3_index,
        count(*) FILTER (WHERE i.product_category =  'violent') AS n_violent,
        count(*) FILTER (WHERE i.product_category <> 'violent') AS n_non_violent,
        COALESCE(sum(COALESCE(w.weight, 1.0))
                 FILTER (WHERE i.product_category =  'violent'), 0) AS w_violent,
        COALESCE(sum(COALESCE(w.weight, 1.0))
                 FILTER (WHERE i.product_category <> 'violent'), 0) AS w_non_violent
    FROM silver.incident i
    {weight_lookup}
    WHERE i.source_id = %(source_id)s
      AND i.occurred_local_date BETWEEN %(window_start)s AND %(window_end)s
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
        COALESCE(x.n_violent, 0)     AS n_violent,
        COALESCE(x.n_non_violent, 0) AS n_non_violent,
        COALESCE(x.w_violent, 0)     AS w_violent,
        COALESCE(x.w_non_violent, 0) AS w_non_violent
    FROM universe u
    LEFT JOIN weighted x USING (h3_index)
),
unpivoted AS (
    SELECT h3_index, area_km2, track, n, w
    FROM joined
    CROSS JOIN LATERAL (VALUES
        ('violent',     n_violent,     w_violent),
        ('non_violent', n_non_violent, w_non_violent)
    ) AS v(track, n, w)
),
city AS (
    -- The rate each cell is shrunk toward: this track's citywide weighted
    -- offense per km2.
    SELECT track, sum(w) / NULLIF(sum(area_km2), 0) AS city_rate
    FROM unpivoted
    GROUP BY track
),
adjusted AS (
    SELECT
        u.h3_index, u.area_km2, u.track, u.n, u.w,
        -- Poisson-gamma posterior rate: the cell's own weighted total plus
        -- eb_prior_km2 worth of citywide-average offense, over its own area
        -- plus that same prior area.
        --
        -- The prior has to be an exposure rather than a count. A cell with no
        -- incidents was still watched for the whole window, so zero is evidence
        -- of a low rate, not missing information -- shrinking by n/(n+k) instead
        -- sends every empty cell to the citywide mean, which ranked a cell with
        -- six assaults safer than a cell with none.
        (u.w + COALESCE(c.city_rate, 0) * %(eb_prior)s)
            / NULLIF(u.area_km2 + %(eb_prior)s, 0) AS adj
    FROM unpivoted u
    JOIN city c USING (track)
),
blended AS (
    SELECT
        a.h3_index, a.area_km2, a.track, a.n, a.w, a.adj,
        -- Risk does not stop at a hexagon edge. A cell with no in-universe
        -- neighbours keeps its own value rather than being pulled toward zero.
        CASE WHEN nb.mean_adj IS NULL THEN a.adj
             ELSE %(self_weight)s * a.adj + (1 - %(self_weight)s) * nb.mean_adj
        END AS smoothed
    FROM adjusted a
    LEFT JOIN LATERAL (
        SELECT avg(x.adj) AS mean_adj
        FROM gold.cell_neighbor nbr
        JOIN adjusted x
          ON x.h3_index = nbr.neighbor_h3 AND x.track = a.track
        WHERE nbr.source_id = %(source_id)s
          AND nbr.h3_res   = %(h3_res)s
          AND nbr.h3_index = a.h3_index
    ) nb ON true
),
ranked AS (
    SELECT
        b.*,
        -- Hazen midrank, ordered so the *lowest* weighted total scores highest:
        -- 1.0 is the safest cell. Tie blocks sit at the centre of their own
        -- range, so the score never degenerates to exactly 0 or 1.
        (
            (rank() OVER (PARTITION BY b.track ORDER BY b.smoothed DESC)
             + (count(*) OVER (PARTITION BY b.track, b.smoothed) - 1) / 2.0)
            - 0.5
        ) / count(*) OVER (PARTITION BY b.track) AS pct,
        rank()   OVER (PARTITION BY b.track ORDER BY b.smoothed DESC) AS rnk,
        count(*) OVER (PARTITION BY b.track) AS cell_total
    FROM blended b
)
INSERT INTO gold.cell_safety (
    source_id, h3_index, h3_res, time_window, track, scheme_version,
    window_start, window_end, incident_count,
    weighted_total, weighted_per_km2, smoothed_per_km2,
    safety_percentile, safety_rank, city_cell_total, safety_tier, refreshed_at
)
SELECT
    %(source_id)s, h3_index, %(h3_res)s, %(time_window)s, track, %(scheme)s,
    %(window_start)s, %(window_end)s, n,
    w, w / area_km2, smoothed,
    pct, rnk, cell_total,
    CASE
        -- Tier 0 is "nothing of this track was reported here", which is not the
        -- same claim as "this is among the safest quarter of the city": an
        -- absence of reports can be an absence of reporting (S13).
        WHEN n = 0      THEN 0
        WHEN pct < 0.25 THEN 1
        WHEN pct < 0.50 THEN 2
        WHEN pct < 0.75 THEN 3
        ELSE 4
    END,
    now()
FROM ranked
"""

_COVERAGE_SQL = f"""
SELECT
    count(*)                                        AS total,
    count(*) FILTER (WHERE w.sourced IS TRUE)       AS sourced
FROM silver.incident i
{_WEIGHT_LOOKUP}
WHERE i.source_id = %(source_id)s
"""


def weight_coverage(conn: psycopg.Connection, source_id: str, scheme: str) -> float:
    """Share of incidents whose weight is a published figure, not a fallback.

    Surfaced rather than logged: a scheme that mostly falls back is a scheme
    whose ranking is really just the coarse UCR bucket (S8.5).
    """
    with conn.cursor() as cur:
        cur.execute(_COVERAGE_SQL, {"source_id": source_id, "scheme": scheme})
        row = cur.fetchone()
    if not row or not row["total"]:
        return 0.0
    return row["sourced"] / row["total"]


def refresh_cell_safety(
    conn: psycopg.Connection,
    source_id: str,
    windows: list[Window],
    scheme: Scheme,
) -> int:
    """Rebuild gold.cell_safety for one scheme, every resolution and window."""
    written = 0
    with conn.cursor() as cur:
        for res in RESOLUTIONS:
            h3_column = _h3_column(res)
            sql = _SAFETY_SQL.format(
                h3_column=h3_column, weight_lookup=_WEIGHT_LOOKUP
            )
            for window in windows:
                # Delete-then-insert inside the caller's transaction, so readers
                # keep seeing the previous ranking until commit.
                cur.execute(
                    """
                    DELETE FROM gold.cell_safety
                    WHERE source_id = %s AND h3_res = %s
                      AND time_window = %s AND scheme_version = %s
                    """,
                    (source_id, res, window.name, scheme.version),
                )
                cur.execute(
                    sql,
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                        "scheme": scheme.version,
                        "eb_prior": scheme.eb_prior_km2,
                        "self_weight": scheme.self_weight,
                    },
                )
                written += cur.rowcount
                log.info(
                    "cell_safety scheme=%s res=%s window=%s -> %s rows",
                    scheme.version,
                    res,
                    window.name,
                    cur.rowcount,
                )
    return written


# ---------------------------------------------------------------------------
# Time of day: the same ranking, recomputed inside each hour block
# ---------------------------------------------------------------------------

_HOUR_SAFETY_SQL = """
WITH weighted AS (
    SELECT
        i.{h3_column}         AS h3_index,
        i.occurred_local_hour AS hour_block,
        count(*) FILTER (WHERE i.product_category =  'violent') AS n_violent,
        count(*) FILTER (WHERE i.product_category <> 'violent') AS n_non_violent,
        COALESCE(sum(COALESCE(w.weight, 1.0))
                 FILTER (WHERE i.product_category =  'violent'), 0) AS w_violent,
        COALESCE(sum(COALESCE(w.weight, 1.0))
                 FILTER (WHERE i.product_category <> 'violent'), 0) AS w_non_violent
    FROM silver.incident i
    {weight_lookup}
    WHERE i.source_id = %(source_id)s
      AND i.occurred_local_date BETWEEN %(window_start)s AND %(window_end)s
      -- Records the source published without a clock time are absent here
      -- rather than defaulted to midnight (009_time_of_day.sql).
      AND i.occurred_local_hour IS NOT NULL
    GROUP BY 1, 2
),
universe AS (
    -- Every cell exists in every hour block, including the ones with nothing
    -- reported: a cell that was quiet at 3am still sat there being watched, and
    -- dropping it would inflate every other cell's percentile in that block.
    SELECT g.h3_index, g.area_km2, b.hour_block
    FROM gold.cell_geometry g
    CROSS JOIN generate_series(0, %(hour_blocks)s - 1) AS b(hour_block)
    WHERE g.source_id = %(source_id)s AND g.h3_res = %(h3_res)s
),
joined AS (
    SELECT
        u.h3_index, u.area_km2, u.hour_block,
        COALESCE(x.n_violent, 0)     AS n_violent,
        COALESCE(x.n_non_violent, 0) AS n_non_violent,
        COALESCE(x.w_violent, 0)     AS w_violent,
        COALESCE(x.w_non_violent, 0) AS w_non_violent
    FROM universe u
    LEFT JOIN weighted x
           ON x.h3_index = u.h3_index AND x.hour_block = u.hour_block
),
unpivoted AS (
    SELECT h3_index, area_km2, hour_block, track, n, w
    FROM joined
    CROSS JOIN LATERAL (VALUES
        ('violent',     n_violent,     w_violent),
        ('non_violent', n_non_violent, w_non_violent)
    ) AS v(track, n, w)
),
city AS (
    -- Per hour block, not citywide-per-day: the rate a 4am cell is shrunk
    -- toward has to be the 4am city, or the prior would drag every quiet hour
    -- toward a daytime average it has nothing to do with.
    SELECT track, hour_block, sum(w) / NULLIF(sum(area_km2), 0) AS city_rate
    FROM unpivoted
    GROUP BY track, hour_block
),
adjusted AS (
    SELECT
        u.h3_index, u.area_km2, u.hour_block, u.track, u.n, u.w,
        -- Same Poisson-gamma posterior as the all-hours ranking. The prior is
        -- an exposure in km2, so it shrinks by area/(area + prior) regardless
        -- of how much offense the cell carries -- which means slicing the
        -- window into 24 does not quietly change how hard the prior bites.
        (u.w + COALESCE(c.city_rate, 0) * %(eb_prior)s)
            / NULLIF(u.area_km2 + %(eb_prior)s, 0) AS adj
    FROM unpivoted u
    JOIN city c ON c.track = u.track AND c.hour_block = u.hour_block
),
neighbor_mean AS (
    -- Grouped join, where the all-hours build uses a correlated LATERAL. That
    -- form re-scans the adjusted set once per row, which is survivable at
    -- ~8,000 rows and quadratic misery at the 192,000 this produces.
    SELECT nbr.h3_index, x.track, x.hour_block, avg(x.adj) AS mean_adj
    FROM gold.cell_neighbor nbr
    JOIN adjusted x ON x.h3_index = nbr.neighbor_h3
    WHERE nbr.source_id = %(source_id)s AND nbr.h3_res = %(h3_res)s
    GROUP BY 1, 2, 3
),
blended AS (
    SELECT
        a.*,
        CASE WHEN nm.mean_adj IS NULL THEN a.adj
             ELSE %(self_weight)s * a.adj + (1 - %(self_weight)s) * nm.mean_adj
        END AS smoothed
    FROM adjusted a
    LEFT JOIN neighbor_mean nm
           ON nm.h3_index  = a.h3_index
          AND nm.track     = a.track
          AND nm.hour_block = a.hour_block
),
ranked AS (
    SELECT
        b.*,
        -- Ranked within the hour block, so the reference class is "other cells
        -- at this hour" and not the all-hours distribution.
        (
            (rank() OVER (PARTITION BY b.track, b.hour_block ORDER BY b.smoothed DESC)
             + (count(*) OVER (PARTITION BY b.track, b.hour_block, b.smoothed) - 1) / 2.0)
            - 0.5
        ) / count(*) OVER (PARTITION BY b.track, b.hour_block) AS pct,
        rank()   OVER (PARTITION BY b.track, b.hour_block ORDER BY b.smoothed DESC) AS rnk,
        count(*) OVER (PARTITION BY b.track, b.hour_block) AS cell_total,
        -- The cell's own totals across all 24 blocks, for the hour index. Taken
        -- from this layer rather than from gold.cell_safety, because that one
        -- includes the incidents with no published hour and this one cannot.
        sum(b.n) OVER (PARTITION BY b.h3_index, b.track) AS n_all_hours,
        sum(b.w) OVER (PARTITION BY b.h3_index, b.track) AS w_all_hours
    FROM blended b
)
INSERT INTO gold.cell_hour_safety (
    source_id, h3_index, h3_res, time_window, hour_block, track, scheme_version,
    window_start, window_end, incident_count,
    weighted_total, weighted_per_km2, smoothed_per_km2,
    safety_percentile, safety_rank, city_cell_total, safety_tier,
    baseline_percentile, percentile_delta, hour_index, refreshed_at
)
SELECT
    %(source_id)s, r.h3_index, %(h3_res)s, %(time_window)s, r.hour_block,
    r.track, %(scheme)s,
    %(window_start)s, %(window_end)s, r.n,
    r.w, r.w / r.area_km2, r.smoothed,
    r.pct, r.rnk, r.cell_total,
    CASE
        -- Tier 0 means nothing of this track was reported here at this hour.
        -- At an hour's resolution that is the common case, and it is still not
        -- the same claim as being among the safest quarter (S13).
        WHEN r.n = 0      THEN 0
        WHEN r.pct < 0.25 THEN 1
        WHEN r.pct < 0.50 THEN 2
        WHEN r.pct < 0.75 THEN 3
        ELSE 4
    END,
    base.safety_percentile,
    r.pct - base.safety_percentile,
    -- Raw figures, not smoothed: this is a statement about this cell, and
    -- blending in the neighbours would make it partly about them. Withheld
    -- entirely below the evidence floor rather than published as a ratio of
    -- two very small numbers.
    CASE WHEN r.n_all_hours >= %(min_evidence)s
         THEN r.w / NULLIF(r.w_all_hours / %(hour_blocks)s::double precision, 0)
    END,
    now()
FROM ranked r
LEFT JOIN gold.cell_safety base
       ON base.source_id      = %(source_id)s
      AND base.h3_index       = r.h3_index
      AND base.h3_res         = %(h3_res)s
      AND base.time_window    = %(time_window)s
      AND base.track          = r.track
      AND base.scheme_version = %(scheme)s
"""


_HOUR_PROFILE_SQL = """
INSERT INTO gold.cell_hour_profile
    (source_id, h3_index, h3_res, time_window, hour_block, category,
     incident_count, refreshed_at)
SELECT
    %(source_id)s,
    {h3_column},
    %(h3_res)s,
    %(time_window)s,
    occurred_local_hour,
    COALESCE(product_category, 'all'),
    count(*),
    now()
FROM silver.incident
WHERE source_id = %(source_id)s
  AND occurred_local_date BETWEEN %(window_start)s AND %(window_end)s
  AND occurred_local_hour IS NOT NULL
GROUP BY GROUPING SETS (
    ({h3_column}, occurred_local_hour, product_category),
    ({h3_column}, occurred_local_hour)
)
"""


def refresh_cell_hour_safety(
    conn: psycopg.Connection,
    source_id: str,
    windows: list[Window],
    scheme: Scheme,
) -> int:
    """Rebuild gold.cell_hour_safety for one scheme.

    Must run after refresh_cell_safety for the same scheme and windows: the
    second rating is a comparison against the all-hours percentile, which this
    reads rather than recomputes.
    """
    written = 0
    hourly = [w for w in windows if w.name in HOURLY_WINDOWS]
    with conn.cursor() as cur:
        for res in HOURLY_RESOLUTIONS:
            sql = _HOUR_SAFETY_SQL.format(
                h3_column=_h3_column(res), weight_lookup=_WEIGHT_LOOKUP
            )
            for window in hourly:
                cur.execute(
                    """
                    DELETE FROM gold.cell_hour_safety
                    WHERE source_id = %s AND h3_res = %s
                      AND time_window = %s AND scheme_version = %s
                    """,
                    (source_id, res, window.name, scheme.version),
                )
                cur.execute(
                    sql,
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                        "scheme": scheme.version,
                        "eb_prior": scheme.eb_prior_km2,
                        "self_weight": scheme.self_weight,
                        "hour_blocks": HOUR_BLOCKS,
                        "min_evidence": MIN_HOUR_EVIDENCE,
                    },
                )
                written += cur.rowcount
                log.info(
                    "cell_hour_safety scheme=%s res=%s window=%s -> %s rows",
                    scheme.version,
                    res,
                    window.name,
                    cur.rowcount,
                )
    return written


def refresh_cell_hour_profile(
    conn: psycopg.Connection, source_id: str, windows: list[Window]
) -> int:
    """Rebuild the sparse per-cell hourly breakdown behind the detail panel."""
    written = 0
    hourly = [w for w in windows if w.name in HOURLY_WINDOWS]
    with conn.cursor() as cur:
        for res in HOURLY_RESOLUTIONS:
            sql = _HOUR_PROFILE_SQL.format(h3_column=_h3_column(res))
            for window in hourly:
                cur.execute(
                    """
                    DELETE FROM gold.cell_hour_profile
                    WHERE source_id = %s AND h3_res = %s AND time_window = %s
                    """,
                    (source_id, res, window.name),
                )
                cur.execute(
                    sql,
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                    },
                )
                written += cur.rowcount
    log.info("cell_hour_profile -> %s rows", written)
    return written


_HOUR_COVERAGE_SQL = """
SELECT
    count(*)                                                  AS total,
    count(*) FILTER (WHERE occurred_local_hour IS NOT NULL)   AS known
FROM silver.incident
WHERE source_id = %(source_id)s
"""


def hour_coverage(conn: psycopg.Connection, source_id: str) -> float:
    """Share of incidents carrying a published clock hour.

    Surfaced on the city snapshot rather than logged: the hourly view silently
    omits everything this does not cover, and S8.5's rule is that a gap in the
    input is reported, not absorbed.
    """
    with conn.cursor() as cur:
        cur.execute(_HOUR_COVERAGE_SQL, {"source_id": source_id})
        row = cur.fetchone()
    if not row or not row["total"]:
        return 0.0
    return row["known"] / row["total"]


def refresh_hourly_layer(
    conn: psycopg.Connection,
    source_id: str,
    windows: list[Window],
    scheme_version: str | None = None,
) -> tuple[int, int, float]:
    """Build the hourly ranking and profile.

    Returns (safety rows, profile rows, share of incidents with a known hour).
    The share is computed here anyway to decide whether building is worthwhile,
    so it is handed back rather than rescanning silver for it.
    """
    if scheme_version:
        schemes = [get_scheme(conn, scheme_version)]
    else:
        schemes = enabled_schemes(conn)

    if not schemes:
        log.warning("no enabled severity scheme; skipping the hourly ranking")
        return 0, 0, 0.0

    share = hour_coverage(conn, source_id)
    log.info("%.1f%% of incidents carry a published clock hour", share * 100)
    if share == 0:
        log.warning(
            "no incident carries a clock hour; skipping the hourly layers "
            "(run python -m safety.migrate to backfill occurred_local_hour)"
        )
        return 0, 0, share
    if share < 0.90:
        log.warning(
            "%.1f%% of incidents have no published clock hour and are absent "
            "from the hourly layers",
            (1 - share) * 100,
        )

    rows = 0
    for scheme in schemes:
        rows += refresh_cell_hour_safety(conn, source_id, windows, scheme)
    profile_rows = refresh_cell_hour_profile(conn, source_id, windows)
    return rows, profile_rows, share


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
    cell_count_r8, cell_count_r9, cell_count_r10,
    center_lat, center_lng, bbox_west, bbox_south, bbox_east, bbox_north,
    crosswalk_version, pipeline_version, attribution_text, terms_url,
    unmapped_offense_count, rejected_record_count,
    severity_scheme_version, severity_weight_coverage, hour_known_share
)
SELECT
    r.source_id, r.city_name, r.agency_name,
    stats.data_as_of, now(),
    r.expected_cadence, r.publication_lag_days, r.freshness_note,
    stats.incident_count, stats.coverage_start, stats.coverage_end,
    cells.r8, cells.r9, cells.r10,
    ST_Y(ST_Centroid(b.geom)), ST_X(ST_Centroid(b.geom)),
    ST_XMin(b.geom::box2d), ST_YMin(b.geom::box2d),
    ST_XMax(b.geom::box2d), ST_YMax(b.geom::box2d),
    r.crosswalk_version, %(pipeline_version)s, r.attribution_text, r.terms_url,
    COALESCE(quality.unmapped, 0), COALESCE(quality.rejected, 0),
    r.severity_scheme_version, %(weight_coverage)s, %(hour_coverage)s
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
        count(*) FILTER (WHERE h3_res = 8)::int  AS r8,
        count(*) FILTER (WHERE h3_res = 9)::int  AS r9,
        count(*) FILTER (WHERE h3_res = 10)::int AS r10
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
    cell_count_r10         = EXCLUDED.cell_count_r10,
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
    rejected_record_count  = EXCLUDED.rejected_record_count,
    severity_scheme_version  = EXCLUDED.severity_scheme_version,
    -- Keep the last known figure when this run did not rebuild the active
    -- scheme, rather than blanking it.
    severity_weight_coverage = COALESCE(
        EXCLUDED.severity_weight_coverage, city_snapshot.severity_weight_coverage),
    hour_known_share         = COALESCE(
        EXCLUDED.hour_known_share, city_snapshot.hour_known_share)
"""


def refresh_city_snapshot(
    conn: psycopg.Connection,
    source_id: str,
    pipeline_version: str,
    weight_coverage_share: float | None = None,
    hour_coverage_share: float | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _SNAPSHOT_SQL,
            {
                "source_id": source_id,
                "pipeline_version": pipeline_version,
                "weight_coverage": weight_coverage_share,
                "hour_coverage": hour_coverage_share,
            },
        )


def refresh_safety_layer(
    conn: psycopg.Connection,
    source_id: str,
    windows: list[Window],
    scheme_version: str | None = None,
) -> tuple[int, float | None]:
    """Build the safety ranking for one scheme, or for every enabled one.

    Returns the row count and the weight coverage of the source's *active*
    scheme, which is the one the serving layer reads.
    """
    if scheme_version:
        schemes = [get_scheme(conn, scheme_version)]
    else:
        schemes = enabled_schemes(conn)

    if not schemes:
        log.warning(
            "no enabled severity scheme; skipping the safety ranking "
            "(run python -m safety.migrate to load reference/severity)"
        )
        return 0, None

    active = active_scheme(conn, source_id)
    rows = 0
    coverage: float | None = None
    for scheme in schemes:
        rows += refresh_cell_safety(conn, source_id, windows, scheme)
        share = weight_coverage(conn, source_id, scheme.version)
        log.info(
            "severity weights for scheme %s: %.1f%% of incidents carry a published figure",
            scheme.version,
            share * 100,
        )
        if share < 0.95:
            log.warning(
                "scheme %s falls back to a derived weight for %.1f%% of incidents; "
                "the ranking is closer to the coarse UCR bucket than to the published scale",
                scheme.version,
                (1 - share) * 100,
            )
        # Only the scheme this city actually serves belongs in the snapshot.
        # Building a candidate scheme must not restate the live figure.
        if scheme.version == active or (active is None and len(schemes) == 1):
            coverage = share
    return rows, coverage


def refresh_all(
    conn: psycopg.Connection, source_id: str, pipeline_version: str
) -> dict[str, Any]:
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
    safety_rows, coverage = refresh_safety_layer(conn, source_id, windows)
    # After the all-hours ranking, never before: the hourly layer's second
    # rating is a comparison against the percentile that one produces.
    hour_rows, hour_profile_rows, hour_share = refresh_hourly_layer(
        conn, source_id, windows
    )
    monthly_rows, mix_rows = refresh_cell_detail(conn, source_id, windows)
    refresh_city_snapshot(conn, source_id, pipeline_version, coverage, hour_share)
    conn.commit()

    return {
        "cells_r8": cells.get(8, 0),
        "cells_r9": cells.get(9, 0),
        "cells_r10": cells.get(10, 0),
        "cell_activity_rows": activity_rows,
        "cell_safety_rows": safety_rows,
        "cell_hour_safety_rows": hour_rows,
        "cell_hour_profile_rows": hour_profile_rows,
        "severity_weight_coverage": round(coverage, 4) if coverage is not None else None,
        "cell_monthly_rows": monthly_rows,
        "cell_offense_mix_rows": mix_rows,
    }
