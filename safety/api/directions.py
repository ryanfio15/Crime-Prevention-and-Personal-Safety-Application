"""Directions and address search endpoints (plan S2; design doc S13).

    GET  /api/v1/directions/status   which modes and geocoding are up, transit budget left
    POST /api/v1/directions          {from, to, mode, depart_at?, window?}: routes, each
                                     with the reported activity along it
    POST /api/v1/geocode             {q, city, limit}: places in a served city
    POST /api/v1/geocode/reverse     {lat, lng}: a label for a point

POST with a JSON body, not GET with a query string, because uvicorn and nginx
log every request line: a GET would write the address someone typed, or where
they are standing, into two access logs. For the same reason nothing here logs
a coordinate or a query -- only the city, a count, a status.

These handlers are `async`, unlike the rest of the API, and deliberately do
not take the `Conn` dependency (main.py). That dependency holds one of the
pool's eight connections for the whole request, and a request here spends most
of its time waiting on a provider several seconds away; eight slow geocodes
would starve the map. A connection is borrowed only for the short queries
(_db()), on the threadpool, and handed straight back.

The provider clients, caches, budgets and breakers live on app.state.routing
(safety/routing/service.py), built in main.py's lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from safety.api import repository as repo
from safety.config import settings
from safety.api.ratelimit import client_key
from safety.h3grid import PRIMARY_RES, cell_centroid, cell_for, grid_disk
from safety.routing import detours, scoring
from safety.routing.cities import city_at
from safety.routing.errors import NoRoute, ProviderUnavailable, RoutingError
from safety.routing.schema import Place, Point, Route, haversine_m
from safety.routing.service import Routing

log = logging.getLogger(__name__)

T = TypeVar("T")

API = "/api/v1"

# Shown under results, as Photon's and OSM's terms ask. The page links each
# name; transitous.org/sources is required by Transitous (R2).
GEOCODE_ATTRIBUTION = "Search by Photon (komoot) · © OpenStreetMap contributors (ODbL)"
ATTRIBUTION = {
    "map_data": "© OpenStreetMap contributors (ODbL)",
    "routing": "Valhalla",
    "geocoding": "Photon by komoot",
    "transit": "Transitous",
    "transit_sources_url": "https://transitous.org/sources",
}

# Responses that describe where one person is going are never kept by a
# browser or an intermediary.
NO_STORE = {"Cache-Control": "no-store"}


def _require_enabled() -> None:
    """The kill switch (settings.directions_enabled). Read per request, so a
    .env change and a restart is all it takes."""
    if not settings.directions_enabled:
        raise HTTPException(
            503, "Directions and address search are switched off on this server.", headers=NO_STORE
        )


router = APIRouter(prefix=API, tags=["directions"], dependencies=[Depends(_require_enabled)])


async def routing_error_handler(request: Request, exc: RoutingError) -> JSONResponse:
    """Registered in main.py. The body is the exception's own message, written
    in errors.py for the visitor; a provider's body is never part of it."""
    level = logging.WARNING if exc.status >= 500 else logging.INFO
    log.log(
        level,
        "%d %s for %s (%s: %s)",
        exc.status,
        exc.code,
        request.url.path,
        exc.provider,
        exc.reason,
    )
    headers = dict(NO_STORE)
    if exc.retry_after:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(exc.body(), status_code=exc.status, headers=headers)


def routing_of(request: Request) -> Routing:
    routing = getattr(request.app.state, "routing", None)
    if routing is None:
        # Only before the lifespan has run, which a real server never serves.
        raise ProviderUnavailable(reason="routing not initialised")
    return routing


# ---------------------------------------------------------------------------
# Database, briefly
# ---------------------------------------------------------------------------


def _with_conn(fn: Callable[..., T], *args: Any) -> T:
    # Imported here, not at the top: main imports this module to mount it.
    from safety.api import main

    assert main.pool is not None, "connection pool not initialised"
    with main.pool.connection() as conn:
        return fn(conn, *args)


async def db(fn: Callable[..., T], *args: Any) -> T:
    """`fn(conn, *args)` on the threadpool with a connection held only for its
    duration. PoolTimeout and QueryCanceled propagate to main.py's 503."""
    return await run_in_threadpool(_with_conn, fn, *args)


# Only what routing needs from a city row; list_cities() also carries dates,
# which would not survive being cached as plain values anyway.
_CITY_FIELDS = (
    "source_id",
    "city_name",
    "bbox_west",
    "bbox_south",
    "bbox_east",
    "bbox_north",
    "center_lat",
    "center_lng",
    "timezone",
)


