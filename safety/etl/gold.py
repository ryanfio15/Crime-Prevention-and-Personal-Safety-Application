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
import re
import time
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

# S5/S9.3: the windows the product needs, precomputed. Not a fixed list any more:
# every city gets the four short windows, then cumulative years back to its
# oldest stored incident (resolve_windows), so a city with twenty years of
# history offers twenty more choices than one with two.
SHORT_WINDOWS = ("last_30d", "last_3m", "last_6m", "last_9m")
WINDOW_PATTERN = re.compile(r"^last_(30d|[369]m|[1-9][0-9]?y)$")
MAX_WINDOW_YEARS = 99

# The names the previous release wrote, and the window each is now a copy of.
# last_12m and last_24m span exactly last_1y and last_2y; last_90d becomes three
# calendar months, a day or two longer. Written alongside the new names while
# settings.gold_legacy_windows is on, so a rollback to that release still finds
# a full map, and served by the API as aliases so bookmarked links keep working.
LEGACY_WINDOWS = {"last_90d": "last_3m", "last_12m": "last_1y", "last_24m": "last_2y"}

# A partial oldest window is offered only if it adds at least this share of a
# full step's worth of data. A 24-month backfill starts on the first of a month,
# so it usually reaches a week or two past "last 2 years"; without this every
# city would offer a "last 3 years" holding two years and a fortnight.
PARTIAL_WINDOW_MIN_SHARE = 0.25

CATEGORIES = ("all", "violent", "property", "quality_of_life", "other")
FILTERED_CATEGORIES = tuple(c for c in CATEGORIES if c != "all")
OFFENSE_MIX_DEPTH = 8
# The span of gold.cell_monthly (refresh_cell_detail).
MONTHLY_SPAN = "last_2y"

# The safety ranking splits the city two ways rather than five, and publishes no
# combined figure. That follows the FBI, which discontinued its own combined
# Crime Index in 2004 -- an unweighted total is dominated by whichever offense is
# most numerous, normally larceny-theft -- and has reported violent and property
# separately ever since.
TRACKS = ("violent", "non_violent")
SAFETY_TIERS = 4

# Resolutions the safety ranking builds at, for every scheme.
#
# The original reason is the per-capita denominator: this mirrors
# safety.etl.census.EXPOSURE_RESOLUTIONS, and a resolution-10 cell is smaller
# than a census block, so its population is an apportionment assumption rather
# than a measurement. A ranking computed on one would be reporting this
# pipeline's own interpolation back to the user.
#
# It applies to area-denominated schemes too, which it did not used to. Area is
# exact at any cell size, so those built at resolution 10 as well -- but
# safety/api/repository.py::SAFETY_RESOLUTIONS is (8, 9) and the serving layer
# refuses a resolution-10 ranking whatever built it. Those rows were ~1.9M per
# city that nothing could read. The scheme parameter cannot widen what the API
# serves, so it no longer widens what the pipeline stores.
SAFETY_RESOLUTIONS = (8, 9)

# What the activity layer builds per resolution, where that is narrower than
# every window x CATEGORIES.
#
# cell_activity is dense by construction -- every cell in the universe gets a
# row per window per category, because a cell with no reported incidents is part
# of the distribution (S3.3) and has to be ranked. That is 20 rows per cell, and
# at resolution 10 it is the largest table in the database: six cities are on the
# order of 230,000 resolution-10 cells, so 4.6M rows, nearly all of them
# n = 0 / tier = 0.
#
# The narrowing is a data-volume judgement and is written here rather than in
# DDL for the same reason HOURLY_RESOLUTIONS is. It is also close to free
# statistically: a resolution-10 cell is ~0.015 km2, and splitting 30 days
# across cells that size leaves a median of zero, so the short windows were
# ranking a field of ties. The drill-down keeps the two windows and the one
# category the map opens on.
ACTIVITY_WINDOWS: dict[int, tuple[str, ...]] = {10: ("last_1y", "last_2y")}
ACTIVITY_CATEGORIES: dict[int, tuple[str, ...]] = {10: ("all",)}


def activity_scope(res: int) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    """The windows and categories the activity layer builds at a resolution.

    None for the windows means every window the city has; the list itself is
    per city (resolve_windows).
    """
    return (
        ACTIVITY_WINDOWS.get(res),
        ACTIVITY_CATEGORIES.get(res, CATEGORIES),
    )


def activity_builds(res: int, window: str) -> bool:
    windows, _ = activity_scope(res)
    return windows is None or window in windows


def window_years(name: str) -> int | None:
    """N for last_Ny, None for the short windows."""
    return int(name[5:-1]) if name.endswith("y") else None


def safety_builds(window: str) -> bool:
    """Whether the safety ranking is built for a window.

    Every window by default. settings.safety_max_window_years is the fallback
    if the long windows do not fit the disk or the refresh budget: windows
    longer than it keep their counts and lose the ranking, and the API says so.
    """
    from safety.config import settings

    limit = settings.safety_max_window_years
    years = window_years(window)
    return limit is None or years is None or years <= limit

# Time of day. Block h covers [h:00, h+1:00) local; 23 is 23:00-24:00.
HOUR_BLOCKS = 24

# The hourly layer is built narrower than the all-hours one. The resolution cap
# is statistical before it is about disk: splitting a window across 24 buckets
# leaves each one with a twenty-fourth of the evidence, and 30 days at
# resolution 10 puts the median cell-hour at zero reported incidents -- there is
# no distribution there to rank.
#
# The window cap is the other way round, and worth being honest about: it is a
# disk decision. gold.cell_hour_safety is the largest table in the database by a
# wide margin -- 1,042 MB at two cities, 31% of the total, against a 5 GB volume
# that has to hold six -- because it is the only layer multiplied by 24. Dropping
# the two-year window halved it, and halved gold.cell_hour_profile with it; the
# per-city years back to 2001 (resolve_windows) would multiply it again.
#
# One year (last_1y, formerly last_12m) is the one kept because longer windows
# are the more redundant: at a year wide the hourly distribution is already
# stable, and a second year mostly reasserts it. Both were within the range where the counts support the
# statistic, so this gives up a real view rather than a marginal one -- see
# docs/PHASE2.md.
#
# Widening either is a one-line change and needs no migration: both hourly
# refreshes delete across every window before skipping the ones out of scope, so
# the rows come back on the next build.
HOURLY_RESOLUTIONS = (8, 9)
HOURLY_WINDOWS = ("last_1y",)

# Below this many incidents across the whole window, a cell's hour-to-hour
# ratio is noise dressed as a measurement, and hour_index is left NULL rather
# than published. Twelve is half an incident per hour block on average.
MIN_HOUR_EVIDENCE = 12


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    start: date
    end: date
    # The first date this city actually has data for inside the window. Later
    # than `start` only for the oldest window, or every window of a city with
    # less history than the window spans.
    data_start: date | None = None
    partial: bool = False


def _window_candidates(anchor: date):
    """Every window name in order, with its start date, up to MAX_WINDOW_YEARS."""
    yield "last_30d", anchor - timedelta(days=29)
    for months in (3, 6, 9):
        yield f"last_{months}m", _shift_months(anchor, months) + timedelta(days=1)
    for years in range(1, MAX_WINDOW_YEARS + 1):
        yield f"last_{years}y", _shift_years(anchor, years) + timedelta(days=1)


