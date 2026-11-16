"""Every query the serving layer runs (design doc S9.3, S9.4).

Two rules hold throughout this module:

1. Reads touch the gold schema only. The read path never scans silver, and
   never aggregates raw incident rows -- that work is already done by the ETL.
   The map serves one window, the last year, and nothing else.
2. Map payloads are assembled into GeoJSON by PostgreSQL itself, so a
   3,600-hexagon layer crosses the wire as one JSON document instead of being
   rebuilt object-by-object in Python.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

# Whitelists. These bound every value that reaches SQL through anything other
# than a bound parameter.
VALID_RESOLUTIONS = (8, 9)
# The one window there is (safety.etl.gold.WINDOW).
DEFAULT_WINDOW = "last_1y"
VALID_HOURS = tuple(range(24))
# 0 = Monday ... 6 = Sunday (gold.cell_dow_factor, 022_day_of_week.sql).
VALID_DAYS = tuple(range(7))

# Mirrors safety.etl.gold.HOURLY_RESOLUTIONS / HOURLY_WINDOWS. Duplicated rather
# than imported so the serving layer keeps no dependency on the ETL package, and
# asserted against the ETL by safety/api/main.py's error text: a request for an
# hour outside this scope gets told why, not an empty map.
HOURLY_RESOLUTIONS = (8, 9)
# One window. gold.cell_hour_safety is the only layer multiplied by 24 and was
# the largest table in the database; a second year mostly restates the first,
# since the hourly distribution is already stable at a year wide.
HOURLY_WINDOWS = ("last_1y",)

# Mirrors safety.etl.gold.SAFETY_RESOLUTIONS, same duplication rationale.
SAFETY_RESOLUTIONS = (8, 9)


def window_label(name: str) -> str:
    """'Last 12 months' (not "last year", which reads as the previous calendar year)."""
    return "Last 12 months" if name == DEFAULT_WINDOW else name


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


_CITY_WINDOWS_SQL = """
SELECT
    w.time_window, w.window_start, w.window_end, w.data_start, w.partial,
    w.safety_built, w.hourly_built, w.res10_built,
    ARRAY(
        SELECT c.caveat_text
        FROM reference.source_series_caveat c
        WHERE c.source_id   = w.source_id
          AND c.period_from <= w.window_end
          AND c.period_to   >  w.data_start
        ORDER BY c.period_from
    ) AS caveats
FROM gold.city_window w
WHERE w.source_id = %s
ORDER BY w.sort_order
"""


def city_windows(conn: psycopg.Connection, source_id: str) -> list[dict[str, Any]]:
    """The window this city is served for (one, or none before its first build).

    Plain types only (ISO date strings), so the result can ride the API's
    stamped JSON cache.
    """
    with conn.cursor() as cur:
        cur.execute(_CITY_WINDOWS_SQL, (source_id,))
        rows = cur.fetchall()
    return [
        {
            "id": r["time_window"],
            "label": window_label(r["time_window"]),
            "start": r["window_start"].isoformat(),
            "end": r["window_end"].isoformat(),
            "data_start": r["data_start"].isoformat(),
            "span_days": (r["window_end"] - r["window_start"]).days + 1,
            "partial": r["partial"],
            "safety": r["safety_built"],
            "hourly": r["hourly_built"],
            "caveats": list(r["caveats"]),
        }
        for r in rows
        if r["time_window"] == DEFAULT_WINDOW
    ]


def match_window(requested: str, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The served window named `requested`, or None."""
    return next((w for w in windows if w["id"] == requested), None)


def cell_source(conn: psycopg.Connection, h3_index: str) -> str | None:
    """Which city a cell belongs to (gold.cell_geometry is keyed by the index)."""
    with conn.cursor() as cur:
        cur.execute("SELECT source_id FROM gold.cell_geometry WHERE h3_index = %s", (h3_index,))
        row = cur.fetchone()
    return row["source_id"] if row else None


def cells_exist(conn: psycopg.Connection, source_id: str, cells: list[str]) -> set[str]:
    """Which of `cells` are in `source_id`'s served cell universe.

    The geocoder's second filter (safety/api/directions.py): a city's bounding
    box is a rectangle and takes in ground the city does not cover -- Mercer
    Island sits inside Seattle's -- while gold.cell_geometry holds exactly the
    cells the map draws. A primary-key probe per cell; callers pass a handful.
    """
    if not cells:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h3_index FROM gold.cell_geometry WHERE source_id = %s AND h3_index = ANY(%s)",
            (source_id, list(cells)),
        )
        return {row["h3_index"] for row in cur.fetchall()}


