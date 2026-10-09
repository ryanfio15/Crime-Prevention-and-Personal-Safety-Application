"""FastAPI serving layer (design doc S9.4).

Exposes exactly the operations the client needs -- cell activity by location,
neighbouring cells in a ring, city metadata and boundaries, offense-category
filters -- over the precomputed gold tables. Nothing here aggregates; if an
endpoint would need to scan silver, that is a signal the ETL is missing a
rollup, not a reason to compute it per request.

Run:  python -m uvicorn safety.api.main:app --reload
"""

from __future__ import annotations

import concurrent.futures
import gzip
import json
import logging
import math
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from safety import PIPELINE_VERSION
from safety.api import repository as repo
from safety.api.ratelimit import RateLimitMiddleware
from safety.api.security import SecurityHeadersMiddleware
from safety.config import REPO_ROOT, WEB_DIR, settings
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
        # statement_timeout lives here, on the API's own connections, and never
        # on the role or the database (ALTER ROLE/DATABASE ... SET): the ETL and
        # migrate share both and legitimately run for minutes. A migration that
        # holds an ACCESS EXCLUSIVE lock for longer than this makes queued reads
        # answer 503 rather than hang.
        kwargs={
            "row_factory": dict_row,
            "options": f"-c statement_timeout={settings.api_statement_timeout_ms}",
        },
        # How long a request waits for a free connection. Pool exhaustion now
        # fails in 10 s with a 503 instead of hanging /health with everything else.
        timeout=settings.api_pool_timeout_seconds,
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
        "Serving layer over precomputed H3 cell rollups, one city at a time. "
        "Values are reported-incident density relative to other cells in the same "
        "city -- not a risk score, not a prediction, and never comparable between "
        "cities: every percentile is computed against that city's own distribution."
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

# A whole-city resolution-10 layer is ~14 MB of GeoJSON, nearly all of it
# repeated coordinate digits, and it compresses about ten to one. Without this
# the finest cell size is only usable on a fast connection, which is the
# opposite of what S2's mobile-first resident/commuter segment needs.
# compresslevel=6 rather than starlette's default 9: level 9 costs several
# times the CPU for a few per cent smaller output. /cells bypasses this
# entirely -- its layers are cached already gzipped (cells(), below).
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

# Added last, so it is outermost: the headers land on every response, the rate
# limiter's 429s included (safety/api/security.py).
app.add_middleware(SecurityHeadersMiddleware)


_BUSY = {"detail": "The service is busy; retry shortly."}


@app.exception_handler(PoolTimeout)
@app.exception_handler(QueryCanceled)
@app.exception_handler(concurrent.futures.TimeoutError)
async def _busy(request: Request, exc: Exception) -> JSONResponse:
    """Saturation and runaway queries are a "come back shortly", not a 500.

    PoolTimeout: no connection freed up within api_pool_timeout_seconds.
    QueryCanceled: statement_timeout cancelled a query. TimeoutError: a request
    waiting on another's identical cache miss gave up (cached()). All are transient from
    the client's side, so they get 503 with Retry-After; the smoke check treats a
    503 from /health as a failure, which is right.
    """
    log.warning("503 for %s: %s: %s", request.url.path, type(exc).__name__, exc)
    return JSONResponse(_BUSY, status_code=503, headers={"Retry-After": "5"})


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

# Values are serialized payloads, not objects, so a hit costs no re-encoding and
# the size of an entry is something this module can actually measure. /cells
# stores its layers gzip-compressed (cells()), so the byte budget and the
# `cache.bytes` figure in /health count compressed bytes for those entries.
#
# An OrderedDict, used least-recently-used: the entry that has gone longest
# without a read is the one evicted. With one city, clearing the whole cache on
# overflow was the cheaper choice -- the map re-requested the same few layers
# constantly and refilled within a handful of requests. Six cities break that.
# The working set is now six times larger, and one pass over Los Angeles at
# resolution 10, which is several times the size of Philadelphia's ~14 MB layer,
# would evict every other city's layers on its way through. Evicting one entry
# at a time is what stops a large city from repeatedly wiping the small ones.
_cache: OrderedDict[tuple, tuple[str, str | bytes]] = OrderedDict()
_cache_stats = {"hits": 0, "misses": 0, "bytes": 0, "evictions": 0}


def _refresh_stamp(conn, source_id: str | None = None) -> str | None:
    """The stamp a cached entry is validated against.

    Per city (S8.2: refresh cadence differs per city). Keying on the aggregate
    would invalidate Philadelphia's cached layers every time Los Angeles
    refreshed, which with a bi-weekly source against a daily one is most days.

    None for a city with no snapshot -- an unknown or not-yet-built city. Such
    responses are never cached: stamped "None", any string in `city` would have
    taken a cache slot.
    """
    version = repo.serving_version(conn, source_id)
    refreshed = version.get("last_refreshed_at")
    return None if refreshed is None else str(refreshed)


# Thread safety. The endpoints are plain `def`s, so Starlette runs them on its
# threadpool and several can be inside cached() at once. Every read or write of
# _cache, _cache_stats and _inflight happens under _cache_lock; the producer --
# the database query -- runs outside it, so one slow layer never blocks hits on
# the others.
#
# Single flight. Without it, N concurrent misses for the same layer (a refresh
# landing while the map is busy, or one user's burst) ran the same multi-second
# query N times. Now the first miss for a (key, stamp) is the leader and builds
# it; the rest wait on its Future and share the result -- or its exception.
#
# Known trade-off: a follower waits while holding its own pooled connection (the
# Conn dependency is acquired before the endpoint runs). A burst of more than
# eight identical misses can therefore exhaust the pool for up to the leader's
# runtime, and the excess gets the 503 from api_pool_timeout_seconds. That is
# still strictly better than each of them running the same query.
_cache_lock = threading.Lock()
_inflight: dict[tuple, concurrent.futures.Future] = {}


