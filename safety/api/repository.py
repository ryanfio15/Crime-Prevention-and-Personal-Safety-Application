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
VALID_RESOLUTIONS = (8, 9, 10)
VALID_WINDOWS = ("last_30d", "last_90d", "last_12m", "last_24m")
VALID_CATEGORIES = ("all", "violent", "property", "quality_of_life", "other")
VALID_HOURS = tuple(range(24))

# Mirrors safety.etl.gold.HOURLY_RESOLUTIONS / HOURLY_WINDOWS. Duplicated rather
# than imported so the serving layer keeps no dependency on the ETL package, and
# asserted against the ETL by safety/api/main.py's error text: a request for an
# hour outside this scope gets told why, not an empty map.
HOURLY_RESOLUTIONS = (8, 9)
HOURLY_WINDOWS = ("last_12m", "last_24m")

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

TRACK_LABELS = {
    "violent": "Violent offenses",
    "non_violent": "Non-violent offenses",
}

# Quartiles, not quintiles: the validated colour ramp for this measure carries
# four steps, so the data and the ramp agree rather than one being squeezed into
# the other. Tier 0 stays a separate state for the same reason it does on the
# activity layer -- no reports is not a claim about safety.
SAFETY_TIER_LABELS = {
    0: "No reported incidents",
    1: "Least safe quarter",
    2: "Lower-middle quarter",
    3: "Upper-middle quarter",
    4: "Safest quarter",
}


def hour_label(hour: int) -> str:
    """'20:00-21:00'. The last block reads 23:00-24:00, not 23:00-00:00."""
    return f"{hour:02d}:00–{hour + 1:02d}:00"


# Rating 2 banded into words, in percentile points, so the measure is never
# carried by colour alone. The bands are wide on purpose: an hourly percentile
# is a much noisier figure than the all-hours one it is being compared against,
# and narrow bands would present that noise as movement.
DELTA_BANDS = (
    (-0.15, "Much worse here than usual"),
    (-0.05, "Worse here than usual"),
    (0.05, "Typical for this cell"),
    (0.15, "Better here than usual"),
)
DELTA_TOP_LABEL = "Much better here than usual"


# Mirrors safety.etl.gold.MIN_HOUR_EVIDENCE. Below this many incidents across
# the whole window, one hour against a twenty-fourth of a tiny total is a ratio
# of two very small numbers: three incidents all year with one of them at 2pm
# reads as 800% of an average hour, which is arithmetic, not evidence.
MIN_HOUR_EVIDENCE = 12

HOUR_BLOCKS = 24


def hour_relative(by_hour: list[dict[str, Any]], hour: int | None) -> dict[str, Any] | None:
    """This cell's incidents at one hour against its own average hour.

    A percentage where 100% is an ordinary hour *for this cell*: 250% is two and
    a half times its own average, 40% is well below it. The comparison is the
    cell against its own day, not against other cells -- the only one that
    answers "is this hour unusual here".

    Counts, not severity weights, and all categories: this is a statement about
    how much gets reported, which is what the figure claims to be.
    """
    if hour is None:
        return None

    counts = [0] * HOUR_BLOCKS
    for row in by_hour:
        if row["category"] == "all":
            counts[row["hour_block"]] = row["incident_count"]
    total = sum(counts)

    if total < MIN_HOUR_EVIDENCE:
        return {
            "hour_count": counts[hour],
            "day_total": total,
            "mean_per_hour": None,
            "percent_of_average": None,
            "enough_evidence": False,
        }

    mean = total / HOUR_BLOCKS
    return {
        "hour_count": counts[hour],
        "day_total": total,
        "mean_per_hour": round(mean, 2),
        # Rounded to whole percent: the input is a count of a few dozen
        # incidents, and a decimal place would imply precision it has not got.
        "percent_of_average": round(counts[hour] / mean * 100),
        "enough_evidence": True,
    }


def delta_label(delta: float | None) -> str | None:
    if delta is None:
        return None
    for threshold, label in DELTA_BANDS:
        if delta < threshold:
            return label
    return DELTA_TOP_LABEL


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