def resolve_windows(anchor: date, history_floor: date | None = None) -> list[Window]:
    """The windows a city is built for: 30 days, 3/6/9 months, then years.

    Windows are anchored to the newest reported date, not to today. Anchoring to
    `now` would silently present a source's publication lag as an absence of
    crime. S12(b) wants "data as of" visible; this makes the windows themselves
    honest about it too.

    Each window is cumulative ("the last 3 years" includes the last 2). The list
    stops at the first window that reaches the city's oldest stored date,
    `history_floor`, so it covers the whole history without offering windows
    that hold nothing more than the one before. A window reaching past the floor
    is marked partial, and is dropped if it would add less than
    PARTIAL_WINDOW_MIN_SHARE of a full step.

    With no floor, the list stops at last_2y: the windows a 24-month backfill
    supports.
    """
    if history_floor is None:
        history_floor = _shift_years(anchor, 2) + timedelta(days=1)

    built: list[Window] = []
    previous_start: date | None = None
    for name, start in _window_candidates(anchor):
        partial = start < history_floor
        if previous_start is not None:
            step = (previous_start - start).days
            added = (previous_start - max(start, history_floor)).days
            if added <= 0:
                break
            if partial and added < step * PARTIAL_WINDOW_MIN_SHARE:
                break
        built.append(Window(name, start, anchor, max(start, history_floor), partial))
        if start <= history_floor:
            break
        previous_start = start
    return built


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:  # 29 February
        return value.replace(year=value.year - years, day=28)


def _shift_months(value: date, months: int) -> date:
    """The same day `months` earlier, clamped to the end of a shorter month."""
    index = value.year * 12 + value.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def history_floor(conn: psycopg.Connection, source_id: str) -> date | None:
    """The oldest date the window list has to reach for this city.

    The oldest stored incident, but never before the registry's configured
    floor: a single record published with a 1901 date would otherwise add a
    century of windows over nothing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT GREATEST(
                       (SELECT min(occurred_local_date) FROM silver.incident WHERE source_id = %(s)s),
                       COALESCE(r.history_start_date, r.backfill_start_date)
                   ) AS floor
            FROM (SELECT 1) _
            LEFT JOIN reference.source_registry r ON r.source_id = %(s)s
            """,
            {"s": source_id},
        )
        row = cur.fetchone()
    return row["floor"] if row else None


def city_windows(conn: psycopg.Connection, source_id: str, anchor: date) -> list[Window]:
    return resolve_windows(anchor, history_floor(conn, source_id))


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

        # COPY into a temp table, then one INSERT ... SELECT, rather than a
        # statement per cell. The cell universe scales with city area: ~25,000
        # resolution-10 cells for Philadelphia's 350 km2, roughly four times that
        # for Los Angeles. An executemany of 100,000 rows, each parsing its own
        # GeoJSON literal, is minutes of round trips for work the server can do
        # in one pass.
        ordered = sorted(cells)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _cell_fill ("
                "  h3_index text, source_id text, h3_res smallint,"
                "  area_km2 double precision, lng double precision,"
                "  lat double precision, boundary text"
                ") ON COMMIT DROP"
            )
            with cur.copy(
                "COPY _cell_fill (h3_index, source_id, h3_res, area_km2, lng, lat, boundary) "
                "FROM STDIN"
            ) as copy:
                for cell in ordered:
                    lng, lat = cell_centroid(cell)
                    copy.write_row(
                        (
                            cell,
                            source_id,
                            res,
                            cell_area_km2(cell),
                            lng,
                            lat,
                            json.dumps(cell_polygon_geojson(cell)),
                        )
                    )
            cur.execute(
                """
                INSERT INTO gold.cell_geometry
                    (h3_index, source_id, h3_res, area_km2, centroid, boundary, built_at)
                SELECT h3_index, source_id, h3_res, area_km2,
                       ST_SetSRID(ST_MakePoint(lng, lat), 4326),
                       ST_SetSRID(ST_GeomFromGeoJSON(boundary), 4326),
                       now()
                FROM _cell_fill
                ON CONFLICT (h3_index) DO UPDATE SET
                    area_km2 = EXCLUDED.area_km2,
                    centroid = EXCLUDED.centroid,
                    boundary = EXCLUDED.boundary,
                    built_at = EXCLUDED.built_at
                """
            )
            cur.execute("DROP TABLE _cell_fill")
        counts[res] = len(ordered)
        log.info("cell universe res %s: %s cells", res, len(ordered))

        # Only where the ranking that reads it is built. gold.cell_neighbor has
        # exactly two readers, _SAFETY_SQL and _HOUR_SAFETY_SQL, and both are
        # capped at SAFETY_RESOLUTIONS -- so resolution-10 adjacency was six
        # pairs per cell, rewritten on every refresh, that nothing ever joined
        # against. The DELETE inside the helper still runs for every resolution,
        # so widening SAFETY_RESOLUTIONS later refills this with no migration.
        if res in SAFETY_RESOLUTIONS:
            _build_cell_neighbors(conn, source_id, res, cells)
        else:
            _clear_cell_neighbors(conn, source_id, res)

    conn.commit()
    return counts


def _clear_cell_neighbors(conn: psycopg.Connection, source_id: str, res: int) -> None:
    """Drop adjacency for a resolution the ranking is no longer built at.

    Runs on every refresh rather than once in a migration, so the table cannot
    hold pairs for a resolution outside SAFETY_RESOLUTIONS however that constant
    moves -- including back the other way.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM gold.cell_neighbor WHERE source_id = %s AND h3_res = %s",
            (source_id, res),
        )
        if cur.rowcount:
            log.info(
                "cell adjacency res %s: dropped %s pair(s); the ranking is not "
                "built at this resolution, so nothing reads them",
                res,
                cur.rowcount,
            )


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
    # Six pairs per cell, so this is the larger of the two writes by a wide
    # margin -- around 150,000 rows for Philadelphia at resolution 10 and roughly
    # four times that for Los Angeles. COPY straight in; the DELETE above already
    # cleared the partition being rebuilt, so there is no conflict to resolve and
    # no temp table needed.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM gold.cell_neighbor WHERE source_id = %s AND h3_res = %s",
            (source_id, res),
        )
        with cur.copy(
            "COPY gold.cell_neighbor (source_id, h3_res, h3_index, neighbor_h3) FROM STDIN"
        ) as copy:
            for row in payload:
                copy.write_row(row)
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
# Period aggregates: read silver once per layer, not once per window
# ---------------------------------------------------------------------------
#
# With windows back to 2001 a city has thirty of them, and each layer used to
# rescan silver for every one -- with the severity-weight lookup run per
# incident per window. Instead each layer reads silver once into a temp table
# keyed by *bucket*: one bucket per month for the first year back from the
# anchor, then one per year. Every window is a whole number of buckets
# (window_bucket_limit), so its counts are a sum over buckets < limit, and the
# 30-day window, which is not month-aligned, rides on its own flag.
#
# Month m back from the anchor is (_shift_months(anchor, m + 1),
# _shift_months(anchor, m)], so "the last K months" -- which starts the day after
# _shift_months(anchor, K) -- is exactly months 0..K-1. last_Ny is 12N months,
# and _shift_years agrees with _shift_months(12N) on every date, 29 February
# included, so the year windows line up too.

# Off only to compare against the original per-window SQL (tests, or a
# suspected discrepancy): the results are identical, the cost is not.
PREAGGREGATE = True


def window_months(name: str) -> int | None:
    """K for last_Km, 12N for last_Ny, None for last_30d."""
    if name == "last_30d":
        return None
    count = int(name[5:-1])
    return count * 12 if name.endswith("y") else count


def _bucket(month_index: int) -> int:
    return month_index if month_index < 12 else 11 + month_index // 12


def window_bucket_limit(name: str) -> int | None:
    """The window is buckets [0, limit); None for last_30d (its own flag)."""
    months = window_months(name)
    if months is None:
        return None
    return months if months <= 12 else 11 + months // 12


def _in_window(window: Window) -> str:
    """SQL predicate over an aggregate's bucket / in_30d columns."""
    limit = window_bucket_limit(window.name)
    return "in_30d" if limit is None else f"bucket < {int(limit)}"