def _store(key: tuple, stamp: str, value: str | bytes) -> None:
    """Admit `value` under `key`. The caller holds _cache_lock (not re-entrant:
    this must never take it itself)."""
    # Re-read under the lock: the entry seen before the producer ran may have
    # been replaced or evicted since. A stale entry for this key is dropped
    # before accounting for the new one, or `bytes` drifts upward by the size of
    # every layer ever refreshed.
    stale = _cache.pop(key, None)
    if stale is not None:
        _cache_stats["bytes"] -= len(stale[1])

    # One entry alone can exceed the budget (a whole-city res-10 layer on a large
    # city). Serve it, but do not try to store it -- admitting it would evict
    # everything else and still not fit.
    if len(value) > settings.cache_max_bytes:
        return

    while _cache and (
        len(_cache) >= settings.cache_max_entries
        or _cache_stats["bytes"] + len(value) > settings.cache_max_bytes
    ):
        _, evicted = _cache.popitem(last=False)
        _cache_stats["bytes"] -= len(evicted[1])
        _cache_stats["evictions"] += 1

    _cache[key] = (stamp, value)
    _cache_stats["bytes"] += len(value)


def cached(
    conn, key: tuple, producer, source_id: str | None = None, store: bool = True
) -> str | bytes:
    """`producer()`'s value, from the cache when the city's refresh stamp matches.

    `store=False` (ad-hoc filters, see cells()) and a city with no stamp bypass
    the cache entirely -- no lookup, no single flight, nothing stored -- so a
    caller walking free-form parameters cannot evict the layers real map
    requests use.
    """
    if not store:
        return producer()
    stamp = _refresh_stamp(conn, source_id)
    if stamp is None:
        return producer()
    flight_key = (key, stamp)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            _cache.move_to_end(key)
            _cache_stats["hits"] += 1
            return hit[1]
        pending = _inflight.get(flight_key)
        leader = pending is None
        if leader:
            pending = _inflight[flight_key] = concurrent.futures.Future()

    if not leader:
        # Another request is already building this exact layer at this stamp;
        # wait for it instead of building it again. Bounded a little past the
        # leader's own statement_timeout; a TimeoutError here is answered 503.
        with _cache_lock:
            _cache_stats["hits"] += 1
        return pending.result(timeout=settings.api_statement_timeout_ms / 1000 + 5)

    try:
        value = producer()
    except BaseException as exc:
        with _cache_lock:
            _inflight.pop(flight_key, None)
        pending.set_exception(exc)
        raise

    with _cache_lock:
        _cache_stats["misses"] += 1
        _store(key, stamp, value)
        _inflight.pop(flight_key, None)
    pending.set_result(value)
    return value


def cached_json(conn, key: tuple, producer, source_id: str | None):
    """Small JSON-able results through the same stamped cache; plain types only.

    The round trip is lossless for the str/int/float rows it is used for.
    Never pass datetimes through it: FastAPI renders those with a trailing Z,
    json.dumps cannot render them at all.
    """
    return json.loads(
        cached(conn, key, lambda: json.dumps(producer()).encode("utf-8"), source_id=source_id)
    )


# ---------------------------------------------------------------------------
# Health and metadata
# ---------------------------------------------------------------------------


def _deployed_commit() -> str | None:
    """The full sha deploy/lib/install.sh wrote next to the code, if any.

    Read once at import: a deploy always restarts uvicorn, so the file cannot
    change under a running process. The auto deployer compares this with the
    sha it just installed to know the restart picked up the new tree. None on a
    checkout that was never deployed (local development, the Docker image).
    """
    try:
        return (REPO_ROOT / "DEPLOYED_COMMIT").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


DEPLOYED_COMMIT = _deployed_commit()


def _cache_stats_snapshot() -> dict[str, int]:
    with _cache_lock:
        return dict(_cache_stats)


@app.get(f"{API}/health", tags=["meta"])
def health(conn: Conn) -> dict[str, Any]:
    version = repo.serving_version(conn)
    return {
        "status": "ok",
        "commit": DEPLOYED_COMMIT,
        "pipeline_version": PIPELINE_VERSION,
        "data_as_of": version.get("data_as_of"),
        "last_refreshed_at": version.get("last_refreshed_at"),
        "incidents": version.get("incident_count"),
        "cache": _cache_stats_snapshot(),
    }


@app.get(f"{API}/version", tags=["meta"])
def version(conn: Conn, city: str | None = None) -> dict[str, Any]:
    """Small poll target: the client reloads its layer when this changes.

    Pass `city` to poll one city. The client does, because it displays one city
    at a time: without it, a bi-weekly Los Angeles refresh would make every
    Philadelphia viewer reload a layer that had not changed.
    """
    return repo.serving_version(conn, city)


def city_windows(conn, source_id: str) -> list[dict[str, Any]]:
    """The windows a city is served for, cached on that city's refresh stamp."""
    return cached_json(
        conn, ("windows", source_id), lambda: repo.city_windows(conn, source_id), source_id
    )


