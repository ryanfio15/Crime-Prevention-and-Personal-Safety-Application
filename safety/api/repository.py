"""Every query the serving layer runs (design doc S9.3, S9.4).

Two rules hold throughout this module:

1. Reads touch the gold schema only. The read path never scans silver, and
   never aggregates raw incident rows -- that work is already done by the ETL.
2. Map payloads are assembled into GeoJSON by PostgreSQL itself, so a
   3,600-hexagon layer crosses the wire as one JSON document instead of being
   rebuilt object-by-object in Python.
"""

from __future__ import annotations

from typing import Any

import psycopg

# Whitelists. These bound every value that reaches SQL through anything other
# than a bound parameter.
VALID_RESOLUTIONS = (8, 9)
VALID_WINDOWS = ("last_30d", "last_90d", "last_12m", "last_24m")
VALID_CATEGORIES = ("all", "violent", "property", "quality_of_life", "other")

WINDOW_LABELS = {
    "last_30d": "Last 30 days",
    "last_90d": "Last 90 days",
    "last_12m": "Last 12 months",
    "last_24m": "Last 24 months",
}

CATEGORY_LABELS = {
    "all": "All reported incidents",
    "violent": "Violent",
    "property": "Property",
    "quality_of_life": "Quality of life",
    "other": "Other",
}

TIER_LABELS = {
    0: "No reported incidents",
    1: "Lowest fifth",
    2: "Lower-middle fifth",
    3: "Middle fifth",
    4: "Upper-middle fifth",
    5: "Highest fifth",
}


# ---------------------------------------------------------------------------
# Cities and metadata
# ---------------------------------------------------------------------------


def list_cities(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.*, r.location_precision_note, r.enabled
            FROM gold.city_snapshot s
            JOIN reference.source_registry r USING (source_id)
            ORDER BY s.city_name
            """
        )
        return cur.fetchall()


def get_city(conn: psycopg.Connection, source_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.*, r.location_precision_note, r.enabled
            FROM gold.city_snapshot s
            JOIN reference.source_registry r USING (source_id)
            WHERE s.source_id = %s
            """,
            (source_id,),
        )
        return cur.fetchone()


def serving_version(conn: psycopg.Connection) -> dict[str, Any]:
    """Cheap poll target so a client can notice an ETL refresh and reload."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max(last_refreshed_at) AS last_refreshed_at,
                   max(data_as_of)        AS data_as_of,
                   sum(incident_count)    AS incident_count
            FROM gold.city_snapshot
            """
        )
        return cur.fetchone() or {}


def categories(conn: psycopg.Connection, source_id: str) -> list[dict[str, Any]]:
    """Product categories with the NIBRS detail sitting underneath each one.

    S7.4: the raw source classification travels alongside the mapped one, so a
    user can always see what the city actually called an incident.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.product_category,
                count(*)                                        AS mapping_count,
                jsonb_agg(
                    jsonb_build_object(
                        'raw_offense_code',   c.raw_offense_code,
                        'raw_offense_text',   c.raw_offense_text,
                        'nibrs_code',         c.nibrs_code,
                        'nibrs_offense_name', c.nibrs_offense_name,
                        'nibrs_group',        c.nibrs_group,
                        'ucr_part',           c.ucr_part,
                        'severity_bucket',    c.severity_bucket,
                        'mapping_confidence', c.mapping_confidence,
                        'notes',              c.notes
                    ) ORDER BY c.raw_offense_code, c.raw_offense_text
                ) AS offenses
            FROM reference.offense_crosswalk c
            JOIN reference.source_registry r
              ON r.source_id = c.source_id AND r.crosswalk_version = c.crosswalk_version
            WHERE c.source_id = %s
            GROUP BY c.product_category
            ORDER BY c.product_category
            """,
            (source_id,),
        )
        return cur.fetchall()


