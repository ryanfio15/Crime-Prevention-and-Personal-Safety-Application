"""The exposure denominator: census population and workplace jobs.

The safety ranking used to divide severity-weighted offence by a cell's area,
which made it partly a map of where Philadelphia is busy. This module builds
what it divides by instead: **ambient population**, residents plus jobs,
apportioned from census blocks into H3 cells.

Residents alone would be worse than area, not better. The airport, the Navy
Yard, Fairmount Park and Penn's Landing carry real incident counts over almost
no resident count, and dividing by that alone ranks them the least safe places
in the city by division rather than by evidence. Jobs are what stop a place
being scored as empty when it is only empty at night.

Two sources, both keyed on 2020 census blocks so they join on GEOID with no
crosswalk:

* **TIGER/Line TABBLOCK20** -- block polygons carrying POP20 and HOUSING20.
  Only the February 2022 and later vintage has those columns; the original
  release has the same filename and stops at INTPTLON20, so the loader checks
  for them rather than trusting the URL.
* **LEHD LODES v8 WAC** -- total jobs by workplace block, enumerated on 2020
  blocks specifically so it joins to the above.

Both land in bronze first, exactly like a police department's data does: "what
did the source publish on this date" is the same question here (S5, S9.2).

S13 bars joining crime data to demographic layers for display. Nothing here
reads race, income, or any other characteristic -- only head counts, used as a
denominator. The methodology endpoint states that distinction on the record.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import shapefile

from safety.config import settings
from safety.etl.adapters.base import RawChunk, SourceConfig

log = logging.getLogger(__name__)

# Resolutions the exposure layer is built for, and therefore the resolutions the
# per-capita ranking can be built for.
#
# Not 10, and the reason is the same one that kept resolutions 11 and 12 out of
# 008. A resolution-10 cell is ~0.015 km2, smaller than a typical city block;
# Philadelphia has roughly 25,000 of them against ~18,900 census blocks. Any
# population figure at that size is this module's areal-apportionment
# assumption handed back as though it were a measurement. Resolution 8 averages
# ~34 blocks per cell and resolution 9 about five, which is where apportionment
# error and the 2020 Census disclosure noise both average down far enough to
# rank on.
EXPOSURE_RESOLUTIONS = (8, 9)

# Vintages. Population is decennial and will not move until 2030; LODES is
# annual, and bumping this is the whole maintenance burden of the jobs half.
POP_VINTAGE = 2020
LODES_YEAR = 2023

_TIGER_URL = (
    "https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/"
    "tl_2020_{state_fips}_tabblock20.zip"
)
_LODES_URL = (
    "https://lehd.ces.census.gov/data/lodes/LODES8/{state}/wac/"
    "{state}_wac_S000_JT00_{year}.csv.gz"
)

BLOCKS_DATASET = "census_blocks"
JOBS_DATASET = "lodes_wac"

# Columns the Feb-2022 TABBLOCK20 release added. Their absence is the one
# failure mode worth naming precisely, because the wrong file downloads happily.
_REQUIRED_DBF_FIELDS = ("GEOID20", "POP20", "HOUSING20", "ALAND20")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _get(url: str) -> httpx.Response:
    """GET with bounded exponential backoff, same policy as the source adapters.

    These are large one-off files on government hosts that rate-limit rather
    than refuse, so a retry is usually the right answer to a 5xx.
    """
    last_exc: Exception | None = None
    for attempt in range(1, settings.http_max_retries + 1):
        try:
            response = httpx.get(
                url,
                timeout=settings.http_timeout_seconds,
                follow_redirects=True,
            )
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"upstream {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            backoff = 2.0**attempt
            log.warning(
                "census request failed (attempt %s/%s), retrying in %.0fs: %s",
                attempt,
                settings.http_max_retries,
                backoff,
                exc,
            )
            time.sleep(backoff)
    raise RuntimeError(f"census request failed after retries: {last_exc}")


def _require_fips(config: SourceConfig) -> tuple[str, list[str]]:
    if not config.state_fips or not config.county_fips:
        raise LookupError(
            f"source '{config.source_id}' has no state_fips/county_fips in the "
            "registry, so there is no way to know which census blocks are its "
            "own; set them and re-run"
        )
    return config.state_fips, list(config.county_fips)


def fetch_blocks(config: SourceConfig) -> RawChunk:
    """Download the state's TIGER/Line tabulation-block shapefile."""
    state_fips, _ = _require_fips(config)
    url = _TIGER_URL.format(state_fips=state_fips)
    log.info("fetching census blocks: %s", url)
    response = _get(url)
    return RawChunk(
        name=f"tl_2020_{state_fips}_tabblock20.zip",
        content_type="application/zip",
        payload=response.content,
        request_url=str(response.url),
        fetched_at=datetime.now(timezone.utc),
        meta={"vintage": POP_VINTAGE, "state_fips": state_fips},
    )