def severity_scheme(conn: psycopg.Connection, source_id: str) -> dict[str, Any] | None:
    """The scheme this city's safety ranking was built with, for methodology."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.scheme_version, s.description, s.source_citation,
                   s.eb_prior_km2, s.self_weight
            FROM reference.severity_scheme s
            JOIN reference.source_registry r
              ON r.severity_scheme_version = s.scheme_version
            WHERE r.source_id = %s
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
WITH scheme AS (
    -- A scalar subquery rather than a filtered SELECT, so this always yields
    -- exactly one row: an empty CTE here would take the CROSS JOIN below to
    -- zero rows and blank the entire map layer.
    SELECT (
        SELECT severity_scheme_version
        FROM reference.source_registry
        WHERE source_id = %(source_id)s
    ) AS version
),
layer AS (
    -- Both safety tracks ride along on every feature, so switching the map
    -- between violent and non-violent is a repaint rather than a request.
    -- LEFT, so the map still renders if the safety layer has not been built.
    SELECT a.h3_index, a.incident_count, a.incidents_per_km2, a.percentile,
           a.activity_tier, a.city_rank, a.city_cell_total,
           a.window_start, a.window_end, g.boundary,
           sv.safety_percentile AS safety_violent,
           sv.safety_tier       AS tier_violent,
           sv.weighted_total    AS weighted_violent,
           sn.safety_percentile AS safety_nonviolent,
           sn.safety_tier       AS tier_nonviolent,
           sn.weighted_total    AS weighted_nonviolent,
           -- Time of day. All NULL unless the request named an hour; unlike the
           -- two tracks these cannot ride along, because carrying all 24 blocks
           -- on every feature multiplies the payload by 24.
           hv.safety_percentile AS hsafety_violent,
           hv.safety_tier       AS htier_violent,
           hv.percentile_delta  AS hdelta_violent,
           hv.incident_count    AS hcount_violent,
           hn.safety_percentile AS hsafety_nonviolent,
           hn.safety_tier       AS htier_nonviolent,
           hn.percentile_delta  AS hdelta_nonviolent,
           hn.incident_count    AS hcount_nonviolent
    FROM gold.cell_activity a
    JOIN gold.cell_geometry g ON g.h3_index = a.h3_index
    CROSS JOIN scheme
    LEFT JOIN gold.cell_safety sv
           ON sv.source_id      = a.source_id
          AND sv.h3_index       = a.h3_index
          AND sv.h3_res         = a.h3_res
          AND sv.time_window    = a.time_window
          AND sv.scheme_version = scheme.version
          AND sv.track          = 'violent'
    LEFT JOIN gold.cell_safety sn
           ON sn.source_id      = a.source_id
          AND sn.h3_index       = a.h3_index
          AND sn.h3_res         = a.h3_res
          AND sn.time_window    = a.time_window
          AND sn.scheme_version = scheme.version
          AND sn.track          = 'non_violent'
    -- The hour predicate sits first so a request with no hour never probes the
    -- index at all; both joins then contribute nothing and every h* column is
    -- NULL, which is exactly what the all-hours view wants.
    LEFT JOIN gold.cell_hour_safety hv
           ON %(hour_block)s::smallint IS NOT NULL
          AND hv.source_id      = a.source_id
          AND hv.h3_index       = a.h3_index
          AND hv.h3_res         = a.h3_res
          AND hv.time_window    = a.time_window
          AND hv.hour_block     = %(hour_block)s::smallint
          AND hv.scheme_version = scheme.version
          AND hv.track          = 'violent'
    LEFT JOIN gold.cell_hour_safety hn
           ON %(hour_block)s::smallint IS NOT NULL
          AND hn.source_id      = a.source_id
          AND hn.h3_index       = a.h3_index
          AND hn.h3_res         = a.h3_res
          AND hn.time_window    = a.time_window
          AND hn.hour_block     = %(hour_block)s::smallint
          AND hn.scheme_version = scheme.version
          AND hn.track          = 'non_violent'
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
        'hour_block',  %(hour_block)s::smallint,
        -- Null or zero means the hourly rollup has never been built. The
        -- client needs that as a distinct state: without it an unbuilt layer
        -- and a genuinely quiet cell both render as zero.
        'hour_known_share', (
            SELECT hour_known_share FROM gold.city_snapshot
            WHERE source_id = %(source_id)s
        ),
        'severity_scheme', (SELECT version FROM scheme),
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
                    'of_cells',   l.city_cell_total,
                    -- 1.0 = safest cell in the city on that track.
                    'safety_violent',    round(l.safety_violent::numeric, 4),
                    'stier_violent',     l.tier_violent,
                    'sw_violent',        round(l.weighted_violent::numeric, 1),
                    'safety_nonviolent', round(l.safety_nonviolent::numeric, 4),
                    'stier_nonviolent',  l.tier_nonviolent,
                    'sw_nonviolent',     round(l.weighted_nonviolent::numeric, 1),
                    -- Rating 1 at the requested hour, then rating 2: how that
                    -- differs from the cell's all-hours standing, and the plain
                    -- ratio against its own average hour.
                    'hsafety_violent',    round(l.hsafety_violent::numeric, 4),
                    'hstier_violent',     l.htier_violent,
                    'hdelta_violent',     round(l.hdelta_violent::numeric, 4),
                    'hsafety_nonviolent', round(l.hsafety_nonviolent::numeric, 4),
                    'hstier_nonviolent',  l.htier_nonviolent,
                    'hdelta_nonviolent',  round(l.hdelta_nonviolent::numeric, 4),
                    -- Incidents in this cell during this hour block, both
                    -- tracks. Always at or below `count`: the ones the source
                    -- published with no clock time are not in any hour.
                    'hcount', CASE
                        WHEN l.hcount_violent IS NULL AND l.hcount_nonviolent IS NULL
                            THEN NULL
                        ELSE COALESCE(l.hcount_violent, 0)
                           + COALESCE(l.hcount_nonviolent, 0)
                    END
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
    hour: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    params = {
        "source_id": source_id,
        "h3_res": h3_res,
        "time_window": time_window,
        "category": category,
        "hour_block": hour,
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
    conn: psycopg.Connection,
    *,
    h3_index: str,
    time_window: str,
    hour: int | None = None,
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

        cur.execute(
            """
            SELECT s.track, s.safety_percentile, s.safety_rank, s.city_cell_total,
                   s.safety_tier, s.incident_count, s.weighted_total, s.weighted_per_km2, s.scheme_version
            FROM gold.cell_safety s
            JOIN reference.source_registry r
              ON r.source_id = s.source_id
             AND r.severity_scheme_version = s.scheme_version
            WHERE s.h3_index = %s AND s.time_window = %s
            ORDER BY CASE s.track WHEN 'violent' THEN 0 ELSE 1 END
            """,
            (h3_index, time_window),
        )
        safety = cur.fetchall()

        # The whole 24-block shape, not just the selected hour: the panel draws
        # the profile so the selected block can be read against the rest of the
        # day rather than as a bare number.
        cur.execute(
            """
            SELECT hour_block, category, incident_count
            FROM gold.cell_hour_profile
            WHERE h3_index = %s AND time_window = %s
            ORDER BY hour_block, category
            """,
            (h3_index, time_window),
        )
        by_hour = cur.fetchall()

        hour_safety: list[dict[str, Any]] = []
        if hour is not None:
            cur.execute(
                """
                SELECT s.track, s.hour_block, s.safety_percentile, s.safety_tier,
                       s.safety_rank, s.city_cell_total, s.incident_count,
                       s.baseline_percentile, s.percentile_delta, s.hour_index
                FROM gold.cell_hour_safety s
                JOIN reference.source_registry r
                  ON r.source_id = s.source_id
                 AND r.severity_scheme_version = s.scheme_version
                WHERE s.h3_index = %s AND s.time_window = %s AND s.hour_block = %s
                ORDER BY CASE s.track WHEN 'violent' THEN 0 ELSE 1 END
                """,
                (h3_index, time_window, hour),
            )
            hour_safety = cur.fetchall()

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
        "safety": [
            {
                **row,
                "track_label": TRACK_LABELS.get(row["track"], row["track"]),
                "tier_label": SAFETY_TIER_LABELS.get(row["safety_tier"]),
            }
            for row in safety
        ],
        "hour": hour,
        "hour_label": hour_label(hour) if hour is not None else None,
        "by_hour": by_hour,
        "hour_relative": hour_relative(by_hour, hour),
        "hour_safety": [
            {
                **row,
                "track_label": TRACK_LABELS.get(row["track"], row["track"]),
                "tier_label": SAFETY_TIER_LABELS.get(row["safety_tier"]),
                "delta_label": delta_label(row["percentile_delta"]),
            }
            for row in hour_safety
        ],
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