def data_quality(conn: psycopg.Connection, source_id: str) -> dict[str, Any]:
    """S8.5 findings, surfaced rather than buried in a log file."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT check_name, severity, sum(occurrences)::int AS occurrences,
                   max(detected_at) AS last_seen
            FROM etl.validation_issue
            WHERE source_id = %s
            GROUP BY check_name, severity
            ORDER BY occurrences DESC
            """,
            (source_id,),
        )
        issues = cur.fetchall()

        cur.execute(
            """
            SELECT pull_id, mode, status, records_fetched, records_rejected,
                   records_upserted, duration_seconds, started_at, finished_at, bronze_uri
            FROM etl.pull_run
            WHERE source_id = %s
            ORDER BY pull_id DESC
            LIMIT 5
            """,
            (source_id,),
        )
        pulls = cur.fetchall()

        cur.execute(
            """
            SELECT coordinate_source, count(*)::int AS n
            FROM silver.incident WHERE source_id = %s
            GROUP BY 1 ORDER BY n DESC
            """,
            (source_id,),
        )
        coordinates = cur.fetchall()

        cur.execute(
            """
            SELECT mapping_confidence, count(*)::int AS n
            FROM silver.incident WHERE source_id = %s
            GROUP BY 1 ORDER BY n DESC
            """,
            (source_id,),
        )
        mappings = cur.fetchall()

    return {
        "validation_issues": issues,
        "recent_pulls": pulls,
        "coordinate_provenance": coordinates,
        "offense_mapping_confidence": mappings,
    }


# ---------------------------------------------------------------------------
# The map layer
# ---------------------------------------------------------------------------

_CELLS_GEOJSON_SQL = """
WITH layer AS (
    SELECT a.h3_index, a.incident_count, a.incidents_per_km2, a.percentile,
           a.activity_tier, a.city_rank, a.city_cell_total,
           a.window_start, a.window_end, g.boundary
    FROM gold.cell_activity a
    JOIN gold.cell_geometry g ON g.h3_index = a.h3_index
    WHERE a.source_id   = %(source_id)s
      AND a.h3_res      = %(h3_res)s
      AND a.time_window = %(time_window)s
      AND a.category    = %(category)s
      AND a.incident_count >= %(min_count)s
      AND (
            %(bbox)s::text IS NULL
            OR g.boundary && ST_MakeEnvelope(
                   %(west)s, %(south)s, %(east)s, %(north)s, 4326)
          )
),
scale AS (
    -- Colour-ramp domain, derived from the data actually being returned so the
    -- legend and the fill stops can never disagree.
    SELECT
        COALESCE(max(incident_count), 0)  AS max_count,
        COALESCE(min(incident_count), 0)  AS min_count,
        COALESCE(sum(incident_count), 0)  AS total_count,
        count(*)                          AS cell_count,
        COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY incident_count), 0) AS p50,
        COALESCE(percentile_cont(0.80) WITHIN GROUP (ORDER BY incident_count), 0) AS p80,
        COALESCE(percentile_cont(0.90) WITHIN GROUP (ORDER BY incident_count), 0) AS p90,
        COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY incident_count), 0) AS p95,
        COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY incident_count), 0) AS p99,
        min(window_start) AS window_start,
        max(window_end)   AS window_end
    FROM layer
)
SELECT jsonb_build_object(
    'type', 'FeatureCollection',
    'metadata', jsonb_build_object(
        'source_id',   %(source_id)s,
        'h3_res',      %(h3_res)s,
        'time_window', %(time_window)s,
        'category',    %(category)s,
        'window_start', scale.window_start,
        'window_end',   scale.window_end,
        'cell_count',   scale.cell_count,
        'total_count',  scale.total_count,
        'min_count',    scale.min_count,
        'max_count',    scale.max_count,
        'breaks', jsonb_build_object(
            'p50', scale.p50, 'p80', scale.p80, 'p90', scale.p90,
            'p95', scale.p95, 'p99', scale.p99
        )
    ),
    'features', COALESCE((
        SELECT jsonb_agg(
            jsonb_build_object(
                'type', 'Feature',
                'id', l.h3_index,
                'geometry', ST_AsGeoJSON(l.boundary)::jsonb,
                'properties', jsonb_build_object(
                    'h3',         l.h3_index,
                    'count',      l.incident_count,
                    'per_km2',    round(l.incidents_per_km2::numeric, 1),
                    'percentile', round(l.percentile::numeric, 4),
                    'tier',       l.activity_tier,
                    'rank',       l.city_rank,
                    'of_cells',   l.city_cell_total
                )
            ) ORDER BY l.h3_index
        ) FROM layer l
    ), '[]'::jsonb)
) AS document
FROM scale
"""