def period_rows(anchor: date, oldest: date) -> list[tuple[date, int, bool]]:
    """(date, bucket, in the last 30 days) for every date from `oldest` to `anchor`."""
    thirty = anchor - timedelta(days=29)
    rows = []
    month = 0
    lower = _shift_months(anchor, 1)
    day = anchor
    while day >= oldest:
        while day <= lower:
            month += 1
            lower = _shift_months(anchor, month + 1)
        rows.append((day, _bucket(month), day >= thirty))
        day -= timedelta(days=1)
    return rows


def _create_period_table(cur: psycopg.Cursor, windows: list[Window]) -> None:
    """_period(d, bucket, in_30d) for every date the windows cover.

    About 9,500 rows for 26 years. Dates outside every window have no row, so
    the inner join to it drops them exactly as BETWEEN start AND end did.
    """
    rows = period_rows(windows[0].end, min(w.start for w in windows))
    cur.execute("DROP TABLE IF EXISTS _period")
    cur.execute(
        "CREATE TEMP TABLE _period (d date PRIMARY KEY, bucket smallint NOT NULL, "
        "in_30d boolean NOT NULL) ON COMMIT DROP"
    )
    with cur.copy("COPY _period (d, bucket, in_30d) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    cur.execute("ANALYZE _period")


def _res_rows(resolutions) -> str:
    """VALUES rows expanding one incident into one row per resolution."""
    return ", ".join(f"({int(res)}, x.{_h3_column(res)})" for res in resolutions)


def _res_bucket_cap(windows: list[Window], resolutions, builds) -> str:
    """SQL limiting each resolution to the buckets its in-scope windows read."""
    cases = []
    for res in resolutions:
        limits = [
            # last_30d can reach into month 1 when month 0 is February-short.
            window_bucket_limit(w.name) or 2 for w in windows if builds(res, w.name)
        ]
        cases.append(f"WHEN {int(res)} THEN {max(limits) if limits else 0}")
    return f"x.bucket < CASE res.h3_res {' '.join(cases)} END"


def _build_activity_aggregate(
    cur: psycopg.Cursor, source_id: str, windows: list[Window]
) -> None:
    cur.execute("DROP TABLE IF EXISTS _activity_agg")
    cur.execute(
        f"""
        CREATE TEMP TABLE _activity_agg ON COMMIT DROP AS
        SELECT
            res.h3_res, res.h3_index, x.bucket, x.in_30d,
            count(*)                                                    AS c_all,
            count(*) FILTER (WHERE x.product_category = 'violent')         AS c_violent,
            count(*) FILTER (WHERE x.product_category = 'property')        AS c_property,
            count(*) FILTER (WHERE x.product_category = 'quality_of_life') AS c_quality_of_life,
            count(*) FILTER (WHERE x.product_category = 'other')           AS c_other
        FROM (
            SELECT i.h3_r8, i.h3_r9, i.h3_r10, i.product_category, p.bucket, p.in_30d
            FROM silver.incident i
            JOIN _period p ON p.d = i.occurred_local_date
            WHERE i.source_id = %s
        ) x
        CROSS JOIN LATERAL (VALUES {_res_rows(RESOLUTIONS)}) AS res(h3_res, h3_index)
        WHERE res.h3_index IS NOT NULL
          AND {_res_bucket_cap(windows, RESOLUTIONS, activity_builds)}
        GROUP BY 1, 2, 3, 4
        """,
        (source_id,),
    )
    cur.execute("ANALYZE _activity_agg")


def _build_safety_aggregate(
    cur: psycopg.Cursor, source_id: str, scheme: Scheme
) -> None:
    """Severity-weighted sums per bucket for one scheme: the weight lookup runs
    once per incident here instead of once per incident per window."""
    cur.execute("DROP TABLE IF EXISTS _safety_agg")
    cur.execute(
        f"""
        CREATE TEMP TABLE _safety_agg ON COMMIT DROP AS
        SELECT
            res.h3_res, res.h3_index, x.bucket, x.in_30d,
            count(*) FILTER (WHERE x.product_category =  'violent') AS n_violent,
            count(*) FILTER (WHERE x.product_category <> 'violent') AS n_non_violent,
            COALESCE(sum(x.weight) FILTER (WHERE x.product_category =  'violent'), 0)
                AS w_violent,
            COALESCE(sum(x.weight) FILTER (WHERE x.product_category <> 'violent'), 0)
                AS w_non_violent
        FROM (
            SELECT i.h3_r8, i.h3_r9, i.h3_r10, i.product_category, p.bucket, p.in_30d,
                   COALESCE(w.weight, 1.0) AS weight
            FROM silver.incident i
            JOIN _period p ON p.d = i.occurred_local_date
            {_WEIGHT_LOOKUP}
            WHERE i.source_id = %(source_id)s
        ) x
        CROSS JOIN LATERAL (VALUES {_res_rows(scheme.resolutions)}) AS res(h3_res, h3_index)
        WHERE res.h3_index IS NOT NULL
        GROUP BY 1, 2, 3, 4
        """,
        {"source_id": source_id, "scheme": scheme.version},
    )
    cur.execute("ANALYZE _safety_agg")


def _build_mix_aggregate(cur: psycopg.Cursor, source_id: str, windows: list[Window]) -> None:
    cur.execute("DROP TABLE IF EXISTS _mix_agg")
    cur.execute(
        f"""
        CREATE TEMP TABLE _mix_agg ON COMMIT DROP AS
        SELECT
            res.h3_res, res.h3_index, x.bucket, x.in_30d, x.raw_offense_text,
            min(x.nibrs_code)       AS nibrs_code,
            min(x.product_category) AS product_category,
            count(*)                AS n
        FROM (
            SELECT i.h3_r8, i.h3_r9, i.h3_r10, i.raw_offense_text, i.nibrs_code,
                   i.product_category, p.bucket, p.in_30d
            FROM silver.incident i
            JOIN _period p ON p.d = i.occurred_local_date
            WHERE i.source_id = %s AND i.raw_offense_text IS NOT NULL
        ) x
        CROSS JOIN LATERAL (VALUES {_res_rows(RESOLUTIONS)}) AS res(h3_res, h3_index)
        WHERE res.h3_index IS NOT NULL
          AND {_res_bucket_cap(windows, RESOLUTIONS, activity_builds)}
        GROUP BY 1, 2, 3, 4, 5
        """,
        (source_id,),
    )
    cur.execute("ANALYZE _mix_agg")


# ---------------------------------------------------------------------------
# Cell activity: one pass per (resolution, window), all categories at once
# ---------------------------------------------------------------------------

# Per-window counts read straight from silver: the original form, kept as the
# reference the pre-aggregated one is tested against (PREAGGREGATE).
_ACTIVITY_COUNTS_DIRECT = """
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
"""

# The same counts summed from _activity_agg (_build_activity_aggregate).
_ACTIVITY_COUNTS_AGG = """
    SELECT
        h3_index,
        sum(c_all)             AS c_all,
        sum(c_violent)         AS c_violent,
        sum(c_property)        AS c_property,
        sum(c_quality_of_life) AS c_quality_of_life,
        sum(c_other)           AS c_other
    FROM _activity_agg
    WHERE h3_res = %(h3_res)s AND {in_window}
    GROUP BY 1
"""

_ACTIVITY_SQL = """
WITH counts AS (
{counts}
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
    -- Narrowed at resolution 10; see ACTIVITY_CATEGORIES. Filtered here, ahead
    -- of the window functions below, which is both cheaper and safe: every
    -- percentile partitions by category, so dropping whole categories cannot
    -- move the ranking of the ones that remain.
    WHERE category = ANY(%(categories)s)
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
    """Rebuild gold.cell_activity for every resolution/window/category in scope."""
    written = 0
    with conn.cursor() as cur:
        if PREAGGREGATE:
            _create_period_table(cur, windows)
            _build_activity_aggregate(cur, source_id, windows)
        for res in RESOLUTIONS:
            h3_column = _h3_column(res)
            scope_windows, scope_categories = activity_scope(res)
            if scope_windows is not None or scope_categories != CATEGORIES:
                log.info(
                    "cell_activity res=%s is narrowed to windows %s, categories %s",
                    res,
                    ", ".join(scope_windows or ("all",)),
                    ", ".join(scope_categories),
                )
            # Delete-then-insert inside the caller's transaction: readers keep
            # seeing the previous rollup until commit, so the map never renders
            # a half-built layer.
            #
            # The DELETE covers every window and every category at this
            # resolution, including ones about to be skipped and ones this
            # refresh no longer builds at all (a legacy name, or a year the
            # city's history no longer reaches). That is what makes narrowing
            # reclaim disk rather than strand rows nothing will overwrite again,
            # and why the scope can be widened back without a migration.
            cur.execute(
                "DELETE FROM gold.cell_activity WHERE source_id = %s AND h3_res = %s",
                (source_id, res),
            )
            for window in windows:
                if not activity_builds(res, window.name):
                    continue
                counts = (
                    _ACTIVITY_COUNTS_AGG.format(in_window=_in_window(window))
                    if PREAGGREGATE
                    else _ACTIVITY_COUNTS_DIRECT.format(h3_column=h3_column)
                )
                cur.execute(
                    _ACTIVITY_SQL.format(counts=counts),
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                        "categories": list(scope_categories),
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
    exposure_kind: str = "area_km2"
    eb_prior_persons: float | None = None
    jobs_weight: float = 1.0

    @property
    def per_capita(self) -> bool:
        return self.exposure_kind == "ambient_population"

    @property
    def eb_prior(self) -> float:
        """The prior in whatever units this scheme's denominator is measured in.

        One knob, two unit systems: km2 of citywide-average evidence for an area
        scheme, ambient persons for a per-capita one. Keeping them in separate
        columns and resolving here means neither can be silently read as the
        other -- 0.40 km2 and 0.40 people are not remotely the same prior.
        """
        if self.per_capita:
            if self.eb_prior_persons is None:
                raise ValueError(
                    f"scheme '{self.version}' is per-capita but has no "
                    "eb_prior_persons; the database CHECK should have caught this"
                )
            return self.eb_prior_persons
        return self.eb_prior_km2

    @property
    def resolutions(self) -> tuple[int, ...]:
        # Not a function of the scheme any more. See SAFETY_RESOLUTIONS: an
        # area-denominated scheme *could* be ranked at resolution 10, but the
        # serving layer will not return it, so building it only cost disk.
        return SAFETY_RESOLUTIONS


_SCHEME_SELECT = """
    SELECT scheme_version, eb_prior_km2, self_weight,
           exposure_kind, eb_prior_persons, jobs_weight
    FROM reference.severity_scheme
"""


def _as_scheme(row: dict[str, Any]) -> Scheme:
    return Scheme(
        row["scheme_version"],
        row["eb_prior_km2"],
        row["self_weight"],
        row["exposure_kind"],
        row["eb_prior_persons"],
        row["jobs_weight"],
    )


def enabled_schemes(conn: psycopg.Connection) -> list[Scheme]:
    with conn.cursor() as cur:
        cur.execute(f"{_SCHEME_SELECT} WHERE enabled ORDER BY scheme_version")
        return [_as_scheme(r) for r in cur.fetchall()]


def get_scheme(conn: psycopg.Connection, version: str) -> Scheme:
    with conn.cursor() as cur:
        cur.execute(f"{_SCHEME_SELECT} WHERE scheme_version = %s", (version,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no severity scheme '{version}'; load reference/severity first")
    return _as_scheme(row)


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

_SAFETY_WEIGHTED_DIRECT = """
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
"""

_SAFETY_WEIGHTED_AGG = """
    SELECT
        h3_index,
        sum(n_violent)     AS n_violent,
        sum(n_non_violent) AS n_non_violent,
        sum(w_violent)     AS w_violent,
        sum(w_non_violent) AS w_non_violent
    FROM _safety_agg
    WHERE h3_res = %(h3_res)s AND {in_window}
    GROUP BY 1
"""

_SAFETY_SQL = """
WITH weighted AS (
{weighted}
),
universe AS (
    -- Area and exposure travel together from here down. The ranking divides by
    -- whichever one the scheme names; area_km2 stays in scope regardless,
    -- because weighted_per_km2 is still written and is still true.
    --
    -- Area is the poorer denominator and that is the point of the exposure
    -- column: a cell's weighted total scales with how many people are in it,
    -- so ranking on area alone reports where the city is busy as much as where
    -- it is dangerous.
    SELECT
        g.h3_index,
        g.area_km2,
        CASE WHEN %(per_capita)s
             THEN COALESCE(e.residents, 0) + COALESCE(e.jobs, 0) * %(jobs_weight)s
             ELSE g.area_km2
        END AS exposure
    FROM gold.cell_geometry g
    LEFT JOIN gold.cell_exposure e
           ON e.source_id = g.source_id
          AND e.h3_index  = g.h3_index
          AND e.h3_res    = g.h3_res
    WHERE g.source_id = %(source_id)s AND g.h3_res = %(h3_res)s
),
joined AS (
    SELECT
        u.h3_index,
        u.area_km2,
        u.exposure,
        COALESCE(x.n_violent, 0)     AS n_violent,
        COALESCE(x.n_non_violent, 0) AS n_non_violent,
        COALESCE(x.w_violent, 0)     AS w_violent,
        COALESCE(x.w_non_violent, 0) AS w_non_violent
    FROM universe u
    LEFT JOIN weighted x USING (h3_index)
),
unpivoted AS (
    SELECT h3_index, area_km2, exposure, track, n, w
    FROM joined
    CROSS JOIN LATERAL (VALUES
        ('violent',     n_violent,     w_violent),
        ('non_violent', n_non_violent, w_non_violent)
    ) AS v(track, n, w)
),
city AS (
    -- The rate each cell is shrunk toward: this track's citywide weighted
    -- offense per unit of exposure.
    SELECT track, sum(w) / NULLIF(sum(exposure), 0) AS city_rate
    FROM unpivoted
    GROUP BY track
),
adjusted AS (
    SELECT
        u.h3_index, u.area_km2, u.exposure, u.track, u.n, u.w,
        -- Poisson-gamma posterior rate: the cell's own weighted total plus
        -- eb_prior worth of citywide-average offense, over its own exposure
        -- plus that same prior.
        --
        -- The prior has to be an exposure rather than a count. A cell with no
        -- incidents was still watched for the whole window, so zero is evidence
        -- of a low rate, not missing information -- shrinking by n/(n+k) instead
        -- sends every empty cell to the citywide mean, which ranked a cell with
        -- six assaults safer than a cell with none.
        --
        -- With a population denominator the prior earns a second job: it is what
        -- keeps a cell whose ambient population rounds to nothing from dividing
        -- by zero. As exposure falls away the posterior tends to
        -- city_rate + w/prior, which is bounded. That is why the airport and the
        -- middle of Fairmount Park can be ranked at all rather than pinned to
        -- the bottom by arithmetic.
        (u.w + COALESCE(c.city_rate, 0) * %(eb_prior)s)
            / NULLIF(u.exposure + %(eb_prior)s, 0) AS adj
    FROM unpivoted u
    JOIN city c USING (track)
),
neighbor_mean AS (
    -- Grouped join rather than a correlated LATERAL, for the same reason the
    -- hourly build uses one: the LATERAL form re-scans `adjusted` once per row.
    -- At resolution 8 that is 1,102 rows and costs about two seconds; at
    -- resolution 10 it is 49,540 and cost two minutes per window, which was
    -- most of a Philadelphia gold refresh and would have been most of six.
    -- Computing every cell's neighbour mean in one pass is the same arithmetic.
    SELECT nbr.h3_index, x.track, avg(x.adj) AS mean_adj
    FROM gold.cell_neighbor nbr
    JOIN adjusted x ON x.h3_index = nbr.neighbor_h3
    WHERE nbr.source_id = %(source_id)s AND nbr.h3_res = %(h3_res)s
    GROUP BY 1, 2
),
blended AS (
    SELECT
        a.h3_index, a.area_km2, a.exposure, a.track, a.n, a.w, a.adj,
        -- Risk does not stop at a hexagon edge. A cell with no in-universe
        -- neighbours keeps its own value rather than being pulled toward zero.
        CASE WHEN nm.mean_adj IS NULL THEN a.adj
             ELSE %(self_weight)s * a.adj + (1 - %(self_weight)s) * nm.mean_adj
        END AS smoothed
    FROM adjusted a
    LEFT JOIN neighbor_mean nm
           ON nm.h3_index = a.h3_index AND nm.track = a.track
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
    exposure, weighted_per_1k,
    safety_percentile, safety_rank, city_cell_total, safety_tier, refreshed_at
)
SELECT
    %(source_id)s, h3_index, %(h3_res)s, %(time_window)s, track, %(scheme)s,
    %(window_start)s, %(window_end)s, n,
    w, w / area_km2, smoothed,
    -- Both NULL for an area scheme: that row's denominator is area_km2, and a
    -- per-1,000-people figure would be a number it never computed.
    CASE WHEN %(per_capita)s THEN exposure END,
    CASE WHEN %(per_capita)s THEN w / NULLIF(exposure / 1000.0, 0) END,
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


# Which offenses fell all the way through to the coarse UCR bucket, and how
# many incidents each accounts for. `weight_coverage` gives the share; this says
# what to do about it.
#
# The gap is expected to be non-empty for a while, and that is the honest state:
# the severity scale is a 1977 survey of 204 criminal events, and NIBRS has more
# offense codes than that. A code with no vignette behind it cannot be given a
# published weight -- inventing one and marking it `sourced = true` would be
# worse than the documented fallback. So the fallbacks stay, and this makes them
# reviewable rather than silent (S8.5's rule, applied to weights).
_UNWEIGHTED_SQL = f"""
SELECT
    i.nibrs_code,
    i.raw_offense_code,
    i.raw_offense_text,
    i.severity_bucket,
    CASE WHEN i.product_category = 'violent' THEN 'violent' ELSE 'non_violent' END
        AS track,
    count(*)::int AS incidents
FROM silver.incident i
{_WEIGHT_LOOKUP}
WHERE i.source_id = %(source_id)s
  -- Matched nothing more specific than the bucket, or matched nothing at all.
  AND (w.sourced IS NOT TRUE)
GROUP BY 1, 2, 3, 4, 5
ORDER BY incidents DESC
"""


def unweighted_offenses(
    conn: psycopg.Connection, source_id: str, scheme: str, limit: int = 40
) -> list[dict[str, Any]]:
    """Offenses whose severity weight is a derived fallback, busiest first."""
    with conn.cursor() as cur:
        cur.execute(_UNWEIGHTED_SQL, {"source_id": source_id, "scheme": scheme})
        rows = cur.fetchall()
    return rows[:limit]


def _require_exposure(
    conn: psycopg.Connection,
    source_id: str,
    scheme: Scheme,
    resolutions: tuple[int, ...] | None = None,
) -> None:
    """Refuse to build a per-capita ranking with no population loaded.

    `resolutions` defaults to the scheme's own; the hourly ranking passes
    HOURLY_RESOLUTIONS, which it builds instead.

    The exposure join is a LEFT JOIN, so a missing gold.cell_exposure does not
    fail -- it quietly makes every denominator zero, at which point the prior
    alone decides the ranking and every cell in the city ties. That would look
    like a working build producing a uniform map, which is a far worse failure
    than an exception.
    """
    wanted = tuple(resolutions or scheme.resolutions)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h3_res, count(*)::int AS cells,
                   coalesce(sum(residents + jobs), 0) AS ambient
            FROM gold.cell_exposure
            WHERE source_id = %s AND h3_res = ANY(%s)
            GROUP BY h3_res
            """,
            (source_id, list(wanted)),
        )
        built = {r["h3_res"]: r for r in cur.fetchall()}

    missing = [res for res in wanted if res not in built]
    if missing:
        raise LookupError(
            f"scheme '{scheme.version}' divides by ambient population, but "
            f"gold.cell_exposure has no rows for '{source_id}' at resolution "
            f"{missing}. Run `python -m safety.etl.run census --city {source_id}` "
            "first."
        )
    empty = [res for res, row in built.items() if row["ambient"] <= 0]
    if empty:
        raise LookupError(
            f"gold.cell_exposure for '{source_id}' at resolution {empty} sums to "
            "zero ambient population; the census load produced no counts, so the "
            "ranking would be the prior alone"
        )


def refresh_cell_safety(
    conn: psycopg.Connection,
    source_id: str,
    windows: list[Window],
    scheme: Scheme,
) -> int:
    """Rebuild gold.cell_safety for one scheme, every resolution and window."""
    if scheme.per_capita:
        _require_exposure(conn, source_id, scheme)
    written = 0
    with conn.cursor() as cur:
        if PREAGGREGATE:
            _create_period_table(cur, windows)
            _build_safety_aggregate(cur, source_id, scheme)
        for res in scheme.resolutions:
            h3_column = _h3_column(res)
            # Delete-then-insert inside the caller's transaction, so readers keep
            # seeing the previous ranking until commit. Every window at once, as
            # in refresh_cell_activity.
            cur.execute(
                """
                DELETE FROM gold.cell_safety
                WHERE source_id = %s AND h3_res = %s AND scheme_version = %s
                """,
                (source_id, res, scheme.version),
            )
            for window in windows:
                if not safety_builds(window.name):
                    continue
                weighted = (
                    _SAFETY_WEIGHTED_AGG.format(in_window=_in_window(window))
                    if PREAGGREGATE
                    else _SAFETY_WEIGHTED_DIRECT.format(
                        h3_column=h3_column, weight_lookup=_WEIGHT_LOOKUP
                    )
                )
                cur.execute(
                    _SAFETY_SQL.format(weighted=weighted),
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                        "scheme": scheme.version,
                        "eb_prior": scheme.eb_prior,
                        "self_weight": scheme.self_weight,
                        "per_capita": scheme.per_capita,
                        "jobs_weight": scheme.jobs_weight,
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
    --
    -- Exposure is the same 24-hour figure in every block, which is the honest
    -- limit of this layer: residents and jobs are where people sleep and work,
    -- not where they are at 3am. It still beats area, which does not vary
    -- either and does not even track population.
    SELECT
        g.h3_index,
        g.area_km2,
        CASE WHEN %(per_capita)s
             THEN COALESCE(e.residents, 0) + COALESCE(e.jobs, 0) * %(jobs_weight)s
             ELSE g.area_km2
        END AS exposure,
        b.hour_block
    FROM gold.cell_geometry g
    LEFT JOIN gold.cell_exposure e
           ON e.source_id = g.source_id
          AND e.h3_index  = g.h3_index
          AND e.h3_res    = g.h3_res
    CROSS JOIN generate_series(0, %(hour_blocks)s - 1) AS b(hour_block)
    WHERE g.source_id = %(source_id)s AND g.h3_res = %(h3_res)s
),
joined AS (
    SELECT
        u.h3_index, u.area_km2, u.exposure, u.hour_block,
        COALESCE(x.n_violent, 0)     AS n_violent,
        COALESCE(x.n_non_violent, 0) AS n_non_violent,
        COALESCE(x.w_violent, 0)     AS w_violent,
        COALESCE(x.w_non_violent, 0) AS w_non_violent
    FROM universe u
    LEFT JOIN weighted x
           ON x.h3_index = u.h3_index AND x.hour_block = u.hour_block
),
unpivoted AS (
    SELECT h3_index, area_km2, exposure, hour_block, track, n, w
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
    SELECT track, hour_block, sum(w) / NULLIF(sum(exposure), 0) AS city_rate
    FROM unpivoted
    GROUP BY track, hour_block
),
adjusted AS (
    SELECT
        u.h3_index, u.area_km2, u.exposure, u.hour_block, u.track, u.n, u.w,
        -- Same Poisson-gamma posterior as the all-hours ranking. The prior is
        -- an exposure, so it shrinks by exposure/(exposure + prior) regardless
        -- of how much offense the cell carries -- which means slicing the
        -- window into 24 does not quietly change how hard the prior bites.
        (u.w + COALESCE(c.city_rate, 0) * %(eb_prior)s)
            / NULLIF(u.exposure + %(eb_prior)s, 0) AS adj
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
    exposure, weighted_per_1k,
    safety_percentile, safety_rank, city_cell_total, safety_tier,
    baseline_percentile, percentile_delta, hour_index, refreshed_at
)
SELECT
    %(source_id)s, r.h3_index, %(h3_res)s, %(time_window)s, r.hour_block,
    r.track, %(scheme)s,
    %(window_start)s, %(window_end)s, r.n,
    r.w, r.w / r.area_km2, r.smoothed,
    CASE WHEN %(per_capita)s THEN r.exposure END,
    CASE WHEN %(per_capita)s THEN r.w / NULLIF(r.exposure / 1000.0, 0) END,
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
    # Before the DELETE, so a per-capita scheme with no population is never
    # rebuilt prior-only (every cell tied) -- the same guard as the all-hours
    # ranking, over the resolutions this layer builds (P1).
    if scheme.per_capita:
        _require_exposure(conn, source_id, scheme, HOURLY_RESOLUTIONS)
    written = 0
    with conn.cursor() as cur:
        for res in HOURLY_RESOLUTIONS:
            sql = _HOUR_SAFETY_SQL.format(
                h3_column=_h3_column(res), weight_lookup=_WEIGHT_LOOKUP
            )
            # Every window, not just the in-scope ones: the DELETE is what makes
            # narrowing HOURLY_WINDOWS reclaim disk instead of stranding rows no
            # later refresh will revisit, and what lets it be widened again with
            # no migration. Same shape as refresh_cell_activity.
            cur.execute(
                """
                DELETE FROM gold.cell_hour_safety
                WHERE source_id = %s AND h3_res = %s AND scheme_version = %s
                """,
                (source_id, res, scheme.version),
            )
            for window in windows:
                if window.name not in HOURLY_WINDOWS or not safety_builds(window.name):
                    continue
                cur.execute(
                    sql,
                    {
                        "source_id": source_id,
                        "h3_res": res,
                        "time_window": window.name,
                        "window_start": window.start,
                        "window_end": window.end,
                        "scheme": scheme.version,
                        "eb_prior": scheme.eb_prior,
                        "self_weight": scheme.self_weight,
                        "per_capita": scheme.per_capita,
                        "jobs_weight": scheme.jobs_weight,
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
    with conn.cursor() as cur:
        for res in HOURLY_RESOLUTIONS:
            sql = _HOUR_PROFILE_SQL.format(h3_column=_h3_column(res))
            # Delete across every window, then skip; see refresh_cell_hour_safety.
            cur.execute(
                "DELETE FROM gold.cell_hour_profile WHERE source_id = %s AND h3_res = %s",
                (source_id, res),
            )
            for window in windows:
                if window.name not in HOURLY_WINDOWS:
                    continue
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


# Once the hour is known from the source itself, the stored timestamp can be
# checked against it. A modal shift of 0 means occurred_at holds a local wall
# clock; a shift of 19 or 20 (that is, -5 or -4) means it is a true UTC instant
# and the adapter has been mislabelling it, which would affect occurred_year and
# every window boundary, not just this layer.
_HOUR_SHIFT_SQL = """
SELECT
    mod(
        (occurred_local_hour
         - EXTRACT(hour FROM occurred_at AT TIME ZONE 'UTC')::int + 24)::int,
        24
    )            AS shift,
    count(*)     AS n
FROM silver.incident
WHERE source_id = %(source_id)s AND occurred_local_hour IS NOT NULL
GROUP BY 1
ORDER BY n DESC
LIMIT 3
"""


def log_hour_shift(conn: psycopg.Connection, source_id: str) -> None:
    """Report how the published hour relates to the stored timestamp."""
    with conn.cursor() as cur:
        cur.execute(_HOUR_SHIFT_SQL, {"source_id": source_id})
        rows = cur.fetchall()
    if not rows:
        return
    total = sum(r["n"] for r in rows)
    top = rows[0]
    log.info(
        "published hour vs stored timestamp: %s",
        ", ".join(f"{r['shift']}h in {r['n'] / total * 100:.1f}%" for r in rows),
    )
    if top["shift"] != 0:
        log.warning(
            "the stored occurred_at is offset from the published hour by %s hours "
            "for most records, so it is a true UTC instant rather than the local "
            "wall clock it is treated as. The hourly layer is unaffected -- it "
            "uses the published hour -- but occurred_year and the window "
            "boundaries are derived from that timestamp and are worth revisiting.",
            (24 - top["shift"]) % 24,
        )


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

    log_hour_shift(conn, source_id)

    rows = 0
    for scheme in schemes:
        try:
            rows += refresh_cell_hour_safety(conn, source_id, windows, scheme)
        except LookupError as exc:
            # A scheme that cannot be built is skipped, not fatal, exactly as in
            # refresh_safety_layer: _require_exposure raises rather than producing
            # a uniform ranking, but nothing else in the hourly layer depends on
            # this scheme, so it must not abort the rest of the gold refresh.
            log.error(
                "cannot build the hourly ranking for scheme '%s' for %s, skipping it: %s",
                scheme.version,
                source_id,
                exc,
            )
            continue
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

_OFFENSE_MIX_RANKED_DIRECT = """
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
"""

_OFFENSE_MIX_RANKED_AGG = """
    SELECT
        h3_index,
        raw_offense_text,
        min(nibrs_code)       AS nibrs_code,
        min(product_category) AS product_category,
        sum(n)                AS n,
        row_number() OVER (
            PARTITION BY h3_index
            ORDER BY sum(n) DESC, raw_offense_text
        ) AS rn
    FROM _mix_agg
    WHERE h3_res = %(h3_res)s AND {in_window}
    GROUP BY 1, 2
"""

_OFFENSE_MIX_SQL = """
WITH ranked AS (
{ranked}
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
    # The trend sparkline covers at most MONTHLY_SPAN, not the widest window: a
    # city with twenty years of history would otherwise store ten times the
    # monthly rows for a chart that draws two years legibly.
    spans = [w for w in windows if w.name == MONTHLY_SPAN]
    widest = spans[0] if spans else max(windows, key=lambda w: (w.end - w.start).days)
    monthly_rows = 0
    mix_rows = 0

    with conn.cursor() as cur:
        if PREAGGREGATE:
            _create_period_table(cur, windows)
            _build_mix_aggregate(cur, source_id, windows)
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

            cur.execute(
                "DELETE FROM gold.cell_offense_mix WHERE source_id = %s AND h3_res = %s",
                (source_id, res),
            )
            for window in windows:
                # The detail panel at resolution 10 only ever asks for the
                # windows the map is built for there.
                if not activity_builds(res, window.name):
                    continue
                ranked = (
                    _OFFENSE_MIX_RANKED_AGG.format(in_window=_in_window(window))
                    if PREAGGREGATE
                    else _OFFENSE_MIX_RANKED_DIRECT.format(h3_column=h3_column)
                )
                cur.execute(
                    _OFFENSE_MIX_SQL.format(ranked=ranked),
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
    severity_scheme_version, severity_weight_coverage, hour_known_share,
    ambient_population, population_vintage, jobs_vintage
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
    r.severity_scheme_version, %(weight_coverage)s, %(hour_coverage)s,
    exposure.ambient, exposure.pop_vintage, exposure.jobs_vintage
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
CROSS JOIN LATERAL (
    -- Read off the blocks rather than the apportioned cells: this is the
    -- denominator as published, before any of this pipeline's arithmetic
    -- touched it, which is the figure worth showing on a methodology page.
    SELECT
        NULLIF(COALESCE(sum(pop20), 0) + COALESCE(sum(jobs), 0), 0)::double precision
            AS ambient,
        CASE WHEN count(*) > 0 THEN 2020 END::smallint AS pop_vintage,
        max(jobs_year)::smallint AS jobs_vintage
    FROM reference.census_block WHERE source_id = r.source_id
) exposure
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
        EXCLUDED.hour_known_share, city_snapshot.hour_known_share),
    ambient_population       = EXCLUDED.ambient_population,
    population_vintage       = EXCLUDED.population_vintage,
    jobs_vintage             = EXCLUDED.jobs_vintage
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
        try:
            rows += refresh_cell_safety(conn, source_id, windows, scheme)
        except LookupError as exc:
            # A scheme that cannot be built is skipped, not fatal. _require_exposure
            # raises rather than producing a uniform map, which is right -- but the
            # granularity was wrong: raising here aborted the whole gold refresh,
            # so a per-capita scheme with no census loaded rolled back cell_activity
            # and the area-based ranking too, and left the map empty. Nothing about
            # those depends on this scheme.
            #
            # Still loud, and still not silently uniform: the scheme's rows are
            # simply absent, so the serving layer's LEFT JOIN finds nothing and the
            # map falls back to counts.
            log.error(
                "cannot build severity scheme '%s' for %s, skipping it: %s",
                scheme.version,
                source_id,
                exc,
            )
            if scheme.version == active:
                log.error(
                    "'%s' is the scheme %s actually serves, so its safety ranking "
                    "will be empty until this is resolved",
                    scheme.version,
                    source_id,
                )
            continue
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


def refresh_cell_exposure(
    conn: psycopg.Connection, source_id: str, rebuild: bool = False
) -> dict[int, int]:
    """Top up the population denominator, if this city has one loaded.

    Imported here rather than at module scope: safety.etl.census pulls in pyshp
    and httpx, and a plain `gold` refresh on a city with no census data should
    not need either.
    """
    from safety.etl import census

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*)::int AS n FROM reference.census_block WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    if not row or not row["n"]:
        log.warning(
            "no census blocks loaded for '%s'; skipping the exposure layer. "
            "A per-capita severity scheme cannot be built until "
            "`python -m safety.etl.run census --city %s` has run.",
            source_id,
            source_id,
        )
        return {}

    try:
        return census.build_cell_exposure(conn, source_id, rebuild=rebuild)
    except LookupError as exc:
        # Same granularity argument as refresh_safety_layer: a denominator that
        # cannot be extended is not a reason to roll back the rest of the gold
        # refresh. It happens when the block polygons have been released and the
        # cell universe has since grown -- an incident landing in a cell no
        # previous pull reached.
        #
        # Those cells end up with no exposure row, which the ranking SQL already
        # handles: the LEFT JOIN yields zero exposure and the scheme's
        # credibility prior bounds it, so the cell is ranked near the citywide
        # rate rather than dividing by zero. That is a worse figure than a real
        # apportionment, for a handful of edge cells, and it is why this is an
        # error and not a warning.
        log.error("cannot extend the exposure layer for %s: %s", source_id, exc)
        return {}

# ---------------------------------------------------------------------------
# The per-city window list, and the legacy names the previous release reads
# ---------------------------------------------------------------------------


def write_city_windows(conn: psycopg.Connection, source_id: str, windows: list[Window]) -> int:
    """Record which windows this city is built for, and what each one carries.

    Written in the refresh's own transaction, so the API never offers a window
    before its rows exist. The flags come from the same scope functions the
    layer refreshes use, so they cannot disagree with what was built.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gold.city_window WHERE source_id = %s", (source_id,))
        cur.executemany(
            """
            INSERT INTO gold.city_window (
                source_id, time_window, sort_order, window_start, window_end,
                data_start, partial, safety_built, hourly_built, res10_built
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    source_id,
                    w.name,
                    order,
                    w.start,
                    w.end,
                    w.data_start or w.start,
                    w.partial,
                    safety_builds(w.name),
                    w.name in HOURLY_WINDOWS and safety_builds(w.name),
                    activity_builds(10, w.name),
                )
                for order, w in enumerate(windows)
            ],
        )
    return len(windows)