def _with_windows(conn, record: dict[str, Any]) -> dict[str, Any]:
    windows = city_windows(conn, record["source_id"])
    default = repo.match_window(repo.DEFAULT_WINDOW, windows)
    return {
        **record,
        "windows": windows,
        "default_window": (default or (windows[-1] if windows else {"id": None}))["id"],
        # For a custom range: the calendar runs from selectable_start (on the
        # record; NULL until the city is rebuilt with gold.cell_daily) and the
        # data stops at coverage_end. The caveats are matched to whatever range
        # the client picks.
        "custom_range_resolutions": (
            list(repo.DAILY_RESOLUTIONS) if record.get("selectable_start") else []
        ),
        "series_caveats": cached_json(
            conn,
            ("caveats", record["source_id"]),
            lambda: repo.series_caveats(conn, record["source_id"]),
            record["source_id"],
        ),
    }


@app.get(f"{API}/cities", tags=["cities"])
def cities(conn: Conn) -> dict[str, Any]:
    return {"cities": [_with_windows(conn, r) for r in repo.list_cities(conn)]}


@app.get(f"{API}/cities/{{source_id}}", tags=["cities"])
def city(source_id: str, conn: Conn) -> dict[str, Any]:
    record = repo.get_city(conn, source_id)
    if record is None:
        raise HTTPException(404, f"no serving data for city '{source_id}'")
    return _with_windows(conn, record)


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
    # The etl.* findings stay live; the two silver GROUP BYs are cached by the
    # city's refresh stamp, so they are exactly as fresh as the map. Key order is
    # unchanged: validation_issues, recent_pulls, coordinate_provenance,
    # offense_mapping_confidence.
    return {
        **repo.data_quality(conn, city),
        **cached_json(
            conn, ("silver_mix", city), lambda: repo.silver_provenance_mix(conn, city), city
        ),
    }


# ---------------------------------------------------------------------------
# The map layer
# ---------------------------------------------------------------------------


def _resolve_window(conn, city: str | None, window: str) -> dict[str, Any]:
    """The served window a request names, for this city.

    Windows are per city (gold.city_window): a city with two years of history
    has no last_10y. Legacy names are accepted as aliases. A city with no
    windows at all -- unknown, or not built yet -- gets the name checked
    against the pattern only, so it answers with an empty layer as before
    rather than a 400 that blames the window.
    """
    if repo.canonical_window(window) is None:
        raise HTTPException(
            400,
            "window must be last_3m, last_6m, last_9m or last_<N>y "
            "(see /api/v1/cities for each city's list)",
        )
    windows = city_windows(conn, city) if city else []
    if not windows:
        return {"id": repo.canonical_window(window), "hourly": True, "res10": True, "safety": True}
    match = repo.match_window(window, windows)
    if match is None:
        raise HTTPException(
            400,
            f"'{window}' is not built for city '{city}', which has "
            f"{', '.join(w['id'] for w in windows)}",
        )
    return match


def _resolve_range(
    conn, city: str | None, start: date | None, end: date | None
) -> tuple[dict[str, Any], repo.DateRange | None]:
    """The served window, or the custom range, two calendar dates name.

    A range that equals one of the city's stored windows is served as that
    window -- the presets on the map always are -- so it keeps everything the
    stored build carries, the time-of-day layer included. Anything else is
    ranked on request from gold.cell_daily (repo.DateRange).

    `end` may run past the newest reported date (the map defaults to today);
    it is clamped there, so "today" and "the day the data stops" are one cache
    entry. `start` may not run before the oldest date the city holds.
    """
    if start is None or end is None:
        raise HTTPException(400, "from and to go together: both dates, YYYY-MM-DD")
    if start > end:
        raise HTTPException(400, "from must be on or before to")
    # A day of slack: the browser picks "today" in its own time zone.
    if end > date.today() + timedelta(days=1):
        raise HTTPException(400, "to cannot be in the future")
    record = repo.get_city(conn, city) if city else None
    if record is None:
        raise HTTPException(400, f"no serving data for city '{city}'")
    first = record.get("selectable_start")
    if first is None:
        raise HTTPException(
            400,
            f"custom date ranges are not built for '{city}' yet; use one of its "
            "windows (see /api/v1/cities)",
        )
    if start < first:
        raise HTTPException(
            400, f"from must be on or after {first.isoformat()}, the oldest date '{city}' holds"
        )
    last = record["coverage_end"]
    clamped = max(start, min(end, last))

    for window in city_windows(conn, city):
        if window.get("start") == start.isoformat() and window.get("end") == clamped.isoformat():
            return window, None

    span_days = (clamped - start).days + 1
    limit = settings.safety_max_window_years
    safety = limit is None or span_days <= limit * 366
    served = {
        "id": None,
        "label": repo.range_label(start, clamped),
        "start": start.isoformat(),
        "end": clamped.isoformat(),
        "span_days": span_days,
        "hourly": False,
        "res10": False,
        "safety": safety,
    }
    return served, repo.DateRange(start, clamped, safety)


def _resolve_period(
    conn, city: str | None, window: str, start: date | None, end: date | None
) -> tuple[dict[str, Any], repo.DateRange | None]:
    """`from`/`to` when either is given, otherwise the named window."""
    if start is not None or end is not None:
        return _resolve_range(conn, city, start, end)
    return _resolve_window(conn, city, window), None