def cells_geojson(
    conn: psycopg.Connection,
    *,
    source_id: str,
    h3_res: int,
    time_window: str,
    category: str,
    min_count: int = 0,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    params = {
        "source_id": source_id,
        "h3_res": h3_res,
        "time_window": time_window,
        "category": category,
        "min_count": min_count,
        "bbox": "set" if bbox else None,
        "west": bbox[0] if bbox else None,
        "south": bbox[1] if bbox else None,
        "east": bbox[2] if bbox else None,
        "north": bbox[3] if bbox else None,
    }
    with conn.cursor() as cur:
        cur.execute(_CELLS_GEOJSON_SQL, params)
        row = cur.fetchone()
    return row["document"] if row else {"type": "FeatureCollection", "features": []}


# ---------------------------------------------------------------------------
# Single-cell detail
# ---------------------------------------------------------------------------


def cell_detail(
    conn: psycopg.Connection, *, h3_index: str, time_window: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.h3_index, g.source_id, g.h3_res, g.area_km2,
                   ST_Y(g.centroid) AS lat, ST_X(g.centroid) AS lng,
                   ST_AsGeoJSON(g.boundary)::jsonb AS geometry
            FROM gold.cell_geometry g
            WHERE g.h3_index = %s
            """,
            (h3_index,),
        )
        cell = cur.fetchone()
        if cell is None:
            return None

        cur.execute(
            """
            SELECT category, incident_count, incidents_per_km2, percentile,
                   activity_tier, city_rank, city_cell_total, window_start, window_end
            FROM gold.cell_activity
            WHERE h3_index = %s AND time_window = %s
            ORDER BY CASE category WHEN 'all' THEN 0 ELSE 1 END, incident_count DESC
            """,
            (h3_index, time_window),
        )
        activity = cur.fetchall()

        cur.execute(
            """
            SELECT month_start, category, incident_count
            FROM gold.cell_monthly
            WHERE h3_index = %s
            ORDER BY month_start
            """,
            (h3_index,),
        )
        monthly = cur.fetchall()

        cur.execute(
            """
            SELECT rank, raw_offense_text, nibrs_code, product_category, incident_count
            FROM gold.cell_offense_mix
            WHERE h3_index = %s AND time_window = %s
            ORDER BY rank
            """,
            (h3_index, time_window),
        )
        offenses = cur.fetchall()

    headline = next((row for row in activity if row["category"] == "all"), None)
    return {
        "cell": cell,
        "time_window": time_window,
        "window_label": WINDOW_LABELS.get(time_window, time_window),
        "headline": headline,
        "tier_label": TIER_LABELS.get(headline["activity_tier"]) if headline else None,
        "by_category": [row for row in activity if row["category"] != "all"],
        "monthly": monthly,
        "top_offenses": offenses,
    }


def cell_ring(
    conn: psycopg.Connection,
    *,
    h3_indexes: list[str],
    time_window: str,
    category: str,
) -> list[dict[str, Any]]:
    """Indexed key lookup for a set of cells -- the S10 read pattern.

    The caller computes the k-ring locally with an H3 library; the server only
    does a primary-key fetch, never a spatial search.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.h3_index, a.incident_count, a.incidents_per_km2, a.percentile,
                   a.activity_tier, a.city_rank, a.city_cell_total,
                   ST_Y(g.centroid) AS lat, ST_X(g.centroid) AS lng
            FROM gold.cell_activity a
            JOIN gold.cell_geometry g ON g.h3_index = a.h3_index
            WHERE a.h3_index = ANY(%s) AND a.time_window = %s AND a.category = %s
            ORDER BY a.incident_count DESC
            """,
            (h3_indexes, time_window, category),
        )
        return cur.fetchall()


def city_totals(
    conn: psycopg.Connection, *, source_id: str, time_window: str, h3_res: int
) -> list[dict[str, Any]]:
    """Citywide totals per category, for the legend and the summary panel."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, sum(incident_count)::int AS incident_count,
                   min(window_start) AS window_start, max(window_end) AS window_end
            FROM gold.cell_activity
            WHERE source_id = %s AND time_window = %s AND h3_res = %s
            GROUP BY category
            ORDER BY CASE category WHEN 'all' THEN 0 ELSE 1 END, incident_count DESC
            """,
            (source_id, time_window, h3_res),
        )
        return cur.fetchall()