# The scheme the map's safety layer is joined on (`scheme` in
# _CELLS_GEOJSON_SQL): route scoring reads the same rows the map paints.
_SERVED_SCHEME = (
    "(SELECT severity_scheme_version FROM reference.source_registry WHERE source_id = %(source_id)s)"
)


def route_baseline(
    conn: psycopg.Connection,
    source_id: str,
    h3_res: int,
    time_window: str,
    hourly: bool,
    days: Sequence[int] = (),
) -> list[dict[str, Any]]:
    """The city averages a route is compared against, per track and hour block.

    valueDomain (web/app.js) run in SQL over the stored layer: the
    exposure-weighted mean of the smoothed rate across cells with something of
    the track reported, plus the plain mean and the counts for the fallback
    when no cell carries a population figure. hour_block is NULL for the
    all-hours row. The hourly rows take each cell's exposure from its
    all-hours row, which is where the map reads it from too. Plain types, so
    the result can ride the stamped JSON cache (safety/api/directions.py).

    With `days` (0 = Monday), also one row per track, hour block and weekday
    in it: the same average over the hour-block rate times each cell's day
    factor (022), as the map's `timed` layer paints it. dow is NULL on every
    other row. A weekday whose factor is not built yet has no rows.
    """
    params = {
        "source_id": source_id,
        "h3_res": h3_res,
        "time_window": time_window,
        "days": list(days),
    }
    hourly_sql = f"""
        UNION ALL
        SELECT h.track, h.hour_block, NULL::smallint AS dow,
               sum(h.smoothed_per_km2 * s.exposure)
                   FILTER (WHERE h.safety_tier <> 0 AND s.exposure > 0),
               sum(s.exposure) FILTER (WHERE h.safety_tier <> 0 AND s.exposure > 0),
               sum(h.smoothed_per_km2) FILTER (WHERE h.safety_tier <> 0),
               count(*) FILTER (WHERE h.safety_tier <> 0)
        FROM gold.cell_hour_safety h
        LEFT JOIN gold.cell_safety s
               ON s.source_id = h.source_id AND s.h3_index = h.h3_index
              AND s.h3_res = h.h3_res AND s.time_window = h.time_window
              AND s.track = h.track AND s.scheme_version = h.scheme_version
        WHERE h.source_id = %(source_id)s AND h.h3_res = %(h3_res)s
          AND h.time_window = %(time_window)s AND h.scheme_version = {_SERVED_SCHEME}
        GROUP BY h.track, h.hour_block
    """
    daily_sql = f"""
        UNION ALL
        SELECT h.track, h.hour_block, d.dow,
               sum(h.smoothed_per_km2 * d.day_factor * s.exposure)
                   FILTER (WHERE h.safety_tier <> 0 AND s.exposure > 0),
               sum(s.exposure) FILTER (WHERE h.safety_tier <> 0 AND s.exposure > 0),
               sum(h.smoothed_per_km2 * d.day_factor) FILTER (WHERE h.safety_tier <> 0),
               count(*) FILTER (WHERE h.safety_tier <> 0)
        FROM gold.cell_hour_safety h
        JOIN gold.cell_dow_factor d
          ON d.source_id = h.source_id AND d.h3_res = h.h3_res
         AND d.time_window = h.time_window AND d.scheme_version = h.scheme_version
         AND d.track = h.track AND d.h3_index = h.h3_index
         AND d.dow = ANY(%(days)s::smallint[])
        LEFT JOIN gold.cell_safety s
               ON s.source_id = h.source_id AND s.h3_index = h.h3_index
              AND s.h3_res = h.h3_res AND s.time_window = h.time_window
              AND s.track = h.track AND s.scheme_version = h.scheme_version
        WHERE h.source_id = %(source_id)s AND h.h3_res = %(h3_res)s
          AND h.time_window = %(time_window)s AND h.scheme_version = {_SERVED_SCHEME}
        GROUP BY h.track, h.hour_block, d.dow
    """
    sql = f"""
        SELECT track, NULL::smallint AS hour_block, NULL::smallint AS dow,
               sum(smoothed_per_km2 * exposure)
                   FILTER (WHERE safety_tier <> 0 AND exposure > 0) AS weighted,
               sum(exposure) FILTER (WHERE safety_tier <> 0 AND exposure > 0) AS people,
               sum(smoothed_per_km2) FILTER (WHERE safety_tier <> 0) AS total,
               count(*) FILTER (WHERE safety_tier <> 0) AS painted
        FROM gold.cell_safety
        WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
          AND time_window = %(time_window)s AND scheme_version = {_SERVED_SCHEME}
        GROUP BY track
        {hourly_sql if hourly else ""}
        {daily_sql if hourly and days else ""}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [
            {
                "track": r["track"],
                "hour_block": r["hour_block"],
                "dow": r["dow"],
                "weighted": float(r["weighted"] or 0),
                "people": float(r["people"] or 0),
                "total": float(r["total"] or 0),
                "painted": int(r["painted"] or 0),
            }
            for r in cur.fetchall()
        ]


def route_cells(
    conn: psycopg.Connection,
    source_id: str,
    h3_res: int,
    time_window: str,
    cells: list[str],
    hour_blocks: list[int],
    days: Sequence[int] = (),
) -> dict[str, dict[str, dict[str, Any]]]:
    """The safety rows for the cells a route crosses: {track: {h3: row}}.

    Each row carries `sm` and `tier` (all hours) and, for each hour block in
    `hour_blocks`, `hsm.<h>` / `htier.<h>` -- the same properties a map
    feature carries, under the names the endpoint maps onto
    scoring.cell_ratio -- and, for each weekday in `days`, `df.<d>`, the
    cell's day factor (022). A cell with no row is absent, and is unrated on the
    route as it is unpainted on the map. Two index probes per cell list; a
    route crosses tens to a few hundred.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {"violent": {}, "non_violent": {}}
    if not cells:
        return out
    params = {
        "source_id": source_id,
        "h3_res": h3_res,
        "time_window": time_window,
        "cells": list(cells),
        "hours": list(hour_blocks),
        "days": list(days),
    }
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT h3_index, track, smoothed_per_km2, safety_tier
            FROM gold.cell_safety
            WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
              AND time_window = %(time_window)s AND scheme_version = {_SERVED_SCHEME}
              AND h3_index = ANY(%(cells)s)
            """,
            params,
        )
        for r in cur.fetchall():
            out.setdefault(r["track"], {})[r["h3_index"]] = {
                "sm": float(r["smoothed_per_km2"]),
                "tier": r["safety_tier"],
            }
        if hour_blocks:
            cur.execute(
                f"""
                SELECT h3_index, track, hour_block, smoothed_per_km2, safety_tier
                FROM gold.cell_hour_safety
                WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
                  AND time_window = %(time_window)s AND scheme_version = {_SERVED_SCHEME}
                  AND hour_block = ANY(%(hours)s) AND h3_index = ANY(%(cells)s)
                """,
                params,
            )
            for r in cur.fetchall():
                row = out.setdefault(r["track"], {}).setdefault(r["h3_index"], {})
                row[f"hsm.{r['hour_block']}"] = float(r["smoothed_per_km2"])
                row[f"htier.{r['hour_block']}"] = r["safety_tier"]
        if days:
            cur.execute(
                f"""
                SELECT h3_index, track, dow, day_factor
                FROM gold.cell_dow_factor
                WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
                  AND time_window = %(time_window)s AND scheme_version = {_SERVED_SCHEME}
                  AND dow = ANY(%(days)s::smallint[]) AND h3_index = ANY(%(cells)s)
                """,
                params,
            )
            for r in cur.fetchall():
                row = out.setdefault(r["track"], {}).setdefault(r["h3_index"], {})
                row[f"df.{r['dow']}"] = float(r["day_factor"])
    return out


def hour_label(hour: int) -> str:
    """'20:00-21:00'. The last block reads 23:00-24:00, not 23:00-00:00."""
    return f"{hour:02d}:00–{hour + 1:02d}:00"


HOUR_BLOCKS = 24


def hour_profile(by_hour: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Reported incidents per hour block, one 24-long list per safety track.

    Split the way the safety ranking splits offence -- violent, and everything
    else -- so the panel's chart and outlook describe the same track the map is
    coloured by. gold.cell_hour_profile has no non-violent category of its own;
    every incident has a product category, so it is `all` less `violent`.
    """
    totals = {c: [0] * HOUR_BLOCKS for c in ("all", "violent")}
    for row in by_hour:
        if row["category"] in totals:
            totals[row["category"]][row["hour_block"]] = row["incident_count"]
    return {
        "violent": totals["violent"],
        "non_violent": [a - v for a, v in zip(totals["all"], totals["violent"])],
    }