def _validate_range(res: int, date_range: repo.DateRange | None, hour: int | None) -> None:
    """What a custom range cannot be served with, said before the generic checks
    (which would blame a window the request never named)."""
    if date_range is None:
        return
    if res not in repo.DAILY_RESOLUTIONS:
        raise HTTPException(
            400,
            f"custom date ranges are served at res {list(repo.DAILY_RESOLUTIONS)}; "
            f"at res {res} pick one of the city's windows -- a cell that size holds "
            "too little over most ranges to rank, and it is not rolled up by day",
        )
    if hour is not None:
        raise HTTPException(
            400,
            "the time-of-day layer is built for the stored last-12-months window "
            "only; pick that window rather than a custom range to use an hour",
        )


def _period_key(served: dict[str, Any], date_range: repo.DateRange | None):
    """The cache-key part for a window or a range."""
    if date_range is None:
        return served["id"]
    return ("range", date_range.start.isoformat(), date_range.end.isoformat())


def _validate_layer(res: int, window: dict[str, Any], category: str) -> None:
    if res not in repo.VALID_RESOLUTIONS:
        raise HTTPException(400, f"res must be one of {list(repo.VALID_RESOLUTIONS)}")
    if category not in repo.VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {list(repo.VALID_CATEGORIES)}")

    # Same principle as _validate_hour: an unbuilt combination would otherwise
    # come back as a layer of zeroes, which is indistinguishable from a city
    # where nothing was reported. Told, with the reason.
    _, categories = repo.activity_scope(res)
    if res == 10 and not window["res10"]:
        raise HTTPException(
            400,
            f"res {res} is built for windows {list(repo.ACTIVITY_WINDOWS[10])} "
            "only -- a cell that size holds too little over a shorter window for "
            "a percentile to separate anything, and the longer windows are not "
            "worth their size at a cell this small. Use a coarser resolution for "
            "the other windows.",
        )
    if category not in categories:
        raise HTTPException(
            400,
            f"res {res} is built for category {list(categories)} only -- splitting "
            "a cell that size by offense category leaves almost every cell empty "
            "in every category, so the ranking would be a field of ties. Use a "
            "coarser resolution to break the layer down by category.",
        )


def _safety_available(res: int) -> bool:
    """Whether the per-capita ranking exists at this cell size.

    Not an error on /cells: the activity layer is served at every resolution and
    the safety fields simply ride along where they exist. It is an error when a
    caller asks for the ranking specifically, which is what _require_safety_res
    is for -- an empty measure and an absent one look identical otherwise.
    """
    return res in repo.SAFETY_RESOLUTIONS


def _require_safety_res(res: int) -> None:
    if not _safety_available(res):
        raise HTTPException(
            400,
            f"the safety ranking is built for res {list(repo.SAFETY_RESOLUTIONS)} "
            f"only -- it divides by ambient population apportioned from census "
            f"blocks, and a res {res} cell is smaller than a census block, so its "
            "population would be an apportionment assumption rather than a "
            "measurement. The incident-count layer is served at every resolution.",
        )


def _validate_hour(hour: int | None, res: int, window: dict[str, Any]) -> None:
    """Reject an hour the pipeline does not build, with the reason.

    An unbuilt combination would otherwise return a layer whose hourly fields
    are all null, which looks identical to "nothing happens here at 3am".
    """
    if hour is None:
        return
    if hour not in repo.VALID_HOURS:
        raise HTTPException(400, "hour must be an integer from 0 to 23")
    if res not in repo.HOURLY_RESOLUTIONS:
        raise HTTPException(
            400,
            f"the time-of-day layer is built for res {list(repo.HOURLY_RESOLUTIONS)} "
            f"only -- a window split 24 ways at res {res} leaves too few incidents "
            "per cell to rank",
        )
    if not window["hourly"]:
        raise HTTPException(
            400,
            f"the time-of-day layer is built for the last 12 months only "
            f"({', '.join(repo.HOURLY_WINDOWS)}) -- shorter windows do not carry "
            "enough incidents once split across 24 hour blocks, and longer ones "
            "would multiply the largest table in the database",
        )