def fetch_jobs(config: SourceConfig, year: int = LODES_YEAR) -> RawChunk:
    """Download the state's LODES Workplace Area Characteristics file."""
    state_fips, _ = _require_fips(config)
    state = _STATE_BY_FIPS.get(state_fips)
    if state is None:
        raise LookupError(
            f"no LODES state abbreviation known for FIPS '{state_fips}'; "
            "add it to _STATE_BY_FIPS"
        )
    url = _LODES_URL.format(state=state, year=year)
    log.info("fetching workplace jobs: %s", url)
    response = _get(url)
    return RawChunk(
        name=f"{state}_wac_S000_JT00_{year}.csv.gz",
        content_type="application/gzip",
        payload=response.content,
        request_url=str(response.url),
        fetched_at=datetime.now(timezone.utc),
        meta={"vintage": year, "state": state},
    )


# LODES paths are keyed by postal abbreviation while the registry carries FIPS.
# Only the states the six Phase 1/2 cities sit in, so an unknown code fails
# loudly rather than building a wrong URL.
_STATE_BY_FIPS = {
    "42": "pa",  # Philadelphia
    "17": "il",  # Chicago
    "53": "wa",  # Seattle
    "06": "ca",  # Los Angeles
    "48": "tx",  # Austin
    "11": "dc",  # Washington, DC
}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_blocks(conn: psycopg.Connection, payload: bytes, config: SourceConfig) -> int:
    """Read the TIGER shapefile and upsert this city's blocks.

    The zip is extracted to a temp directory rather than read out of the archive
    in place: pyshp seeks in the .shx index, and a stream out of zipfile is not
    dependably seekable.
    """
    state_fips, counties = _require_fips(config)
    wanted = set(counties)

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(tmp)
        stem = _shapefile_stem(Path(tmp))

        reader = shapefile.Reader(str(stem))
        fields = [f[0] for f in reader.fields[1:]]
        missing = [f for f in _REQUIRED_DBF_FIELDS if f not in fields]
        if missing:
            raise ValueError(
                f"{stem.name} is missing {missing}. This is the pre-February-2022 "
                "TIGER/Line release, which ships the same filename without the "
                "2020 Census counts. Re-download from "
                "https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/"
            )

        idx = {name: i for i, name in enumerate(fields)}
        payload_rows: list[tuple[Any, ...]] = []
        for shape_record in reader.iterShapeRecords():
            record = shape_record.record
            if record[idx["COUNTYFP20"]] not in wanted:
                continue
            payload_rows.append(
                (
                    record[idx["GEOID20"]],
                    config.source_id,
                    int(record[idx["POP20"]] or 0),
                    int(record[idx["HOUSING20"]] or 0),
                    int(record[idx["ALAND20"]] or 0),
                    _as_multipolygon_geojson(shape_record.shape.__geo_interface__),
                )
            )
        reader.close()

    if not payload_rows:
        raise LookupError(
            f"no blocks matched state {state_fips} county {sorted(wanted)} in the "
            "shapefile; check the registry's county_fips"
        )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO reference.census_block
                (geoid20, source_id, pop20, housing20, aland20, geom, loaded_at)
            VALUES (
                %s, %s, %s, %s, %s,
                ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                now()
            )
            ON CONFLICT (geoid20) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                pop20     = EXCLUDED.pop20,
                housing20 = EXCLUDED.housing20,
                aland20   = EXCLUDED.aland20,
                geom      = EXCLUDED.geom,
                loaded_at = EXCLUDED.loaded_at
            """,
            payload_rows,
        )
    conn.commit()

    residents = sum(row[2] for row in payload_rows)
    log.info(
        "loaded %s census blocks for %s (%s residents, %s vintage)",
        len(payload_rows),
        config.source_id,
        f"{residents:,}",
        POP_VINTAGE,
    )
    return len(payload_rows)


def _shapefile_stem(root: Path) -> Path:
    """Locate the extracted .shp, whatever the archive chose to call it."""
    candidates = sorted(root.rglob("*.shp"))
    if not candidates:
        raise LookupError("no .shp in the TIGER archive")
    return candidates[0].with_suffix("")


def _as_multipolygon_geojson(geometry: dict[str, Any]) -> str:
    """Normalize pyshp's geometry to MultiPolygon, matching the column type.

    TIGER publishes most blocks as a single Polygon and a few -- blocks split by
    water, mostly -- as MultiPolygon. ST_Multi would promote either, but doing
    it here keeps what reaches PostGIS uniform.
    """
    if geometry.get("type") == "Polygon":
        geometry = {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}
    return json.dumps(geometry)


def load_jobs(
    conn: psycopg.Connection, payload: bytes, source_id: str, year: int = LODES_YEAR
) -> int:
    """Attach LODES workplace job counts to the blocks already loaded.

    The WAC file covers a whole state; only the rows whose block is already in
    reference.census_block for this city do anything, so the filtering is the
    join rather than a pre-pass.
    """
    text = gzip.decompress(payload).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if "w_geocode" not in (reader.fieldnames or []) or "C000" not in (reader.fieldnames or []):
        raise ValueError(
            f"LODES WAC file has unexpected columns {reader.fieldnames}; "
            "expected at least w_geocode and C000"
        )

    # The WAC file covers the whole state -- a few hundred thousand blocks -- of
    # which only this city's are wanted. Filtering in Python against the loaded
    # set first turns a few hundred thousand UPDATE round trips into about
    # nineteen thousand.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geoid20 FROM reference.census_block WHERE source_id = %s",
            (source_id,),
        )
        known = {r["geoid20"] for r in cur.fetchall()}
    if not known:
        raise LookupError(
            f"no census blocks loaded for '{source_id}'; load the blocks before "
            "the jobs that attach to them"
        )

    rows = [
        (int(r["C000"] or 0), year, r["w_geocode"])
        for r in reader
        if r["w_geocode"] in known
    ]

    with conn.cursor() as cur:
        # Blocks with no WAC row have no jobs recorded, which is a real zero
        # rather than a gap: LODES omits blocks where nobody works.
        cur.execute(
            "UPDATE reference.census_block SET jobs = 0, jobs_year = %s WHERE source_id = %s",
            (year, source_id),
        )
        cur.executemany(
            """
            UPDATE reference.census_block
               SET jobs = %s, jobs_year = %s
             WHERE geoid20 = %s
            """,
            rows,
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(sum(jobs), 0)::bigint AS jobs,
                   count(*) FILTER (WHERE jobs > 0)::int AS blocks_with_jobs
            FROM reference.census_block WHERE source_id = %s
            """,
            (source_id,),
        )
        totals = cur.fetchone() or {}
    log.info(
        "attached %s jobs across %s blocks for %s (LODES %s)",
        f"{totals.get('jobs', 0):,}",
        totals.get("blocks_with_jobs", 0),
        source_id,
        year,
    )
    _log_job_outliers(conn, source_id)
    return len(rows)