async def served_cities(routing: Routing) -> dict[str, dict[str, Any]]:
    """Served cities by source_id, cached for a few minutes (service.py)."""

    async def load() -> dict[str, dict[str, Any]]:
        rows = await db(repo.list_cities)
        return {row["source_id"]: {k: row.get(k) for k in _CITY_FIELDS} for row in rows}

    return await routing.cities.get_or_create("cities", load)


async def city_for_point(routing: Routing, lat: float, lng: float) -> dict[str, Any] | None:
    """The served city a point is in, by web/app.js's cityAt rule."""
    return city_at((await served_cities(routing)).values(), lat, lng)  # type: ignore[return-value]


async def served_places(source_id: str, places: list[Place]) -> list[Place]:
    """Only the places whose resolution-8 cell the city's map actually draws.

    The bbox (sent to Photon, and re-checked in providers.normalise_photon) is
    a rectangle: a Seattle search can return Mercer Island, inside the box but
    outside the city's data (R3). One query for the whole batch.
    """
    if not places:
        return []
    cells = [cell_for(p.point.lat, p.point.lng, PRIMARY_RES) for p in places]
    served = await db(repo.cells_exist, source_id, sorted(set(cells)))
    return [p for p, cell in zip(places, cells) if cell in served]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


class GeocodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The raw bound is generous; the real one (3..120) is applied after
    # control characters and runs of whitespace are removed.
    q: str = Field(max_length=500)
    city: str = Field(min_length=1, max_length=16, pattern=r"^[a-z0-9_]+$")
    limit: int = Field(5, ge=1, le=6)

    @field_validator("q")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        value = " ".join(_CONTROL.sub(" ", value).split())
        if not 3 <= len(value) <= 120:
            raise ValueError("q must be 3 to 120 characters")
        return value


class ReverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/directions/status")
async def directions_status(request: Request) -> JSONResponse:
    """Which modes the page should offer right now.

    Drive, walk and bike share the Valhalla container: a /status probe
    (cached 60 s) plus its circuit breaker. Transit and geocoding are never
    probed -- a probe would spend the very budget being reported -- so they
    report their breaker and what is left of today's budget.
    """
    routing = routing_of(request)
    street_up = routing.valhalla.breaker.available and await routing.valhalla.probe()
    transit_left = routing.transitous.budget.remaining() if routing.transitous.budget else 0
    geocode_left = routing.photon.budget.remaining() if routing.photon.budget else 0

    def mode(available: bool, reason: str | None) -> dict[str, Any]:
        return {"available": available, "reason": None if available else reason}

    transit_reason = "budget" if transit_left <= 0 else "unavailable"
    geocode_reason = "budget" if geocode_left <= 0 else "unavailable"
    transit_up = routing.transitous.breaker.available and transit_left > 0
    geocode_up = routing.photon.breaker.available and geocode_left > 0
    return JSONResponse(
        {
            "modes": {
                "drive": mode(street_up, "unavailable"),
                "walk": mode(street_up, "unavailable"),
                "bike": mode(street_up, "unavailable"),
                "transit": mode(transit_up, transit_reason),
            },
            "geocode": mode(geocode_up, geocode_reason),
            "transit_budget_left": transit_left,
            "route_max_km": settings.route_max_km,
            "attribution": ATTRIBUTION,
        },
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/geocode")
async def geocode(body: GeocodeRequest, request: Request) -> JSONResponse:
    """Places matching `q` in one served city, best match first."""
    routing = routing_of(request)
    city = (await served_cities(routing)).get(body.city)
    if city is None or city.get("bbox_west") is None:
        raise HTTPException(422, "city must be one of the served cities", headers=NO_STORE)
    places = await routing.photon.search(
        body.q,
        (city["bbox_west"], city["bbox_south"], city["bbox_east"], city["bbox_north"]),
        (city["center_lat"], city["center_lng"]),
        body.limit,
    )
    places = (await served_places(body.city, places))[: body.limit]
    log.info("geocode %s: %d results", body.city, len(places))
    return JSONResponse(
        {
            "city": body.city,
            "results": [p.to_dict() for p in places],
            "attribution": GEOCODE_ATTRIBUTION,
        },
        headers=NO_STORE,
    )


@router.post("/geocode/reverse")
async def geocode_reverse(body: ReverseRequest, request: Request) -> JSONResponse:
    """A label for a picked or located point, and the served city it is in.

    A point outside every served city is answered without asking Photon:
    nothing there can be routed, and the lookup would spend budget and send
    someone's location upstream for nothing. With no nearby address the label
    is the coordinates themselves (R3).
    """
    routing = routing_of(request)
    point = Point(body.lat, body.lng)
    city = await city_for_point(routing, body.lat, body.lng)
    found = await routing.photon.reverse(body.lat, body.lng) if city else None

    coords = f"{body.lat:.5f}, {body.lng:.5f}"
    if found is not None:
        # The visitor's own point, with the nearby address as its name: a
        # route should start where they clicked, not at the house it is near.
        result = {**found.to_dict(), **point.to_dict()}
    else:
        result = {
            "label": coords,
            "title": coords,
            "subtitle": "",
            **point.to_dict(),
            "kind": "point",
        }
    log.info(
        "reverse %s: %s", city["source_id"] if city else "outside", "found" if found else "none"
    )
    return JSONResponse(
        {
            "city": city["source_id"] if city else None,
            "result": result,
            "attribution": GEOCODE_ATTRIBUTION,
        },
        headers=NO_STORE,
    )


# ---------------------------------------------------------------------------
# Directions
# ---------------------------------------------------------------------------

MODES = ("drive", "walk", "bike", "transit")

# How far ahead, or behind, "Depart at" may be. MOTIS plans on the timetable it
# has, which runs weeks out; a trip a year away would be planned against a
# timetable that does not exist yet.
DEPART_PAST = timedelta(hours=1)
DEPART_AHEAD = timedelta(days=30)

# Alternates asked of Valhalla on the first call, before any detour.
ALTERNATES = 2

# Travels with every result: what the figure is and is not (design doc S13).
CAVEATS = (
    "Based on incidents reported to police and published by the city -- not all "
    "crime, and not all of it reported.",
    "A comparison with this city's average for the route as a whole, not a rating "
    "of either address, and not a prediction of what will happen.",
)
TRANSIT_CAVEAT = (
    "Only the walks to, from and between stops, and the stops themselves, are "
    "compared; time on board is not."
)


class LatLng(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class DirectionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    origin: LatLng = Field(alias="from")
    destination: LatLng = Field(alias="to")
    mode: Literal["drive", "walk", "bike", "transit"]
    # Wall-clock time in the city, as <input type="datetime-local"> gives it.
    # None is "leave now".
    depart_at: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
    # The map's window; an unserved one falls back to the city default.
    window: str | None = Field(None, max_length=16, pattern=r"^[a-z0-9_]+$")


def _departure(city: dict[str, Any], depart_at: str | None) -> datetime:
    """The departure as an aware datetime in the city's zone."""
    try:
        zone = ZoneInfo(city.get("timezone") or "UTC")
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    now = datetime.now(zone)
    if depart_at is None:
        return now
    try:
        when = datetime.fromisoformat(depart_at).replace(tzinfo=zone)
    except ValueError:
        raise HTTPException(
            422, "depart_at is not a valid date and time", headers=NO_STORE
        ) from None
    if not now - DEPART_PAST <= when <= now + DEPART_AHEAD:
        raise HTTPException(
            422, "Departure must be between an hour ago and 30 days from now.", headers=NO_STORE
        )
    return when


def _window_and_baseline(conn: Any, source_id: str, requested: str | None) -> dict[str, Any]:
    """The served window a request means, and its city averages (cached on
    the city's refresh stamp, like every other per-city figure)."""
    from safety.api import main

    windows = main.city_windows(conn, source_id)
    window = (
        repo.match_window(requested or repo.DEFAULT_WINDOW, windows)
        or repo.match_window(repo.DEFAULT_WINDOW, windows)
        or (windows[-1] if windows else None)
    )
    if window is None or not window.get("safety"):
        return {"window": window, "rows": []}
    hourly = bool(window.get("hourly"))
    rows = main.cached_json(
        conn,
        ("route_baseline", source_id, window["id"], hourly),
        lambda: repo.route_baseline(conn, source_id, scoring.SCORE_RES, window["id"], hourly),
        source_id,
    )
    return {"window": window, "rows": rows, "hourly": hourly}


def _baselines(rows: list[dict[str, Any]]) -> dict[str, scoring.Baseline]:
    out = {track: scoring.Baseline(None) for track in scoring.TRACKS}
    for r in rows:
        average = scoring.average_from_sums(r["weighted"], r["people"], r["total"], r["painted"])
        base = out.setdefault(r["track"], scoring.Baseline(None))
        if r["hour_block"] is None:
            base.overall = average
        elif average is not None:
            base.hourly[r["hour_block"]] = average
    return out


class _Scorer:
    """Runs, hours and ratios for a set of routes, one city, one window."""

    def __init__(
        self,
        source_id: str,
        window: dict[str, Any] | None,
        hourly: bool,
        baselines: dict[str, scoring.Baseline],
        departure: datetime,
    ) -> None:
        self.source_id = source_id
        self.window = window
        self.hourly = hourly
        self.baselines = baselines
        self.departure = departure
        self.cells: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in scoring.TRACKS}
        # What has been fetched: cells with their all-hours rows, and
        # (cell, hour) pairs with their hourly rows. A cell fetched for one
        # hour still needs fetching for the next.
        self.known: set[str] = set()
        self.known_hours: set[tuple[str, int]] = set()

    def _start(self, route: Route) -> datetime:
        # A transit itinerary leaves when it leaves, not when it was asked for.
        if route.start_time:
            try:
                start = datetime.fromisoformat(route.start_time.replace("Z", "+00:00"))
                return start.astimezone(self.departure.tzinfo)
            except ValueError:
                pass
        return self.departure

    def hours(self, route: Route, runs: list[scoring.Run]) -> list[int | None]:
        if not self.hourly:
            return [None] * len(runs)
        start = self._start(route)
        return [(start + timedelta(seconds=r.t_offset_s)).hour for r in runs]

    async def load(self, cells: set[str], hours: set[int]) -> None:
        """Fetch the rows for any cells, or cell-hours, not fetched yet. One
        borrowed connection, two index probes."""
        if self.window is None:
            return
        hours = hours if self.hourly else set()
        missing = sorted(
            c
            for c in cells
            if c not in self.known or any((c, h) not in self.known_hours for h in hours)
        )
        if not missing:
            return
        rows = await db(
            repo.route_cells,
            self.source_id,
            scoring.SCORE_RES,
            self.window["id"],
            missing,
            sorted(hours),
        )
        # Merged into what is there, so an earlier fetch's hours are kept.
        for track, by_cell in rows.items():
            have = self.cells.setdefault(track, {})
            for cell, row in by_cell.items():
                have.setdefault(cell, {}).update(row)
        self.known.update(missing)
        self.known_hours.update((c, h) for c in missing for h in hours)

    def ratios(
        self, track: str, runs: list[scoring.Run], hours: list[int | None]
    ) -> dict[int, float | None]:
        base = self.baselines.get(track) or scoring.Baseline(None)
        by_cell = self.cells.get(track, {})
        out: dict[int, float | None] = {}
        for i, (run, hour) in enumerate(zip(runs, hours)):
            row = by_cell.get(run.cell)
            if row is not None and hour is not None:
                row = {**row, "hsm": row.get(f"hsm.{hour}"), "htier": row.get(f"htier.{hour}")}
            out[i] = scoring.cell_ratio(row, base, hour)
        return out


async def _street_routes(
    routing: Routing, scorer: _Scorer, a: Point, b: Point, mode: str
) -> list[Route]:
    """Fastest first, distinct, with detours added on foot and by bike when
    Valhalla's own alternates are not distinct enough (detours.py)."""
    routes = await routing.valhalla.route([a, b], mode, alternates=ALTERNATES)
    if mode == "drive":
        return routes

    runs = [scoring.route_runs(r) for r in routes]
    keep = scoring.distinct_routes(routes, runs)
    if len(keep) >= 2:
        return [routes[i] for i in keep]

    fastest = routes[0]
    vias = detours.side_offsets(a, b)
    # The quietest rated cell near the fastest route's worst stretch, on the
    # violent track: the one the map opens on.
    fast_runs = runs[0]
    hours = scorer.hours(fastest, fast_runs)
    await scorer.load(scoring.route_cells(fast_runs), {h for h in hours if h is not None})
    ratios = scorer.ratios("violent", fast_runs, hours)
    worst = scoring.worst_run(fast_runs, ratios)
    if worst is not None:
        near = grid_disk(fast_runs[worst].cell, detours.NEAR_RINGS)
        hour = hours[worst]
        await scorer.load(set(near), {hour} if hour is not None else set())
        candidates = []
        for cell in near:
            probe = scoring.Run(cell, 1.0, fast_runs[worst].t_offset_s)
            r = scorer.ratios("violent", [probe], [hour])[0]
            if r is not None and r < (ratios[worst] or 0):
                candidates.append((r, cell))
        if candidates:
            lat, lng = cell_centroid(min(candidates)[1])
            vias.append(Point(lat, lng))

    attempts = await asyncio.gather(
        *(routing.valhalla.route([a, via, b], mode) for via in vias[: detours.MAX_DETOURS]),
        return_exceptions=True,
    )
    for result in attempts:
        if isinstance(result, RoutingError):
            continue
        if isinstance(result, BaseException):
            raise result
        detour = result[0]
        if detours.within_detour(fastest.distance_m, detour.distance_m):
            routes.append(detour)
            runs.append(scoring.route_runs(detour))

    order = sorted(range(len(routes)), key=lambda i: routes[i].duration_s)
    routes = [routes[i] for i in order]
    runs = [runs[i] for i in order]
    return [routes[i] for i in scoring.distinct_routes(routes, runs)]


@router.post("/directions")
async def directions(body: DirectionsRequest, request: Request) -> JSONResponse:
    """Routes between two points in one served city, each annotated with the
    reported activity along it, and the lower-activity pick per track."""
    started = time.monotonic()
    routing = routing_of(request)
    a = Point(body.origin.lat, body.origin.lng)
    b = Point(body.destination.lat, body.destination.lng)

    city_a = await city_for_point(routing, a.lat, a.lng)
    city_b = await city_for_point(routing, b.lat, b.lng)
    if city_a is None or city_b is None:
        return JSONResponse(
            {
                "detail": "Both points have to be inside one of the cities this map covers.",
                "code": "outside",
            },
            status_code=422,
            headers=NO_STORE,
        )
    if city_a["source_id"] != city_b["source_id"]:
        return JSONResponse(
            {
                "detail": "Both points have to be in the same city.",
                "code": "different_cities",
                "cities": [city_a["source_id"], city_b["source_id"]],
            },
            status_code=422,
            headers=NO_STORE,
        )
    city = city_a
    straight_km = haversine_m((a.lat, a.lng), (b.lat, b.lng)) / 1000
    if straight_km > settings.route_max_km:
        return JSONResponse(
            {"detail": "Those two points are too far apart to route.", "code": "too_far"},
            status_code=422,
            headers=NO_STORE,
        )

    departure = _departure(city, body.depart_at)

    context = await db(_window_and_baseline, city["source_id"], body.window)
    window = context["window"]
    scorer = _Scorer(
        city["source_id"],
        window,
        bool(context.get("hourly")),
        _baselines(context["rows"]),
        departure,
    )

    if body.mode == "transit":
        # The per-client throttle is charged only when a plan actually goes
        # to Transitous: not for a cache hit, and not for a call that failed.
        throttle, key = routing.transit_throttle, client_key(request.scope)
        routes = await routing.transitous.plan(
            a,
            b,
            departure.astimezone(UTC),
            before_call=lambda: throttle.check(key),
            after_failure=lambda: throttle.forget(key),
        )
    else:
        routes = await _street_routes(routing, scorer, a, b, body.mode)
    if not routes:
        raise NoRoute(reason="nothing left after filtering")

    runs = [scoring.route_runs(r) for r in routes]
    hours = [scorer.hours(r, rr) for r, rr in zip(routes, runs)]
    await scorer.load(
        set().union(*(scoring.route_cells(rr) for rr in runs)),
        {h for hs in hours for h in hs if h is not None},
    )

    activity: dict[str, list[dict[str, Any]]] = {}
    picks: dict[str, dict[str, Any]] = {}
    for track in scoring.TRACKS:
        activity[track] = [
            scoring.track_activity(rr, scorer.ratios(track, rr, hs))
            for rr, hs in zip(runs, hours)
        ]
        picks[track] = scoring.pick(body.mode, routes, activity[track])

    out_routes = []
    for i, route in enumerate(routes):
        entry = route.to_dict()
        entry["activity"] = {track: activity[track][i] for track in scoring.TRACKS}
        out_routes.append(entry)

    caveats = list(CAVEATS)
    if body.mode == "transit":
        caveats.append(TRANSIT_CAVEAT)
    log.info(
        "directions %s %s: %d routes in %.0f ms",
        body.mode,
        city["source_id"],
        len(routes),
        (time.monotonic() - started) * 1000,
    )
    return JSONResponse(
        {
            "city": city["source_id"],
            "mode": body.mode,
            "from": a.to_dict(),
            "to": b.to_dict(),
            "depart_at": departure.strftime("%Y-%m-%dT%H:%M"),
            "timezone": str(departure.tzinfo),
            "window": {"id": window["id"], "label": window["label"]} if window else None,
            # Whether each stretch was compared at the hour it would be passed.
            "hourly": scorer.hourly,
            "routes": out_routes,
            "picks": picks,
            "rules": {
                "min_rated_share": scoring.MIN_RATED_SHARE,
                "min_relative_drop": scoring.MIN_RELATIVE_DROP,
                "min_absolute_drop": scoring.MIN_ABSOLUTE_DROP,
            },
            "caveats": caveats,
            "attribution": ATTRIBUTION,
        },
        headers=NO_STORE,
    )