@app.get(f"{API}/cells", tags=["cells"])
def cells(
    request: Request,
    conn: Conn,
    city: str = "phl",
    res: int = 8,
    window: str = repo.DEFAULT_WINDOW,
    category: str = "all",
    min_count: int = Query(0, ge=0),
    hour: int | None = Query(
        None,
        ge=0,
        le=23,
        description=(
            "Local hour block 0-23; block h covers [h:00, h+1:00). Adds the "
            "time-of-day ratings to every feature."
        ),
    ),
    measure: str | None = Query(
        None,
        description=(
            "Assert which measure the caller intends to render. Omit to get "
            "whatever exists at this resolution; pass 'safety' to be told with a "
            "400, rather than with nulls, when the ranking is not built here."
        ),
    ),
    bbox: str | None = Query(
        None, description="Viewport filter as 'west,south,east,north' in WGS84 degrees"
    ),
    from_: date | None = Query(
        None,
        alias="from",
        description="Custom range start, YYYY-MM-DD (with `to`; replaces `window`).",
    ),
    to: date | None = Query(
        None, description="Custom range end, YYYY-MM-DD, inclusive (with `from`)."
    ),
) -> Response:
    """The H3 hexagon layer as GeoJSON, coloured client-side from `count`.

    For a stored window (`window`), or for any two dates (`from`, `to`).
    """
    served, date_range = _resolve_period(conn, city, window, from_, to)
    _validate_range(res, date_range, hour)
    _validate_layer(res, served, category)
    _validate_hour(hour, res, served)
    window = served["id"]
    if measure is not None:
        if measure not in ("activity", "safety"):
            raise HTTPException(400, "measure must be 'activity' or 'safety'")
        if measure == "safety":
            _require_safety_res(res)

    parsed_bbox: tuple[float, float, float, float] | None = None
    if bbox:
        try:
            west, south, east, north = (float(part) for part in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be 'west,south,east,north'") from None
        parsed_bbox = (west, south, east, north)
        # float() accepts "nan" and "inf", which would reach SQL as nonsense
        # and give every variant its own cache key.
        if not all(math.isfinite(v) for v in parsed_bbox):
            raise HTTPException(400, "bbox values must be finite numbers")

    key = (
        "cells", city, res, _period_key(served, date_range), category, min_count, hour,
        parsed_bbox,
    )
    payload = cached(
        conn,
        key,
        # Encoded *and compressed* inside the cache, not after it. At resolution
        # 10 the document is ~14 MB; re-encoding it cost more than the query that
        # built it, and GZipMiddleware re-compressed it at level 9 on every hit.
        # Stored as gzip bytes, a hit is a memory copy, and the cache's byte
        # budget holds about ten times as many layers.
        lambda: gzip.compress(
            json.dumps(
                repo.cells_geojson(
                    conn,
                    source_id=city,
                    h3_res=res,
                    time_window=window,
                    category=category,
                    min_count=min_count,
                    hour=hour,
                    bbox=parsed_bbox,
                    date_range=date_range,
                ),
                default=str,
            ).encode("utf-8"),
            compresslevel=6,
        ),
        source_id=city,
        # Only the shapes the shipped map requests are cached (it sends neither
        # bbox nor min_count, web/app.js loadLayer); free-form filters are
        # served fresh and cannot evict real layers.
        store=parsed_bbox is None and min_count == 0,
    )
    headers = {"Cache-Control": "public, max-age=60", "Vary": "Accept-Encoding"}
    # The same naive substring test starlette's GZipMiddleware uses: a client
    # sending `gzip;q=0` would still get gzip, which no real browser does.
    # GZipMiddleware and nginx (gzip_proxied) both pass a response that already
    # carries Content-Encoding through untouched, so this is gzipped exactly once.
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(
            payload,
            media_type="application/geo+json",
            headers={**headers, "Content-Encoding": "gzip"},
        )
    # Rare: a client that cannot take gzip gets it inflated here, once per request.
    return Response(gzip.decompress(payload), media_type="application/geo+json", headers=headers)


@app.get(f"{API}/cells/ring", tags=["cells"])
def cells_ring(
    conn: Conn,
    h3: str,
    k: int = Query(1, ge=0, le=6),
    window: str = repo.DEFAULT_WINDOW,
    category: str = "all",
    from_: date | None = Query(
        None,
        alias="from",
        description="Custom range start, YYYY-MM-DD (with `to`; replaces `window`).",
    ),
    to: date | None = Query(
        None, description="Custom range end, YYYY-MM-DD, inclusive (with `from`)."
    ),
) -> dict[str, Any]:
    """S10: activity for a cell plus its k-ring of neighbours.

    The ring itself is an O(1) H3 operation, and the lookup is by primary key
    -- no bounding-box or spatial query is involved at any point.
    """
    if not is_valid_cell(h3):
        raise HTTPException(400, f"'{h3}' is not a valid H3 index")
    source = repo.cell_source(conn, h3)
    res = cell_resolution(h3)
    served, date_range = _resolve_period(conn, source, window, from_, to)
    _validate_range(res, date_range, None)
    _validate_layer(res, served, category)
    window = served["id"]

    indexes = grid_disk(h3, k)
    rows = repo.cell_ring(
        conn,
        h3_indexes=indexes,
        time_window=window,
        category=category,
        date_range=date_range,
        source_id=source,
        h3_res=res,
    )
    return {
        "origin": h3,
        "k": k,
        "requested": len(indexes),
        "resolved": len(rows),
        "window": window if date_range is None else repo.RANGE_WINDOW,
        "range": served if date_range else None,
        "category": category,
        "cells": rows,
    }


@app.get(f"{API}/cells/lookup", tags=["cells"])
def cells_lookup(
    conn: Conn,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    res: int = 8,
    window: str = repo.DEFAULT_WINDOW,
    from_: date | None = Query(
        None,
        alias="from",
        description="Custom range start, YYYY-MM-DD (with `to`; replaces `window`).",
    ),
    to: date | None = Query(
        None, description="Custom range end, YYYY-MM-DD, inclusive (with `from`)."
    ),
) -> dict[str, Any]:
    """Resolve a coordinate to its cell and return that cell's rollup.

    S10 notes the client can do the H3 step itself, offline, with no server
    round trip; this endpoint exists for the geocoded-address path, where the
    lookup is already happening server-side.
    """
    if res not in repo.VALID_RESOLUTIONS:
        raise HTTPException(400, f"res must be one of {list(repo.VALID_RESOLUTIONS)}")
    cell = cells_for_point(lat, lng)[res]
    source = repo.cell_source(conn, cell)
    if source is None:
        return {
            "h3": cell,
            "in_coverage": False,
            "message": "That location falls outside the covered city boundary.",
        }
    served, date_range = _resolve_period(conn, source, window, from_, to)
    _validate_range(res, date_range, None)
    _validate_layer(res, served, "all")
    detail = repo.cell_detail(
        conn, h3_index=cell, time_window=served["id"], date_range=date_range
    )
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
    window: str = repo.DEFAULT_WINDOW,
    hour: int | None = Query(None, ge=0, le=23),
    from_: date | None = Query(
        None,
        alias="from",
        description="Custom range start, YYYY-MM-DD (with `to`; replaces `window`).",
    ),
    to: date | None = Query(
        None, description="Custom range end, YYYY-MM-DD, inclusive (with `from`)."
    ),
) -> dict[str, Any]:
    if not is_valid_cell(h3_index):
        raise HTTPException(400, f"'{h3_index}' is not a valid H3 index")
    source = repo.cell_source(conn, h3_index)
    if source is None and (from_ is not None or to is not None):
        raise HTTPException(404, f"cell '{h3_index}' is not in the covered area")
    served, date_range = _resolve_period(conn, source, window, from_, to)
    _validate_range(cell_resolution(h3_index), date_range, hour)
    _validate_hour(hour, cell_resolution(h3_index), served)

    detail = repo.cell_detail(
        conn, h3_index=h3_index, time_window=served["id"], hour=hour, date_range=date_range
    )
    if detail is None:
        raise HTTPException(404, f"cell '{h3_index}' is not in the covered area")
    return detail