def _table_columns(conn: psycopg.Connection, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'gold' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r["column_name"] for r in cur.fetchall()]


def write_legacy_windows(
    conn: psycopg.Connection, source_id: str, tables: tuple[str, ...]
) -> int:
    """Copy last_3m / last_1y / last_2y rows under the previous release's names.

    Only while settings.gold_legacy_windows is on (one release): the release
    before this one queries last_90d / last_12m / last_24m, and a rollback to it
    must still find a map. The copies are exact for 12m and 24m, which span the
    same dates as 1y and 2y; last_90d gets three calendar months, a day or two
    wider than it used to be, which a rollback can live with.
    """
    from safety.config import settings

    if not settings.gold_legacy_windows:
        return 0
    copied = 0
    with conn.cursor() as cur:
        for table in tables:
            columns = _table_columns(conn, table)
            select = ", ".join(
                "%(legacy)s" if c == "time_window" else f'"{c}"' for c in columns
            )
            names = ", ".join(f'"{c}"' for c in columns)
            cur.execute(
                f"DELETE FROM gold.{table} WHERE source_id = %s AND time_window = ANY(%s)",
                (source_id, list(LEGACY_WINDOWS)),
            )
            for legacy, current in LEGACY_WINDOWS.items():
                cur.execute(
                    f"INSERT INTO gold.{table} ({names}) SELECT {select} "
                    f"FROM gold.{table} WHERE source_id = %(source_id)s "
                    f"AND time_window = %(current)s",
                    {"legacy": legacy, "current": current, "source_id": source_id},
                )
                copied += cur.rowcount
    log.info("legacy window names for %s: %s rows copied", source_id, copied)
    return copied


