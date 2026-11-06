"""FastAPI serving layer (design doc S9.4).

Exposes exactly the operations the map needs -- every city's cell layer over
the last year, one cell's detail, city metadata, and the first load's progress
-- over the precomputed gold tables. There is no other data to ask for. Nothing here aggregates; if an
endpoint would need to scan silver, that is a signal the ETL is missing a
rollup, not a reason to compute it per request.

Run:  python -m uvicorn safety.api.main:app --reload
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gzip
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from safety import PIPELINE_VERSION, shards
from safety.api import directions
from safety.api import repository as repo
from safety.api.ratelimit import RateLimitMiddleware
from safety.api.security import SecurityHeadersMiddleware
from safety.etl import progress
from safety.config import REPO_ROOT, WEB_DIR, settings
from safety.h3grid import cell_resolution, cells_for_point, is_valid_cell
from safety.routing.errors import RoutingError
from safety.routing.service import Routing, new_http_client

log = logging.getLogger(__name__)

pool: ConnectionPool | None = None
# With cities split across databases (settings.db_shards, safety/shards.py):
# one pool per database, by host, and which host holds each city. Empty
# otherwise, and then every request uses `pool`. With shards, `pool` is the
# first database's -- where the provider budgets are counted.
pools: dict[str, ConnectionPool] = {}
_city_hosts: dict[str, str] = {}


def _open_pool(dsn: str) -> ConnectionPool:
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=settings.api_pool_max_size,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    shard_list = shards.configured()
    if shard_list:
        for shard in shard_list:
            pools[shard.host] = _open_pool(settings.dsn_for(shard.host))
            _city_hosts.update({city: shard.host for city in shard.cities})
        pool = pools[shard_list[0].host]
    else:
        pool = _open_pool(settings.dsn)
    for each in all_pools():
        each.wait(timeout=30)
    log.info("connection pools ready: %d database(s)", len(all_pools()))
    # One outbound HTTP client for every routing provider, so connections to
    # Photon and Transitous are pooled and kept alive across requests rather
    # than re-handshaking TLS per geocode keystroke (safety/routing/).
    http = new_http_client()
    app.state.http = http
    # The pool goes along so the provider budgets can be counted in the
    # database when settings.routing_budget_backend is `db` (many replicas).
    app.state.routing = Routing.build(http, pool=pool)
    warm = None
    if settings.cache_warm_on_start:
        warm = asyncio.create_task(_warm_cache())
    try:
        yield
    finally:
        if warm is not None and not warm.done():
            warm.cancel()
        await http.aclose()
        for each in all_pools():
            each.close()
        pools.clear()
        _city_hosts.clear()


def _city_now(timezone: str | None) -> datetime:
    """Now on a city's own clock (UTC for an unknown zone)."""
    try:
        return datetime.now(ZoneInfo(timezone or "UTC"))
    except Exception:
        return datetime.now(ZoneInfo("UTC"))


async def _warm_cache() -> None:
    """Fill the response cache with what a first visitor asks for.

    Over loopback HTTP rather than by calling the endpoint functions, so the
    warm requests take exactly the path a real one does -- parameter defaults,
    window resolution, cache keys -- and cannot drift from it. Best effort: a
    failure is logged and the process serves normally, just cold.
    """
    import httpx

    port = os.environ.get("PORT", "8000")
    base = f"http://127.0.0.1:{port}{API}"
    # Give uvicorn a moment to bind after lifespan startup returns.
    await asyncio.sleep(2)
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.get(f"{base}/cities")
            response.raise_for_status()
            records = response.json().get("cities", [])
            cities = [c["source_id"] for c in records]
            for record in records:
                # What the page asks for: the city at the hour and weekday it is
                # now there (web/app.js loadCityLayer), at the zoomed-out size.
                now = _city_now(record.get("timezone"))
                await client.get(
                    f"{base}/cells",
                    params={"city": record["source_id"], "res": 8, "hour": now.hour,
                            "day": now.weekday()},
                )
                # Under the /cells rate-limit zone (2 r/s) for 127.0.0.1.
                await asyncio.sleep(0.6)
        log.info("cache warmed for %d cities", len(cities))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("cache warm-up failed: %s: %s", type(exc).__name__, exc)