@app.get(f"{API}/summary", tags=["cells"])
def summary(
    conn: Conn,
    city: str = "phl",
    window: str = repo.DEFAULT_WINDOW,
    res: int = 8,
    from_: date | None = Query(
        None,
        alias="from",
        description="Custom range start, YYYY-MM-DD (with `to`; replaces `window`).",
    ),
    to: date | None = Query(
        None, description="Custom range end, YYYY-MM-DD, inclusive (with `from`)."
    ),
) -> dict[str, Any]:
    served, date_range = _resolve_period(conn, city, window, from_, to)
    _validate_range(res, date_range, None)
    _validate_layer(res, served, "all")
    window = served["id"]
    return {
        "city": city,
        "window": window if date_range is None else repo.RANGE_WINDOW,
        "window_label": served["label"] if date_range else repo.window_label(window),
        "range": served if date_range else None,
        "totals": repo.city_totals(
            conn, source_id=city, time_window=window, h3_res=res, date_range=date_range
        ),
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
    per_capita = (scheme or {}).get("exposure_kind") == "ambient_population"
    return {
        "what_it_is": (
            "Two separate rankings -- one for violent offences, one for non-violent -- "
            "of how much weighted offence a cell carries "
            + (
                "per person present, against every other cell in the same city. "
                if per_capita
                else "against every other cell in the same city. "
            )
            + "1.0 is the safest cell on that track, 0 the least safe."
        ),
        "denominator": _denominator(record, scheme) if per_capita else None,
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
            "credibility_prior": (
                scheme["eb_prior_persons"] if per_capita else scheme["eb_prior_km2"]
            )
            if scheme
            else None,
            "credibility_prior_units": "ambient people" if per_capita else "km²",
            "self_weight": scheme["self_weight"] if scheme else None,
            "note": (
                (
                    "A cell's own figure is trusted in proportion to how many people "
                    "are in it, and is then blended with its immediate neighbours. "
                    if per_capita
                    else "A cell's own figure is trusted in proportion to how much "
                    "ground it covers, and is then blended with its immediate "
                    "neighbours. "
                )
                + "Without this, a single serious incident in an otherwise empty cell "
                "would rank that cell the least safe in the city on a sample of one. "
                "A consequence worth knowing: a quiet cell surrounded by busy ones is "
                "pulled down, by design."
            ),
            "also_the_floor": (
                "It does a second job here. A few cells have almost nobody living or "
                "working in them -- the airside of the airport, the middle of a park "
                "-- and dividing by that alone would send them to the bottom of the "
                "ranking by division rather than by evidence. The prior bounds them."
            )
            if per_capita
            else None,
        },
        "known_limitations": [
            "The severity weights were collected in 1977 and reflect how the American "
            "public ranked seriousness then.",
            "Severity weighting moves the ranking less than might be expected, because "
            "the different reported offence types tend to rise and fall together.",
        ]
        + (
            [
                "Jobs stand in for daytime population; they are not measured "
                "footfall. Somewhere with heavy through-traffic and few workers -- a "
                "transit concourse, a stadium approach on a match day -- still reads "
                "as emptier than it is.",
                "The population figures are from the 2020 Census and the job figures "
                "from 2023, against incidents reported since. Where the city has "
                "built or emptied since then, the denominator lags.",
            ]
            if per_capita
            else [
                "There is no population or footfall denominator, so a cell is not "
                "adjusted for how many people pass through it. A business district "
                "with few residents and heavy daytime traffic reads worse than its "
                "risk to any one person warrants."
            ]
        ),
    }