def _log_job_outliers(conn: psycopg.Connection, source_id: str, top: int = 5) -> None:
    """Surface the blocks carrying implausible job counts.

    LODES assigns jobs to the establishment where it can, but some employers --
    school districts and transit agencies especially -- land entirely on their
    headquarters block. That is a known artifact of the source, and an artifact
    named in a log is a different thing from one quietly inside a denominator.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT geoid20, jobs, pop20
            FROM reference.census_block
            WHERE source_id = %s AND jobs IS NOT NULL
            ORDER BY jobs DESC LIMIT %s
            """,
            (source_id, top),
        )
        for row in cur.fetchall():
            log.info(
                "  top jobs block %s: %s jobs, %s residents",
                row["geoid20"],
                f"{row['jobs']:,}",
                f"{row['pop20']:,}",
            )


# ---------------------------------------------------------------------------
# Apportionment
# ---------------------------------------------------------------------------

_EXPOSURE_SQL = """
INSERT INTO gold.cell_exposure
    (source_id, h3_index, h3_res, residents, jobs, block_count,
     pop_vintage, jobs_vintage, built_at)
SELECT
    g.source_id,
    g.h3_index,
    g.h3_res,
    -- Areal apportionment: a block's people are split across the cells it
    -- overlaps in proportion to how much of its area falls in each. The
    -- assumption is that a block is internally uniform, which is the best
    -- available -- the Census Bureau publishes nothing finer.
    COALESCE(sum(b.pop20            * f.frac), 0),
    COALESCE(sum(COALESCE(b.jobs,0) * f.frac), 0),
    count(b.geoid20)::int,
    %(pop_vintage)s,
    max(b.jobs_year),
    now()
FROM gold.cell_geometry g
LEFT JOIN reference.census_block b
       ON b.source_id = g.source_id
      AND b.geom && g.boundary
LEFT JOIN LATERAL (
    SELECT ST_Area(ST_Intersection(b.geom, g.boundary))
         / NULLIF(ST_Area(b.geom), 0) AS frac
) f ON true
WHERE g.source_id = %(source_id)s
  AND g.h3_res = ANY(%(resolutions)s)
GROUP BY g.source_id, g.h3_index, g.h3_res
"""