ALL_HOURS_TABLES = ("cell_activity", "cell_safety", "cell_offense_mix")
HOURLY_TABLES = ("cell_hour_safety", "cell_hour_profile")


def refresh_all(
    conn: psycopg.Connection,
    source_id: str,
    pipeline_version: str,
    include_hourly: bool = True,
) -> dict[str, Any]:
    """Full gold refresh for one city. Runs as a single transaction.

    `include_hourly=False` leaves the time-of-day layers alone. They are by far
    the most expensive thing here -- the same ranking recomputed 24 times, at two
    resolutions, per scheme -- and also the slowest-moving, since their window
    is a year wide. A day of new incidents moves an
    hourly percentile computed over two years almost not at all.

    Skipping them is therefore the right trade for a frequent incremental, with
    `safety.etl.run hourly` on its own slower schedule. It is a real trade, not a
    free one: until that runs, the hourly view reflects the previous build. The
    all-hours percentile it is compared against does get rebuilt here, so the two
    are briefly derived from different windows of data.
    """
    timings: dict[str, float] = {}
    clock = time.monotonic()

    def lap(phase: str) -> None:
        nonlocal clock
        now = time.monotonic()
        timings[phase] = round(now - clock, 2)
        clock = now

    anchor = data_anchor(conn, source_id)
    if anchor is None:
        raise LookupError(f"no silver rows for '{source_id}'; nothing to roll up")

    cells = build_cell_universe(conn, source_id)
    windows = city_windows(conn, source_id, anchor)
    log.info(
        "gold anchor date %s; windows: %s",
        anchor,
        ", ".join(f"{w.name}[{w.start}..{w.end}]" for w in windows),
    )
    lap("cell_universe")

    # Exposure is keyed on the cell universe, so it has to follow it and precede
    # anything that divides by it. Skipped, with a warning, when no census data
    # has been loaded -- an area-denominated scheme still builds fine without it,
    # and refusing here would make the census pull a hard prerequisite of every
    # gold refresh rather than of the per-capita ranking specifically.
    exposure_cells = refresh_cell_exposure(conn, source_id)
    lap("exposure")

    activity_rows = refresh_cell_activity(conn, source_id, windows)
    lap("activity")
    safety_rows, coverage = refresh_safety_layer(conn, source_id, windows)
    lap("safety")

    hour_rows: int | None = None
    hour_profile_rows: int | None = None
    hour_share: float | None = None
    if include_hourly:
        # After the all-hours ranking, never before: the hourly layer's second
        # rating is a comparison against the percentile that one produces.
        hour_rows, hour_profile_rows, hour_share = refresh_hourly_layer(
            conn, source_id, windows
        )
        lap("hourly")
    else:
        # Said out loud. A silently stale layer is the failure mode this whole
        # module is written against, and hour_known_share passing as None below
        # keeps the snapshot's previous figure rather than blanking it.
        log.info(
            "skipping the time-of-day layers for %s; run "
            "`safety.etl.run hourly --city %s` to rebuild them",
            source_id,
            source_id,
        )

    monthly_rows, mix_rows = refresh_cell_detail(conn, source_id, windows)
    lap("detail")
    write_city_windows(conn, source_id, windows)
    write_legacy_windows(
        conn, source_id, ALL_HOURS_TABLES + (HOURLY_TABLES if include_hourly else ())
    )
    refresh_city_snapshot(conn, source_id, pipeline_version, coverage, hour_share)
    conn.commit()
    lap("snapshot_and_commit")
    log.info("gold refresh for %s took %s", source_id, timings)

    return {
        "cells_r8": cells.get(8, 0),
        "cells_r9": cells.get(9, 0),
        "cells_r10": cells.get(10, 0),
        "cell_exposure_rows": sum(exposure_cells.values()),
        "cell_activity_rows": activity_rows,
        "cell_safety_rows": safety_rows,
        # None, not 0: "not rebuilt this run" and "rebuilt and produced nothing"
        # are different outcomes and the caller prints this.
        "cell_hour_safety_rows": hour_rows,
        "cell_hour_profile_rows": hour_profile_rows,
        "hourly_skipped": not include_hourly,
        "severity_weight_coverage": round(coverage, 4) if coverage is not None else None,
        "cell_monthly_rows": monthly_rows,
        "cell_offense_mix_rows": mix_rows,
        "windows": [w.name for w in windows],
        "timings_seconds": timings,
    }