def _denominator(record: dict[str, Any], scheme: dict[str, Any] | None) -> dict[str, Any]:
    """What the ranking divides by, and why it is not simply residents.

    This is the disclosure the per-capita change most needs to carry. Dividing
    by population is the obvious fix to "the map is really a population map",
    and dividing by *residents* is the obvious way to do it -- and it is wrong
    in a way that is worth stating rather than leaving for a user to discover
    when the airport shows up as the most dangerous place in the city.
    """
    ambient = record.get("ambient_population")
    return {
        "what_it_is": (
            "Ambient population: the people who live in a cell plus the people who "
            "work in it. Reported offence is divided by this rather than by the "
            "cell's area, so a place is measured against how many people are "
            "actually there."
        ),
        "why_not_residents_alone": (
            "Because the places with almost no residents are not empty. "
            + (
                record.get("denominator_examples_note")
                or "Airports, industrial land, parks and central business districts "
                "all have real reported incidents and very few people living in "
                "them."
            )
            + " Dividing those by residents alone would rank them the least safe "
            "places in the city on arithmetic rather than on evidence -- worse "
            "than the area denominator it replaced, not better. Counting workplaces "
            "is what stops a place being scored as deserted when it is only "
            "deserted at night."
        ),
        "sources": [
            "Residents: U.S. Census Bureau, 2020 Census population by tabulation "
            "block (TIGER/Line TABBLOCK20).",
            "Jobs: U.S. Census Bureau, LEHD LODES version 8 Workplace Area "
            "Characteristics, "
            f"{record.get('jobs_vintage') or 'latest available year'}.",
        ],
        "city_ambient_population": round(ambient) if ambient else None,
        "population_vintage": record.get("population_vintage"),
        "jobs_vintage": record.get("jobs_vintage"),
        "jobs_weight": (scheme or {}).get("jobs_weight"),
        "jobs_weight_note": (
            "How much one job counts against one resident. There is no published "
            "figure for this; it is a chosen parameter, and at 1.0 a workplace and "
            "a home count equally."
        ),
        "how_it_reaches_a_cell": (
            "Census blocks are the smallest geography the Census Bureau publishes. "
            "Each block's people are split across the hexagons it overlaps in "
            "proportion to how much of its area falls in each. A resolution-8 "
            "hexagon draws on roughly thirty blocks, which is what makes both the "
            "apportionment error and the noise the Census Bureau adds to block "
            "counts for privacy average out."
        ),
        "resolution_limit": (
            "This is why the ranking is not offered at the finest cell size. A "
            "resolution-10 hexagon is smaller than a typical city block -- a city "
            "has more of them than it has census blocks -- so any population "
            "figure there would be the apportionment assumption handed back as "
            "though it were a measurement."
        ),
        "not_a_demographic_overlay": (
            "Only head counts are used: total residents, total jobs. No race, "
            "income, or other characteristic is read, joined, or displayed."
        ),
    }


def _basis_limitation(basis: list[dict[str, Any]]) -> str:
    """One line naming what this source's timestamps measure.

    The short form, for the product-wide limitations list. `_timestamp_caveat`
    is the long form, where it is the dominant source of error rather than one
    item among several.
    """
    if len(basis) == 1:
        return (
            f"This source's timestamps record {basis[0]['label']}."
            if basis[0]["occurred_basis"] == "occurrence"
            else f"This source's timestamps record {basis[0]['label']}, not "
            "observed occurrence times."
        )
    parts = ", ".join(
        f"{row['label']} ({row['share'] * 100:.0f}%)" for row in basis[:3]
    )
    return f"Timestamps here are a mix: {parts}."


def _timestamp_caveat(
    record: dict[str, Any], basis: list[dict[str, Any]]
) -> str:
    """The hourly view's largest limitation, in this city's own terms.

    Three parts: that it is the largest limitation, what this source's timestamps
    measure, and — only where the timestamps are not an occurrence time — why
    that skews the shape of the day rather than merely offsetting it.
    """
    opening = (
        "This is the most important limitation of the hourly view, and it is "
        "larger here than anywhere else in the product. "
    )

    note = record.get("occurrence_basis_note")
    if note:
        body = note
    elif basis:
        dominant = basis[0]
        share = f"{dominant['share'] * 100:.0f}% of records" if len(basis) > 1 else "Records"
        body = (
            f"{share} here are timestamped with {dominant['label']}, which is not "
            "necessarily the time an offence occurred."
        )
    else:
        body = (
            "The source's timestamps have not been characterised for this city "
            "yet; treat the hour as approximate."
        )

    # The skew argument only holds for a timestamp driven by someone making a
    # call. An actual recorded occurrence time does not cluster toward waking
    # hours in the same way, and claiming it does would be its own error.
    if any(row["occurred_basis"] in ("dispatch", "report") for row in basis):
        body += (
            " For an assault the two are minutes apart; for a burglary discovered "
            "when someone gets home, or a car break-in noticed the next morning, "
            "they are not. Reported times therefore cluster toward when people are "
            "awake and calling, so the hourly view is closer to when incidents are "
            "reported than to when crime happens."
        )
    return opening + body


