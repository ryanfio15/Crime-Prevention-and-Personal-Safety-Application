"""FastAPI serving layer (design doc S9.4).

Exposes exactly the operations the client needs -- cell activity by location,
neighbouring cells in a ring, city metadata and boundaries, offense-category
filters -- over the precomputed gold tables. Nothing here aggregates; if an
endpoint would need to scan silver, that is a signal the ETL is missing a
rollup, not a reason to compute it per request.

Run:  python -m uvicorn safety.api.main:app --reload
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from safety import PIPELINE_VERSION
from safety.api import repository as repo
from safety.api.ratelimit import RateLimitMiddleware
from safety.config import WEB_DIR, settings
from safety.h3grid import RESOLUTIONS, cell_resolution, cells_for_point, grid_disk, is_valid_cell

log = logging.getLogger(__name__)

pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = ConnectionPool(
        settings.dsn,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row},
        # Restarting the database container kills every pooled connection, and
        # without this the pool keeps handing the dead ones out until it is
        # bounced. Validate on checkout so the API rides out a `docker compose
        # restart` instead of 500ing until uvicorn is restarted too.
        check=ConnectionPool.check_connection,
        open=True,
    )
    pool.wait(timeout=30)
    log.info("connection pool ready")
    yield
    pool.close()


app = FastAPI(
    title="Crime Prevention & Personal Safety API",
    version=PIPELINE_VERSION,
    description=(
        "Phase 1 serving layer over precomputed H3 cell rollups for Philadelphia. "
        "Values are reported-incident density relative to other cells in the same "
        "city -- not a risk score, and not a prediction."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

API = "/api/v1"

# Only /api/v1/ paths are limited, so the static frontend and /docs are
# untouched -- see safety/api/ratelimit.py for the two zones and why they differ.
if settings.enable_rate_limit:
    app.add_middleware(RateLimitMiddleware)


def get_conn():
    assert pool is not None, "connection pool not initialised"
    with pool.connection() as conn:
        yield conn


Conn = Annotated[Any, Depends(get_conn)]


# ---------------------------------------------------------------------------
# Cache (design doc S9.5)
#
# Stands in for the Redis layer: same read pattern (the map re-requests the
# same few layer configurations constantly), and the same invalidation rule --
# keyed on the ETL's own refresh timestamp rather than a fixed global TTL,
# because refresh cadence differs per city (S8.2).
# ---------------------------------------------------------------------------

_cache: dict[tuple, tuple[float, str, Any]] = {}
_CACHE_MAX_ENTRIES = 256
_cache_stats = {"hits": 0, "misses": 0}


def _refresh_stamp(conn) -> str:
    version = repo.serving_version(conn)
    return str(version.get("last_refreshed_at"))


def cached(conn, key: tuple, producer):
    stamp = _refresh_stamp(conn)
    hit = _cache.get(key)
    if hit is not None and hit[1] == stamp:
        _cache_stats["hits"] += 1
        return hit[2]

    _cache_stats["misses"] += 1
    value = producer()
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[key] = (time.time(), stamp, value)
    return value


# ---------------------------------------------------------------------------
# Health and metadata
# ---------------------------------------------------------------------------


@app.get(f"{API}/health", tags=["meta"])
def health(conn: Conn) -> dict[str, Any]:
    version = repo.serving_version(conn)
    return {
        "status": "ok",
        "pipeline_version": PIPELINE_VERSION,
        "data_as_of": version.get("data_as_of"),
        "last_refreshed_at": version.get("last_refreshed_at"),
        "incidents": version.get("incident_count"),
        "cache": dict(_cache_stats),
    }


@app.get(f"{API}/version", tags=["meta"])
def version(conn: Conn) -> dict[str, Any]:
    """Small poll target: the client reloads its layer when this changes."""
    return repo.serving_version(conn)


@app.get(f"{API}/cities", tags=["cities"])
def cities(conn: Conn) -> dict[str, Any]:
    return {"cities": repo.list_cities(conn)}


@app.get(f"{API}/cities/{{source_id}}", tags=["cities"])
def city(source_id: str, conn: Conn) -> dict[str, Any]:
    record = repo.get_city(conn, source_id)
    if record is None:
        raise HTTPException(404, f"no serving data for city '{source_id}'")
    return record


@app.get(f"{API}/categories", tags=["cities"])
def categories(conn: Conn, city: str = "phl") -> dict[str, Any]:
    return {
        "city": city,
        "labels": repo.CATEGORY_LABELS,
        "tiers": repo.TIER_LABELS,
        "windows": repo.WINDOW_LABELS,
        "resolutions": list(RESOLUTIONS),
        "categories": repo.categories(conn, city),
    }


@app.get(f"{API}/quality", tags=["meta"])
def quality(conn: Conn, city: str = "phl") -> dict[str, Any]:
    return repo.data_quality(conn, city)


# ---------------------------------------------------------------------------
# The map layer
# ---------------------------------------------------------------------------


def _validate_layer(res: int, window: str, category: str) -> None:
    if res not in repo.VALID_RESOLUTIONS:
        raise HTTPException(400, f"res must be one of {list(repo.VALID_RESOLUTIONS)}")
    if window not in repo.VALID_WINDOWS:
        raise HTTPException(400, f"window must be one of {list(repo.VALID_WINDOWS)}")
    if category not in repo.VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {list(repo.VALID_CATEGORIES)}")


@app.get(f"{API}/cells", tags=["cells"])
def cells(
    conn: Conn,
    city: str = "phl",
    res: int = 8,
    window: str = "last_12m",
    category: str = "all",
    min_count: int = Query(0, ge=0),
    bbox: str | None = Query(
        None, description="Viewport filter as 'west,south,east,north' in WGS84 degrees"
    ),
) -> Response:
    """The H3 hexagon layer as GeoJSON, coloured client-side from `count`."""
    _validate_layer(res, window, category)

    parsed_bbox: tuple[float, float, float, float] | None = None
    if bbox:
        try:
            west, south, east, north = (float(part) for part in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be 'west,south,east,north'") from None
        parsed_bbox = (west, south, east, north)

    key = ("cells", city, res, window, category, min_count, parsed_bbox)
    document = cached(
        conn,
        key,
        lambda: repo.cells_geojson(
            conn,
            source_id=city,
            h3_res=res,
            time_window=window,
            category=category,
            min_count=min_count,
            bbox=parsed_bbox,
        ),
    )
    return Response(
        content=json.dumps(document, default=str),
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.get(f"{API}/cells/ring", tags=["cells"])
def cells_ring(
    conn: Conn,
    h3: str,
    k: int = Query(1, ge=0, le=6),
    window: str = "last_12m",
    category: str = "all",
) -> dict[str, Any]:
    """S10: activity for a cell plus its k-ring of neighbours.

    The ring itself is an O(1) H3 operation, and the lookup is by primary key
    -- no bounding-box or spatial query is involved at any point.
    """
    if not is_valid_cell(h3):
        raise HTTPException(400, f"'{h3}' is not a valid H3 index")
    _validate_layer(cell_resolution(h3), window, category)

    indexes = grid_disk(h3, k)
    rows = repo.cell_ring(conn, h3_indexes=indexes, time_window=window, category=category)
    return {
        "origin": h3,
        "k": k,
        "requested": len(indexes),
        "resolved": len(rows),
        "window": window,
        "category": category,
        "cells": rows,
    }


@app.get(f"{API}/cells/lookup", tags=["cells"])
def cells_lookup(
    conn: Conn,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    res: int = 8,
    window: str = "last_12m",
) -> dict[str, Any]:
    """Resolve a coordinate to its cell and return that cell's rollup.

    S10 notes the client can do the H3 step itself, offline, with no server
    round trip; this endpoint exists for the geocoded-address path, where the
    lookup is already happening server-side.
    """
    _validate_layer(res, window, "all")
    cell = cells_for_point(lat, lng)[res]
    detail = repo.cell_detail(conn, h3_index=cell, time_window=window)
    if detail is None:
        return {
            "h3": cell,
            "in_coverage": False,
            "message": "That location falls outside the covered city boundary.",
        }
    return {"h3": cell, "in_coverage": True, **detail}


@app.get(f"{API}/cells/{{h3_index}}", tags=["cells"])
def cell(
    conn: Conn,
    h3_index: str,
    window: str = "last_12m",
) -> dict[str, Any]:
    if not is_valid_cell(h3_index):
        raise HTTPException(400, f"'{h3_index}' is not a valid H3 index")
    if window not in repo.VALID_WINDOWS:
        raise HTTPException(400, f"window must be one of {list(repo.VALID_WINDOWS)}")

    detail = repo.cell_detail(conn, h3_index=h3_index, time_window=window)
    if detail is None:
        raise HTTPException(404, f"cell '{h3_index}' is not in the covered area")
    return detail


@app.get(f"{API}/summary", tags=["cells"])
def summary(
    conn: Conn,
    city: str = "phl",
    window: str = "last_12m",
    res: int = 8,
) -> dict[str, Any]:
    _validate_layer(res, window, "all")
    return {
        "city": city,
        "window": window,
        "window_label": repo.WINDOW_LABELS[window],
        "totals": repo.city_totals(conn, source_id=city, time_window=window, h3_res=res),
    }


# ---------------------------------------------------------------------------
# Methodology (design doc S12, S13)
# ---------------------------------------------------------------------------


def _safety_measure(conn, record: dict[str, Any]) -> dict[str, Any]:
    """Explain the safety ranking, including where its weights fall back.

    S13 asks the product to be explicit about what the underlying data can
    support. A severity-weighted ranking adds a second thing needing disclosure
    on top of the counts -- whose severity judgements these are, and where the
    numbers ran out.
    """
    scheme = repo.severity_scheme(conn, record["source_id"])
    coverage = record.get("severity_weight_coverage")
    return {
        "what_it_is": (
            "Two separate rankings -- one for violent offences, one for non-violent -- "
            "of how much weighted offence a cell carries against every other cell in "
            "the same city. 1.0 is the safest cell on that track, 0 the least safe."
        ),
        "why_two_rankings": (
            "The FBI's own combined Crime Index counted a murder and a shoplifting as "
            "one each, and the CJIS Advisory Policy Board discontinued it in June 2004 "
            "because the total was always driven by whichever offence was most "
            "numerous -- normally larceny-theft, which made up 59.7% of the 2001 Index "
            "against murder's 0.1%. The FBI has published violent and property totals "
            "separately ever since, and this follows that."
        ),
        "weights": {
            "source": scheme["source_citation"] if scheme else None,
            "scheme_version": scheme["scheme_version"] if scheme else None,
            "note": (
                "Offence severity is not an FBI figure -- the FBI publishes no "
                "per-offence weights. These come from a 1977 survey in which about "
                "60,000 people rated the seriousness of 204 criminal events, scaled so "
                "that a score twice as high means twice as serious."
            ),
            "published_share": round(coverage, 4) if coverage is not None else None,
            "fallback_note": (
                "Offences with no matching item in the survey fall back to the median "
                "weight of their UCR Part I / Part II bucket. Those fallbacks are "
                "marked as such in the weight table rather than presented as published "
                "figures."
            ),
        },
        "smoothing": {
            "credibility_prior_km2": scheme["eb_prior_km2"] if scheme else None,
            "self_weight": scheme["self_weight"] if scheme else None,
            "note": (
                "A cell's own figure is trusted in proportion to how much ground it "
                "covers, and is then blended with its immediate neighbours. "
                "Without this, a single serious incident in an otherwise empty cell "
                "would rank that cell the least safe in the city on a sample of one. "
                "A consequence worth knowing: a quiet cell surrounded by busy ones is "
                "pulled down, by design."
            ),
        },
        "known_limitations": [
            "There is no population or footfall denominator, so a cell is not adjusted "
            "for how many people pass through it. A business district with few "
            "residents and heavy daytime traffic reads worse than its risk to any one "
            "person warrants.",
            "The severity weights were collected in 1977 and reflect how the American "
            "public ranked seriousness then.",
            "Severity weighting moves the ranking less than might be expected, because "
            "the different reported offence types tend to rise and fall together.",
        ],
    }


@app.get(f"{API}/methodology", tags=["meta"])
def methodology(conn: Conn, city: str = "phl") -> dict[str, Any]:
    record = repo.get_city(conn, city)
    if record is None:
        raise HTTPException(404, f"no serving data for city '{city}'")
    return {
        "what_this_shows": (
            "Counts of crime incidents reported to and recorded by police, aggregated "
            "into roughly 500-metre hexagonal cells (Uber H3 resolution 8, average area "
            "0.74 km²), and ranked relative to other cells in the same city over the "
            "same time window."
        ),
        "what_this_is_not": [
            "Not a prediction. Nothing here forecasts future events.",
            "Not a safety or risk score for an address, a block, or a person. The "
            "safety ranking compares whole cells against other cells in the same "
            "city; it says nothing about any particular street or building.",
            "Not a measure of crime. It measures reported and recorded incidents, "
            "which is a different quantity.",
            "A cell with no reported incidents is not therefore safe. It may be a "
            "place where crime goes unreported, which is why those cells are shown "
            "in a neutral colour rather than at the safe end of the scale.",
        ],
        "known_limitations": [
            "Reported crime is shaped by how willing people are to report and by where "
            "police are deployed. Historically under-reported offence types and "
            "historically over-enforced ones do not appear here in proportion to how "
            "often they actually occur.",
            "Coordinates are published at block level by the source agency, so no "
            "reading below roughly a city block is meaningful.",
            "Philadelphia publishes police dispatch times, not observed occurrence "
            "times.",
            "Recent records are preliminary and are revised and reclassified by the "
            "department after first publication.",
        ],
        "no_demographic_overlays": (
            "Crime data is never joined to race, income, or other demographic layers "
            "for display."
        ),
        "cell_model": {
            "grid": "Uber H3",
            "primary_resolution": 8,
            "primary_resolution_note": "~461 m edge, ~0.74 km² average area",
            "detail_resolution": 9,
            "detail_resolution_note": "~174 m edge, ~0.105 km² average area",
            "fine_resolution": 10,
            "fine_resolution_note": (
                "~76 m across, ~0.015 km² average area. This is the finest cell "
                "offered, and deliberately so: the source publishes coordinates "
                "rounded to the block, so a smaller cell would show that rounding "
                "rather than where crime happened."
            ),
            "relative_measure": (
                "Each cell's percentile is the fraction of cells in the same city with "
                "strictly lower reported-incident density for the same window and "
                "category. Tier 0 means nothing was reported in the cell, which is a "
                "different statement from being in the quietest fifth."
            ),
        },
        "safety_measure": _safety_measure(conn, record),
        "classification": {
            "standard": "FBI NIBRS offense codes, with the coarser UCR Part I / Part II "
            "split retained as a fallback where a precise NIBRS mapping is ambiguous.",
            "crosswalk_version": record["crosswalk_version"],
            "raw_codes_preserved": True,
        },
        "attribution": record["attribution_text"],
        "terms_url": record["terms_url"],
        "data_as_of": record["data_as_of"],
        "update_cadence": record["expected_cadence"],
        "freshness_note": record["freshness_note"],
        "location_precision_note": record["location_precision_note"],
        "coverage": {
            "start": record["coverage_start"],
            "end": record["coverage_end"],
            "incidents": record["incident_count"],
        },
    }


# The temporary front end. Mounted last so it never shadows an API route.
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