app = FastAPI(
    title="Crime Prevention & Personal Safety API",
    version=PIPELINE_VERSION,
    description=(
        "Serving layer over precomputed H3 cell rollups of the last year, one city "
        "at a time. Values are reported-incident density relative to other cells in the same "
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
# untouched -- see safety/api/ratelimit.py for the zones and why they differ.
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


# Provider failures (safety/routing/errors.py) answer with the status and the
# visitor-facing message the exception carries -- never the provider's body.
app.add_exception_handler(RoutingError, directions.routing_error_handler)


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


def all_pools() -> list[ConnectionPool]:
    """Every database's pool: one, unless the cities are split (pools)."""
    if pools:
        return list(pools.values())
    return [pool] if pool is not None else []


def pool_for(city: str | None) -> ConnectionPool:
    """The pool of the database holding `city`; the only pool without shards.

    An unknown city goes to the first database, which answers it as one
    database always has: no such city."""
    chosen = pools.get(_city_hosts.get(city or "", ""), pool) if pools else pool
    assert chosen is not None, "connection pool not initialised"
    return chosen


# The endpoints' own default when a request names no city.
DEFAULT_CITY = "phl"

# Which city each looked-up cell belongs to, so a cell's database is found
# once (cell detail is a click, not the hot path). Bounded; cleared when full.
_cell_city: dict[str, str] = {}
_CELL_CITY_MAX = 50_000


def _city_of_cell(cell: str) -> str | None:
    """Ask each database whose cell this is (gold.cell_geometry is per city)."""
    if not is_valid_cell(cell):
        return None
    known = _cell_city.get(cell)
    if known is not None:
        return known
    for each in all_pools():
        with each.connection() as conn:
            city = repo.cell_source(conn, cell)
        if city is not None:
            if len(_cell_city) >= _CELL_CITY_MAX:
                _cell_city.clear()
            _cell_city[cell] = city
            return city
    return None


def request_city(request: Request) -> str | None:
    """The city a request is about, as its endpoint will read it."""
    params, query = request.path_params, request.query_params
    city = params.get("source_id") or query.get("city")
    if city:
        return city
    cell = params.get("h3_index") or query.get("h3")
    if cell is None and "lat" in query and "lng" in query:
        try:
            cell = cells_for_point(float(query["lat"]), float(query["lng"]))[8]
        except (ValueError, KeyError):
            cell = None
    if cell:
        return _city_of_cell(cell)
    return DEFAULT_CITY


def _spans_databases(request: Request) -> bool:
    """An endpoint that, with shards, reads every database itself: it gets no
    connection up front, so one database being down cannot fail it."""
    path = request.url.path
    if path in (f"{API}/health", f"{API}/cities"):
        return True
    return path == f"{API}/version" and not request.query_params.get("city")


def get_conn(request: Request):
    # Without shards there is one pool and nothing to work out.
    if not pools:
        assert pool is not None, "connection pool not initialised"
        with pool.connection() as conn:
            yield conn
        return
    if _spans_databases(request):
        yield None
        return
    with pool_for(request_city(request)).connection() as conn:
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


# Stamp memo (settings.cache_stamp_ttl_seconds). With a TTL of 0 every cached
# lookup asks the database for the city's stamp, which keeps a single process
# exactly current. Many replicas serving thousands of visitors turn that one
# query per hit into the bulk of the database's load, so a deployment can trust
# a stamp for a few seconds: a new snapshot reaches the map that much later, and
# a hit costs no query at all. Keyed by source_id (None = the aggregate).
_stamp_memo: dict[str | None, tuple[float, str | None]] = {}
_stamp_lock = threading.Lock()


def _current_stamp(conn, source_id: str | None) -> str | None:
    ttl = settings.cache_stamp_ttl_seconds
    if ttl <= 0:
        return _refresh_stamp(conn, source_id)
    now = time.monotonic()
    with _stamp_lock:
        memo = _stamp_memo.get(source_id)
    if memo is not None and memo[0] > now:
        return memo[1]
    stamp = _refresh_stamp(conn, source_id)
    with _stamp_lock:
        _stamp_memo[source_id] = (now + ttl, stamp)
    return stamp


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
    stamp = _current_stamp(conn, source_id)
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


def _merge_versions(versions: list[dict[str, Any]]) -> dict[str, Any]:
    """serving_version() across databases: latest stamps, summed counts."""

    def latest(key: str):
        values = [v.get(key) for v in versions if v.get(key) is not None]
        return max(values) if values else None

    def total(key: str):
        values = [v.get(key) for v in versions if v.get(key) is not None]
        return sum(values) if values else None

    return {
        "last_refreshed_at": latest("last_refreshed_at"),
        "data_as_of": latest("data_as_of"),
        "incident_count": total("incident_count"),
        "cities": total("cities") or 0,
    }


def _version_everywhere() -> tuple[dict[str, Any], dict[str, str]]:
    """The aggregate serving_version over every database, and each one's state.

    Raises only if no database answers: a site with one database down still
    serves the other cities, so its pods should stay in the load balancer."""
    versions, states = [], {}
    last_error: Exception | None = None
    for host, each in pools.items():
        try:
            with each.connection() as conn:
                versions.append(repo.serving_version(conn))
            states[host] = "ok"
        except Exception as exc:  # noqa: BLE001 - reported per database
            log.warning("database %s: %s: %s", host, type(exc).__name__, exc)
            states[host] = "unavailable"
            last_error = exc
    if not versions and last_error is not None:
        raise last_error
    return _merge_versions(versions), states


@app.get(f"{API}/health", tags=["meta"])
def health(conn: Conn) -> dict[str, Any]:
    databases: dict[str, str] | None = None
    if pools:
        version, databases = _version_everywhere()
    else:
        version = repo.serving_version(conn)
    degraded = databases is not None and any(v != "ok" for v in databases.values())
    return {
        "status": "degraded" if degraded else "ok",
        **({"databases": databases} if databases is not None else {}),
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
    if pools and city is None:
        return _version_everywhere()[0]
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
        "series_caveats": cached_json(
            conn,
            ("caveats", record["source_id"]),
            lambda: repo.series_caveats(conn, record["source_id"]),
            record["source_id"],
        ),
    }


def _from_every_database(read) -> list[dict[str, Any]]:
    """`read(conn)`'s rows from each database, sorted by city_name.

    A database that cannot answer drops out -- its cities go missing from the
    list, the rest are served -- unless none answers, which raises (503)."""
    rows: list[dict[str, Any]] = []
    last_error: Exception | None = None
    answered = False
    for host, each in pools.items():
        try:
            with each.connection() as conn:
                rows.extend(read(conn))
            answered = True
        except Exception as exc:  # noqa: BLE001 - one database down is not an outage
            log.warning("cities from %s skipped: %s: %s", host, type(exc).__name__, exc)
            last_error = exc
    if not answered and last_error is not None:
        raise last_error
    rows.sort(key=lambda r: r["city_name"])
    return rows


def list_cities_all() -> list[dict[str, Any]]:
    """repo.list_cities over every database (the one, without shards)."""
    if not pools:
        with pool_for(None).connection() as conn:
            return repo.list_cities(conn)
    return _from_every_database(repo.list_cities)


@app.get(f"{API}/cities", tags=["cities"])
def cities(conn: Conn) -> dict[str, Any]:
    if not pools:
        return {"cities": [_with_windows(conn, r) for r in repo.list_cities(conn)]}
    # Each city's windows and caveats come from its own database.
    return {
        "cities": _from_every_database(
            lambda shard_conn: [_with_windows(shard_conn, r) for r in repo.list_cities(shard_conn)]
        )
    }


@app.get(f"{API}/cities/{{source_id}}", tags=["cities"])
def city(source_id: str, conn: Conn) -> dict[str, Any]:
    record = repo.get_city(conn, source_id)
    if record is None:
        raise HTTPException(404, f"no serving data for city '{source_id}'")
    return _with_windows(conn, record)


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


def _resolve_window(conn, city: str | None) -> dict[str, Any]:
    """The one window the map serves (repo.DEFAULT_WINDOW), as this city has it.

    A city with no window built yet -- unknown, or still loading -- gets a
    stand-in, so it answers with an empty layer rather than a 400.
    """
    windows = city_windows(conn, city) if city else []
    match = repo.match_window(repo.DEFAULT_WINDOW, windows)
    if match is None:
        return {"id": repo.DEFAULT_WINDOW, "hourly": True, "safety": True}
    return match


def _validate_res(res: int) -> None:
    if res not in repo.VALID_RESOLUTIONS:
        raise HTTPException(400, f"res must be one of {list(repo.VALID_RESOLUTIONS)}")


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
            400, f"the time-of-day layer is built for res {list(repo.HOURLY_RESOLUTIONS)} only"
        )


def _validate_day(day: int | None, res: int, window: dict[str, Any]) -> None:
    """Reject a weekday where the factor is not built: the same scope as the
    hour layer (gold.refresh_hourly_layer builds both)."""
    if day is None:
        return
    if day not in repo.VALID_DAYS:
        raise HTTPException(400, "day must be an integer from 0 (Monday) to 6 (Sunday)")
    if res not in repo.HOURLY_RESOLUTIONS:
        raise HTTPException(
            400,
            f"the day-of-week factor is built for res {list(repo.HOURLY_RESOLUTIONS)} "
            "only, as the time-of-day layer is",
        )


@app.get(f"{API}/cells", tags=["cells"])
def cells(
    request: Request,
    conn: Conn,
    city: str = "phl",
    res: int = 8,
    hour: int | None = Query(
        None,
        ge=0,
        le=23,
        description=(
            "Local hour block 0-23; block h covers [h:00, h+1:00). Adds the "
            "time-of-day ratings to every feature."
        ),
    ),
    day: int | None = Query(
        None,
        ge=0,
        le=6,
        description=(
            "Local day of the week, 0 = Monday ... 6 = Sunday. Scales each cell's "
            "rate -- all hours, or the `hour` block -- by its weekday factor and "
            "re-ranks the city, into the same time-of-day fields."
        ),
    ),
) -> Response:
    """The city's H3 hexagon layer over the last year, as GeoJSON.

    The only data the map can ask for: one city, one of the two cell sizes, and
    optionally an hour and a weekday. There is no other window, date range or
    category. A query parameter this endpoint does not declare is ignored, so an
    old link still gets the last year.
    """
    _validate_res(res)
    served = _resolve_window(conn, city)
    _validate_hour(hour, res, served)
    _validate_day(day, res, served)
    window = served["id"]

    key = ("cells", city, res, window, hour, day)
    payload = cached(
        conn,
        key,
        # Encoded *and compressed* inside the cache, not after it: a hit is a
        # memory copy, and the cache's byte budget holds about ten times as
        # many layers.
        lambda: gzip.compress(
            json.dumps(
                repo.cells_geojson(
                    conn,
                    source_id=city,
                    h3_res=res,
                    time_window=window,
                    hour=hour,
                    day=day,
                ),
                default=str,
            ).encode("utf-8"),
            compresslevel=6,
        ),
        source_id=city,
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


@app.get(f"{API}/cells/{{h3_index}}", tags=["cells"])
def cell(
    conn: Conn,
    h3_index: str,
    hour: int | None = Query(None, ge=0, le=23),
) -> dict[str, Any]:
    """One cell over the last year: its ratings and its hour and weekday profiles."""
    if not is_valid_cell(h3_index):
        raise HTTPException(400, f"'{h3_index}' is not a valid H3 index")
    _validate_res(cell_resolution(h3_index))
    source = repo.cell_source(conn, h3_index)
    served = _resolve_window(conn, source)
    _validate_hour(hour, cell_resolution(h3_index), served)

    detail = repo.cell_detail(conn, h3_index=h3_index, time_window=served["id"], hour=hour)
    if detail is None:
        raise HTTPException(404, f"cell '{h3_index}' is not in the covered area")
    return detail


# ---------------------------------------------------------------------------
# Load progress (safety/etl/progress.py): the bar on the live site
# ---------------------------------------------------------------------------


@app.get(f"{API}/progress", tags=["meta"])
def load_progress() -> Response:
    """How far the cities' first load has got, across every database.

    Never cached: the page polls it every few seconds while a load runs, and
    each poll is one small query per database. A database that cannot answer
    drops its cities out of the figure rather than failing the request.
    """
    rows: list[dict[str, Any]] = []
    for each in all_pools():
        try:
            with each.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(progress.STATE_SQL)
                    rows.extend(cur.fetchall())
        except Exception as exc:  # noqa: BLE001 - a database down is not an outage
            log.warning("progress from a database skipped: %s: %s", type(exc).__name__, exc)
    body = progress.summarize(rows)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


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
            "The same two severity-weighted rankings, violent and non-violent, "
            "adjusted for the selected hour of the local day. Each cell's all-hours "
            "rate is multiplied by how much of its day's offence on that track falls "
            "in that hour, and every cell is then ranked against every other at the "
            "same hour."
        ),
        "how_it_works": [
            "Hour factor: the cell's share of its own day's severity-weighted "
            "offence at this hour, times 24. 1.0 is an ordinary hour for that cell; "
            "2.5 is two and a half times its usual load, 0.4 well below it. A "
            "cell's 24 factors average exactly 1, so the adjustment moves a cell's "
            "rate around the clock without making its day as a whole better or worse.",
            "Safety at this hour: the all-hours rate times the hour factor, ranked "
            "against every other cell in the city at the same hour. 1.0 is the "
            "safest. Because the rate being scaled is the all-hours one, a cell's "
            "standing at an hour stays anchored to how it ranks across the whole day.",
        ],
        "hour_factor_note": (
            "A cell with only a few incidents on a track has no reliable hour "
            "pattern of its own -- three incidents in a year, one of them at 2pm, "
            "would make 2pm eight times an ordinary hour. So each cell's pattern is "
            "blended with the whole city's pattern for that track, weighted by how "
            "much the cell has: at twelve incidents the two count equally, a busy "
            "cell follows its own pattern, and a cell with none follows the city's."
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
            "same year."
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
            "safety_resolutions": list(repo.SAFETY_RESOLUTIONS),
            "window": (
                "The last year only: incidents more than 365 days old are deleted, "
                "and every ranking covers the year ending on the newest reported date."
            ),
            "relative_measure": (
                "Each cell's percentile is the fraction of cells in the same city with "
                "strictly lower reported-incident density over the last year. "
                "Tier 0 means nothing was reported in the cell, which is a "
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


# Directions and address search (safety/api/directions.py). Included here,
# before the static mount below, like every other API route.
app.include_router(directions.router)


class RevalidatedStaticFiles(StaticFiles):
    """The front end, sent with `Cache-Control: no-cache`.

    Without a Cache-Control header browsers cache these files heuristically
    and reuse them without asking, so after a deploy a visitor can run the old
    app.js against the new index.html (or keep the old page outright) for
    hours. no-cache still lets them keep a copy; it only makes them check the
    ETag first, which costs a 304 when nothing changed.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# The temporary front end. Mounted last so it never shadows an API route.
app.mount("/", RevalidatedStaticFiles(directory=str(WEB_DIR), html=True), name="web")