def _time_of_day(
    record: dict[str, Any], basis: list[dict[str, Any]]
) -> dict[str, Any]:
    """Explain the hourly view, and above all what its timestamps really are.

    The dispatch-versus-occurrence gap is disclosed for the whole product
    already, but it is a footnote at day resolution and the dominant source of
    error at hour resolution. It gets said again, here, in those terms.

    Which terms those are is per city, and this is the passage where getting it
    wrong would matter most. Philadelphia publishes a police dispatch time;
    Seattle publishes a recorded offence start time. Stating the first over the
    second would be a plain factual error about the data on screen, so the
    caveat is assembled from what the records actually carry plus the source's
    own note, rather than written once.
    """
    share = record.get("hour_known_share")
    return {
        "what_it_is": (
            "The same severity-weighted ranking, recomputed inside each one-hour "
            "block of the local day, so a cell is compared against other cells at "
            "that hour rather than against the all-day distribution."
        ),
        "two_ratings": [
            "Safety at this hour: where the cell sits against every other cell in "
            "the city during the same hour block. 1.0 is the safest.",
            "Change from usual: that figure minus the cell's own all-hours "
            "percentile. It measures relative movement only -- the whole city is "
            "quieter at 4am, and a cell holding its rank through the night is not "
            "getting safer, it is keeping pace with everywhere else.",
        ],
        "hour_index_note": (
            "Alongside the two rankings, a selected cell shows how much gets "
            "reported there at that hour against its own average hour, as a "
            "percentage where 100% is an ordinary hour for that cell. That is the "
            "absolute reading a percentile cannot give: 250% is two and a half "
            "times as many reported incidents as the cell usually sees in an hour, "
            "40% is well below it. It counts incidents rather than weighting them "
            "by severity, because it is a statement about how much is reported. It "
            "is withheld where a cell carries too little across the window for the "
            "ratio to mean anything -- three incidents in a year with one of them "
            "at 2pm is 800% of an average hour, which is arithmetic rather than "
            "evidence."
        ),
        "timestamp_caveat": _timestamp_caveat(record, basis),
        "timestamp_basis": basis,
        "coverage": {
            "hour_known_share": round(share, 4) if share is not None else None,
            "note": (
                "Records the source published with no clock time are left out of "
                "the hourly layers entirely rather than being counted at midnight, "
                "which would put a fabricated spike at the hour people look at most."
            ),
        },
        "scope": {
            "resolutions": list(repo.HOURLY_RESOLUTIONS),
            "windows": [repo.window_label(w) for w in repo.HOURLY_WINDOWS],
            "note": (
                "Built for the last 12 months at the two coarser cell sizes. "
                "Splitting a window 24 ways divides the evidence by 24, and at the "
                "finest cell size over 30 days the median cell-hour has no reported "
                "incidents at all -- there is no distribution left to rank. The "
                "longer windows are not built: recomputing a ranking inside every "
                "hour of the day is the most storage-intensive thing this pipeline "
                "produces, and at a year wide the hourly pattern is already stable "
                "enough that a second year mostly restates it."
            ),
        },
        "known_limitations": [
            "An hourly percentile is far noisier than the all-hours one it is "
            "compared against, even after smoothing. Small movements in the "
            "second rating should not be read as real change, which is why it is "
            "banded into wide steps rather than shown as a bare number.",
            "Hour blocks are local clock time, so an hour spans different amounts "
            "of daylight across the year, and the two daylight-saving transitions "
            "are not adjusted for.",
            "The population a cell is divided by does not vary by hour, and this is "
            "where that matters most. Residents and jobs describe where people "
            "sleep and where they work; neither says how many are present at 3am. A "
            "business district is divided by its full daytime workforce at every "
            "hour of the night, so the small hours there are flattered, and a "
            "nightlife street is divided by the few people who live on it. Read the "
            "hourly ranking as a comparison between cells at the same hour, not as "
            "a risk per person present at that hour.",
        ],
    }


@app.get(f"{API}/methodology", tags=["meta"])
def methodology(conn: Conn, city: str = "phl") -> dict[str, Any]:
    record = repo.get_city(conn, city)
    if record is None:
        raise HTTPException(404, f"no serving data for city '{city}'")
    # A silver GROUP BY: cached by refresh stamp, like the /quality mixes.
    basis = cached_json(conn, ("basis", city), lambda: repo.occurrence_basis(conn, city), city)
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
            "city; it says nothing about any particular street or building. "
            "Dividing by the people in a cell makes it a fairer comparison between "
            "places, not a probability that anything will happen to you.",
            "Not a measure of crime. It measures reported and recorded incidents, "
            "which is a different quantity.",
            "A cell with no reported incidents is not therefore safe. It may be a "
            "place where crime goes unreported, which is why those cells are shown "
            "in a neutral colour rather than at the safe end of the scale.",
        ],
        # The per-city entries come from the registry and from what the records
        # themselves carry, because these are facts about one agency's publishing
        # practice rather than about the product. Stating Philadelphia's dispatch
        # times over Seattle's recorded occurrence times would be a plain error.
        "known_limitations": [
            "Reported crime is shaped by how willing people are to report and by where "
            "police are deployed. Historically under-reported offence types and "
            "historically over-enforced ones do not appear here in proportion to how "
            "often they actually occur.",
            record["location_precision_note"]
            or "Coordinates are published at block level by the source agency, so no "
            "reading below roughly a city block is meaningful.",
        ]
        + ([_basis_limitation(basis)] if basis else [])
        + [
            record["freshness_note"]
            or "Recent records are preliminary and are revised and reclassified by "
            "the department after first publication.",
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
                "rather than where crime happened. Incident counts are shown at "
                "this size; the safety ranking is not, because a cell this small "
                "has no population figure behind it that is not guesswork. Counts "
                "here cover the last 12 and 24 months, and all offense types "
                "together: a cell this small is empty in most single categories "
                "over most shorter windows, so those breakdowns would rank a set "
                "of cells that all hold zero against each other."
            ),
            "safety_resolutions": list(repo.SAFETY_RESOLUTIONS),
            "fine_resolution_windows": list(repo.ACTIVITY_WINDOWS[10]),
            "fine_resolution_categories": list(repo.ACTIVITY_CATEGORIES[10]),
            "relative_measure": (
                "Each cell's percentile is the fraction of cells in the same city with "
                "strictly lower reported-incident density for the same window and "
                "category. Tier 0 means nothing was reported in the cell, which is a "
                "different statement from being in the quietest fifth."
            ),
        },
        "safety_measure": _safety_measure(conn, record),
        "time_of_day": _time_of_day(record, basis),
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