def build_cell_exposure(conn: psycopg.Connection, source_id: str) -> dict[int, int]:
    """Materialize gold.cell_exposure for every exposure resolution.

    A LEFT JOIN, so every cell in the universe gets a row -- including the ones
    no block reaches. A cell with zero ambient population is a real statement
    about that cell (the airside of the airport, the middle of the river), and
    it still has to be ranked; the scheme's credibility prior is what keeps it
    from dividing by zero.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM gold.cell_exposure WHERE source_id = %s", (source_id,)
        )
        cur.execute(
            _EXPOSURE_SQL,
            {
                "source_id": source_id,
                "resolutions": list(EXPOSURE_RESOLUTIONS),
                "pop_vintage": POP_VINTAGE,
            },
        )

        cur.execute(
            """
            SELECT h3_res,
                   count(*)::int              AS cells,
                   sum(residents)             AS residents,
                   sum(jobs)                  AS jobs,
                   count(*) FILTER (WHERE residents + jobs < 1)::int AS near_empty
            FROM gold.cell_exposure
            WHERE source_id = %s
            GROUP BY h3_res ORDER BY h3_res
            """,
            (source_id,),
        )
        built = cur.fetchall()

        cur.execute(
            """
            SELECT COALESCE(sum(pop20), 0)::bigint AS residents,
                   COALESCE(sum(jobs), 0)::bigint  AS jobs
            FROM reference.census_block WHERE source_id = %s
            """,
            (source_id,),
        )
        source_totals = cur.fetchone() or {"residents": 0, "jobs": 0}
    conn.commit()

    counts: dict[int, int] = {}
    for row in built:
        counts[row["h3_res"]] = row["cells"]
        # Apportionment should conserve people. It will not conserve them
        # exactly: blocks straddling the city boundary give part of themselves
        # to cells outside the universe. A large gap means the boundary or the
        # county filter is wrong, which is worth saying out loud at build time.
        retained = (
            row["residents"] / source_totals["residents"]
            if source_totals["residents"]
            else 0.0
        )
        log.info(
            "cell_exposure res %s: %s cells, %s residents + %s jobs "
            "(%.1f%% of the block total retained), %s cell(s) near-empty",
            row["h3_res"],
            row["cells"],
            f"{row['residents']:,.0f}",
            f"{row['jobs']:,.0f}",
            retained * 100,
            row["near_empty"],
        )
        if retained < 0.95:
            log.warning(
                "res %s kept only %.1f%% of the city's census population; the "
                "cell universe may not cover the blocks, or county_fips may be wrong",
                row["h3_res"],
                retained * 100,
            )

    if not counts:
        raise LookupError(
            f"no exposure rows built for '{source_id}'; the cell universe is "
            "empty at resolutions "
            f"{list(EXPOSURE_RESOLUTIONS)} -- run the gold rollups first"
        )
    return counts


def city_ambient_total(conn: psycopg.Connection, source_id: str) -> dict[str, Any]:
    """Citywide residents/jobs behind the ranking, for gold.city_snapshot."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(sum(pop20), 0)::double precision AS residents,
                   COALESCE(sum(jobs), 0)::double precision  AS jobs,
                   max(jobs_year) AS jobs_vintage
            FROM reference.census_block WHERE source_id = %s
            """,
            (source_id,),
        )
        row = cur.fetchone() or {}
    return {
        "ambient_population": (row.get("residents") or 0) + (row.get("jobs") or 0),
        "population_vintage": POP_VINTAGE if row.get("residents") else None,
        "jobs_vintage": row.get("jobs_vintage"),
    }