def day_profile(by_day: list[dict[str, Any]]) -> dict[str, list[int]] | None:
    """Reported incidents per weekday (0 = Monday), one 7-long list per track,
    or None when there are no rows."""
    if not by_day:
        return None
    totals = {t: [0] * len(VALID_DAYS) for t in ("violent", "non_violent")}
    for row in by_day:
        if row["track"] in totals:
            totals[row["track"]][row["dow"]] = row["incident_count"]
    return totals


# ---------------------------------------------------------------------------
# Cities and metadata
# ---------------------------------------------------------------------------


def list_cities(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.*, r.location_precision_note, r.enabled,
                   r.occurrence_basis_note, r.denominator_examples_note, r.timezone
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
            SELECT s.*, r.location_precision_note, r.enabled,
                   r.occurrence_basis_note, r.denominator_examples_note, r.timezone
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
                   s.eb_prior_km2, s.eb_prior_persons, s.self_weight,
                   s.exposure_kind, s.jobs_weight
            FROM reference.severity_scheme s
            JOIN reference.source_registry r
              ON r.severity_scheme_version = s.scheme_version
            WHERE r.source_id = %s
            """,
            (source_id,),
        )
        return cur.fetchone()


def serving_version(
    conn: psycopg.Connection, source_id: str | None = None
) -> dict[str, Any]:
    """Cheap poll target so a client can notice an ETL refresh and reload.

    Per city when `source_id` is given, which is what cache invalidation wants:
    the aggregate form moves whenever *any* city is refreshed, so keying cached
    layers on it would throw away five cities' work every time the sixth
    reloaded. The aggregate is still the right answer for /health and /version,
    where the question really is "has anything changed".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max(last_refreshed_at) AS last_refreshed_at,
                   max(data_as_of)        AS data_as_of,
                   sum(incident_count)    AS incident_count,
                   count(*)::int          AS cities
            FROM gold.city_snapshot
            WHERE %s::text IS NULL OR source_id = %s
            """,
            (source_id, source_id),
        )
        return cur.fetchone() or {}


_BASIS_MIX_SQL = """
SELECT occurred_basis, count(*)::int AS n
FROM silver.incident
WHERE source_id = %s
GROUP BY 1 ORDER BY n DESC
"""

# The three values silver.incident.occurred_basis permits (004_silver.sql). A
# source that publishes both an occurrence and a report time picks one per
# record, so a mixed city shows up as two rows here rather than as a fourth
# basis -- which is more informative anyway, since the shares are visible.
BASIS_LABELS = {
    "dispatch": "the time police were dispatched",
    "occurrence": "the time the offence is recorded as having occurred",
    "report": "the time the offence was reported to police",
}


def occurrence_basis(conn: psycopg.Connection, source_id: str) -> list[dict[str, Any]]:
    """The mix of timestamp bases behind this city's records, most common first."""
    with conn.cursor() as cur:
        cur.execute(_BASIS_MIX_SQL, (source_id,))
        rows = cur.fetchall()
    total = sum(row["n"] for row in rows) or 1
    return [
        {
            **row,
            "share": round(row["n"] / total, 4),
            "label": BASIS_LABELS.get(row["occurred_basis"], row["occurred_basis"]),
        }
        for row in rows
    ]


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

        # The bronze snapshot path is deliberately not selected: the host's
        # filesystem layout is not public information. The ETL's replay paths
        # (safety/etl/run.py) read it from etl.pull_run directly, never from here.
        cur.execute(
            """
            SELECT pull_id, mode, status, records_fetched, records_rejected,
                   records_upserted, duration_seconds, started_at, finished_at
            FROM etl.pull_run
            WHERE source_id = %s
            ORDER BY pull_id DESC
            LIMIT 5
            """,
            (source_id,),
        )
        pulls = cur.fetchall()

    # Live, not cached: both reads are on small, indexed etl tables, and a failed
    # pull adds a pull_run row without a gold refresh to bump the stamp.
    return {"validation_issues": issues, "recent_pulls": pulls}


def silver_provenance_mix(conn: psycopg.Connection, source_id: str) -> dict[str, Any]:
    """How this city's coordinates and offence mappings were obtained (S8.5).

    Two GROUP BYs over the city's silver rows: cached by refresh stamp in the
    API (see the note above occurrence_basis), never run per request.
    """
    with conn.cursor() as cur:
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

    return {"coordinate_provenance": coordinates, "offense_mapping_confidence": mappings}


def series_caveats(conn: psycopg.Connection, source_id: str) -> list[dict[str, Any]]:
    """The city's recording changes, for the client to match against any range.

    Plain types (ISO dates), so the result can ride the stamped JSON cache.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT period_from, period_to, caveat_text
            FROM reference.source_series_caveat
            WHERE source_id = %s
            ORDER BY period_from
            """,
            (source_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "from": r["period_from"].isoformat(),
            # Exclusive, as stored.
            "to": r["period_to"].isoformat(),
            "text": r["caveat_text"],
        }
        for r in rows
    ]


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
timed AS (
    -- The day of the week (022): the all-hours rate -- or the requested hour
    -- block's -- times each cell's factor for that weekday. Every cell of the
    -- city. Empty unless a day was asked for;
    -- a city whose factor is not built yet reads as a factor of 1.
    SELECT s.h3_index, s.track, s.safety_tier AS base_tier,
           COALESCE(h.smoothed_per_km2, s.smoothed_per_km2)
               * COALESCE(d.day_factor, 1) AS rate,
           -- The day's incidents, or the hour's where an hour was asked for:
           -- nothing is stored for an hour on a given weekday.
           CASE WHEN %(hour_block)s::smallint IS NULL
                THEN d.incident_count ELSE h.incident_count END AS n
    FROM gold.cell_safety s
    CROSS JOIN scheme
    LEFT JOIN gold.cell_hour_safety h
           ON %(hour_block)s::smallint IS NOT NULL
          AND h.source_id      = s.source_id
          AND h.h3_index       = s.h3_index
          AND h.h3_res         = s.h3_res
          AND h.time_window    = s.time_window
          AND h.hour_block     = %(hour_block)s::smallint
          AND h.scheme_version = s.scheme_version
          AND h.track          = s.track
    LEFT JOIN gold.cell_dow_factor d
           ON d.source_id      = s.source_id
          AND d.h3_res         = s.h3_res
          AND d.time_window    = s.time_window
          AND d.scheme_version = s.scheme_version
          AND d.dow            = %(dow)s::smallint
          AND d.track          = s.track
          AND d.h3_index       = s.h3_index
    WHERE %(dow)s::smallint IS NOT NULL
      AND s.source_id      = %(source_id)s
      AND s.h3_res         = %(h3_res)s
      AND s.time_window    = %(time_window)s
      AND s.scheme_version = scheme.version
),
timed_ranked AS (
    -- Ranked as gold.cell_hour_safety is (Hazen midrank, 1.0 = safest), and
    -- tiered the same way: tier 0 stays the all-hours tier 0, nothing of the
    -- track reported here at all.
    SELECT t.h3_index, t.track, t.rate, t.n, t.base_tier,
           (
               (rank() OVER (PARTITION BY t.track ORDER BY t.rate DESC)
                + (count(*) OVER (PARTITION BY t.track, t.rate) - 1) / 2.0)
               - 0.5
           ) / count(*) OVER (PARTITION BY t.track) AS pct
    FROM timed t
),
timed_tiered AS (
    SELECT r.*,
           CASE
               WHEN r.base_tier = 0 THEN 0
               WHEN r.pct < 0.25    THEN 1
               WHEN r.pct < 0.50    THEN 2
               WHEN r.pct < 0.75    THEN 3
               ELSE 4
           END AS tier
    FROM timed_ranked r
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
           -- The quantity the ranking is actually computed on, so a
           -- value-anchored ramp orders cells the same way the percentile
           -- beside it does. Under a per-capita scheme this is offence per
           -- ambient person, not per km2 -- the client's ramp hinges on the
           -- median of whatever it is handed, so the unit change passes
           -- through without a ramp change.
           sv.smoothed_per_km2  AS smoothed_violent,
           sv.weighted_per_1k   AS per1k_violent,
           sn.safety_percentile AS safety_nonviolent,
           sn.safety_tier       AS tier_nonviolent,
           sn.weighted_total    AS weighted_nonviolent,
           sn.smoothed_per_km2  AS smoothed_nonviolent,
           sn.weighted_per_1k   AS per1k_nonviolent,
           -- The denominator itself, so the panel can state what a cell was
           -- ranked against rather than leaving it implicit.
           sv.exposure          AS exposure,
           -- Time of day, and the day of the week. All NULL unless the request
           -- named an hour or a day; unlike the two tracks these cannot ride
           -- along, because carrying all 24 blocks on every feature multiplies
           -- the payload by 24. With a day they come from the ranking above;
           -- with an hour alone, from the stored hourly layer.
           CASE WHEN %(dow)s::smallint IS NULL THEN hv.safety_percentile ELSE tv.pct  END AS hsafety_violent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hv.safety_tier       ELSE tv.tier END AS htier_violent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hv.smoothed_per_km2  ELSE tv.rate END AS hsmoothed_violent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hv.incident_count    ELSE tv.n    END AS hcount_violent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hn.safety_percentile ELSE tn.pct  END AS hsafety_nonviolent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hn.safety_tier       ELSE tn.tier END AS htier_nonviolent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hn.smoothed_per_km2  ELSE tn.rate END AS hsmoothed_nonviolent,
           CASE WHEN %(dow)s::smallint IS NULL THEN hn.incident_count    ELSE tn.n    END AS hcount_nonviolent
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
          AND %(dow)s::smallint IS NULL
    LEFT JOIN gold.cell_hour_safety hn
           ON %(hour_block)s::smallint IS NOT NULL
          AND hn.source_id      = a.source_id
          AND hn.h3_index       = a.h3_index
          AND hn.h3_res         = a.h3_res
          AND hn.time_window    = a.time_window
          AND hn.hour_block     = %(hour_block)s::smallint
          AND hn.scheme_version = scheme.version
          AND hn.track          = 'non_violent'
          AND %(dow)s::smallint IS NULL
    LEFT JOIN timed_tiered tv ON tv.h3_index = a.h3_index AND tv.track = 'violent'
    LEFT JOIN timed_tiered tn ON tn.h3_index = a.h3_index AND tn.track = 'non_violent'
    WHERE a.source_id   = %(source_id)s
      AND a.h3_res      = %(h3_res)s
      AND a.time_window = %(time_window)s
      AND a.category    = 'all'
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
        'category',    'all',
        'hour_block',  %(hour_block)s::smallint,
        'dow',         %(dow)s::smallint,
        -- Whether the per-capita ranking exists at this cell size. The client
        -- needs it as a distinct state: without it, a resolution where the
        -- ranking is not built and a city where every cell happens to be
        -- equally safe both arrive as a layer full of nulls.
        'safety_available', %(safety_available)s::boolean,
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
                    'sm_violent',        round(l.smoothed_violent::numeric, 3),
                    'safety_nonviolent', round(l.safety_nonviolent::numeric, 4),
                    'stier_nonviolent',  l.tier_nonviolent,
                    'sw_nonviolent',     round(l.weighted_nonviolent::numeric, 1),
                    'sm_nonviolent',     round(l.smoothed_nonviolent::numeric, 3),
                    -- Severity-weighted offence per 1,000 ambient
                    -- residents-and-jobs, and the denominator behind it. Null
                    -- under an area-denominated scheme.
                    'p1k_violent',       round(l.per1k_violent::numeric, 2),
                    'p1k_nonviolent',    round(l.per1k_nonviolent::numeric, 2),
                    'exposure',          round(l.exposure::numeric, 0),
                    -- The ranking at the requested hour: the all-hours rate
                    -- times this cell's hour factor, re-ranked at that hour.
                    'hsafety_violent',    round(l.hsafety_violent::numeric, 4),
                    'hstier_violent',     l.htier_violent,
                    'hsm_violent',        round(l.hsmoothed_violent::numeric, 3),
                    'hsafety_nonviolent', round(l.hsafety_nonviolent::numeric, 4),
                    'hstier_nonviolent',  l.htier_nonviolent,
                    'hsm_nonviolent',     round(l.hsmoothed_nonviolent::numeric, 3),
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
    time_window: str = DEFAULT_WINDOW,
    hour: int | None = None,
    day: int | None = None,
) -> dict[str, Any]:
    """The city's layer over the last year, optionally at an hour and a weekday."""
    params = {
        "source_id": source_id,
        "h3_res": h3_res,
        "time_window": time_window,
        "hour_block": hour,
        "dow": day,
        "safety_available": h3_res in SAFETY_RESOLUTIONS,
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
    time_window: str = DEFAULT_WINDOW,
    hour: int | None = None,
) -> dict[str, Any] | None:
    """One cell over the last year: its ratings, and its hour and weekday shapes."""
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

        # The denominator this cell was ranked against. Its own row rather than
        # a field on the safety rows, because it is a property of the place --
        # the same figure for both tracks.
        cur.execute(
            """
            SELECT residents, jobs, block_count, pop_vintage, jobs_vintage
            FROM gold.cell_exposure WHERE h3_index = %s
            """,
            (h3_index,),
        )
        exposure = cur.fetchone()

        cur.execute(
            """
            SELECT category, incident_count, incidents_per_km2, percentile,
                   activity_tier, city_rank, city_cell_total, window_start, window_end
            FROM gold.cell_activity
            WHERE h3_index = %s AND time_window = %s AND category = 'all'
            """,
            (h3_index, time_window),
        )
        headline = cur.fetchone()

        cur.execute(
            """
            SELECT s.track, s.safety_percentile, s.safety_rank, s.city_cell_total,
                   s.safety_tier, s.incident_count, s.weighted_total,
                   s.weighted_per_km2, s.weighted_per_1k, s.exposure, s.scheme_version
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

        # The whole 24-block shape, so the popup can read the current hour
        # against the rest of the day rather than as a bare number.
        cur.execute(
            """
            SELECT hour_block, category, incident_count
            FROM gold.cell_hour_profile
            WHERE h3_index = %s AND time_window = %s
              AND category IN ('all', 'violent')
            ORDER BY hour_block, category
            """,
            (h3_index, time_window),
        )
        by_hour = cur.fetchall()

        # The week's shape, per track, from the weekday factor's own counts (022).
        cur.execute(
            """
            SELECT f.track, f.dow, f.incident_count
            FROM gold.cell_dow_factor f
            JOIN reference.source_registry r
              ON r.source_id = f.source_id
             AND r.severity_scheme_version = f.scheme_version
            WHERE f.h3_index = %s AND f.time_window = %s
            """,
            (h3_index, time_window),
        )
        by_day = cur.fetchall()

        hour_safety: list[dict[str, Any]] = []
        if hour is not None:
            cur.execute(
                """
                SELECT s.track, s.hour_block, s.safety_percentile, s.safety_tier,
                       s.safety_rank, s.city_cell_total, s.incident_count,
                       s.hour_index AS hour_factor
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

    return {
        "cell": cell,
        "exposure": _exposure_payload(exposure),
        "time_window": time_window,
        "window_label": window_label(time_window),
        "headline": headline,
        "tier_label": TIER_LABELS.get(headline["activity_tier"]) if headline else None,
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
        "hour_profile": hour_profile(by_hour),
        # Incidents per weekday, Monday first, one 7-long list per track; null
        # where the weekday factor is not built yet.
        "day_profile": day_profile(by_day),
        "hour_safety": [
            {
                **row,
                "track_label": TRACK_LABELS.get(row["track"], row["track"]),
                "tier_label": SAFETY_TIER_LABELS.get(row["safety_tier"]),
            }
            for row in hour_safety
        ],
    }


def _exposure_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The cell's ambient population, rounded to something honest.

    Whole people, because the fractions are an artifact of splitting census
    blocks across hexagons and printing 41.6 residents would claim a precision
    the apportionment has not got.
    """
    if row is None:
        return None
    residents = row["residents"] or 0
    jobs = row["jobs"] or 0
    return {
        "residents": round(residents),
        "jobs": round(jobs),
        "ambient": round(residents + jobs),
        "block_count": row["block_count"],
        "pop_vintage": row["pop_vintage"],
        "jobs_vintage": row["jobs_vintage"],
    }
