"""Clients for the three routing providers, each normalising to schema.py.

    ValhallaClient    self-hosted, drive / walk / bike    POST /route, GET /status
    TransitousClient  public MOTIS, transit               GET {transitous_plan_path}
    PhotonClient      public, address search + reverse    GET /api, GET /reverse

Every client is handed the one shared httpx.AsyncClient (created in the API's
lifespan) and builds every URL from settings -- a base URL and a fixed path --
so nothing a visitor sends can choose the host a request goes to. Redirects are
not followed (httpx's default), for the same reason.

Caching, the daily budget and the circuit breaker sit inside the clients, in
that order: a cache hit costs nothing and is served even while the breaker is
open or the budget is spent; a miss is charged to the budget, then checked
against the breaker, then made. The caches hold the provider's raw JSON and
normalise on every read, so no caller can mutate an object another caller is
holding.

Every behaviour below that looks arbitrary was measured against the live
services during research (plan "Research corrections" R1-R3); the fixtures in
tests/fixtures/routing/ are those answers.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Hashable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from safety import PIPELINE_VERSION
from safety.config import settings
from safety.routing.breaker import CircuitBreaker
from safety.routing.budget import DailyBudget
from safety.routing.errors import (
    BadRequest,
    NoRoute,
    OutOfCoverage,
    ProviderTimeout,
    ProviderUnavailable,
    RoutingError,
)
from safety.routing.polyline import decode
from safety.routing.schema import (
    Place,
    Point,
    Route,
    Step,
    TransitLeg,
    TransitStop,
    clean_text,
    finite,
    line_length_m,
)
from safety.routing.ttlcache import TTLCache

log = logging.getLogger(__name__)

# httpx logs every request line at INFO, query string included -- for Photon
# that is the address someone typed, for Transitous both ends of their trip.
# Pinned to WARNING here, where the clients that would emit them live, so no
# logging configuration elsewhere (a basicConfig at INFO, uvicorn --log-level
# debug) can turn them on by accident.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def user_agent() -> str:
    """Transitous and Photon ask for an identifiable client with a contact;
    Transitous also wants a version (R2)."""
    return f"SafetyMap/{PIPELINE_VERSION} (+{settings.routing_contact})"


def as_point(value: Point | Sequence[float]) -> Point:
    if isinstance(value, Point):
        point = value
    else:
        lat, lng = value
        point = Point(float(lat), float(lng))
    if not (
        math.isfinite(point.lat)
        and math.isfinite(point.lng)
        and -90 <= point.lat <= 90
        and -180 <= point.lng <= 180
    ):
        raise ValueError("point out of range")
    return point


def _join(segments: Iterable[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Concatenate line segments, dropping the shared point where one ends
    and the next begins."""
    out: list[tuple[float, float]] = []
    for seg in segments:
        if out and seg and out[-1] == seg[0]:
            out.extend(seg[1:])
        else:
            out.extend(seg)
    return out


class _Client:
    """The common request path: cache, budget, breaker, call, log, map errors."""

    provider = ""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        timeout: float | None = None,
        cache: TTLCache | None = None,
        budget: DailyBudget | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.timeout = settings.routing_timeout_seconds if timeout is None else timeout
        self.cache = cache
        self.budget = budget
        self.breaker = breaker or CircuitBreaker()

    async def _cached(self, key: Hashable, producer: Callable[[], Awaitable[Any]]) -> Any:
        if self.cache is None:
            return await producer()
        return await self.cache.get_or_create(key, producer)

    def _error_for(self, status: int, payload: Any) -> RoutingError:
        """A 4xx answer as one of errors.py's exceptions. Per provider."""
        return BadRequest(provider=self.provider, reason=f"HTTP {status}")

    async def _call(
        self,
        op: str,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> Any:
        """Make one upstream call and return its parsed JSON body.

        An open breaker is checked before the budget is charged, so an outage
        does not burn the day's allowance on calls never made. The budget is
        charged before the breaker's half-open trial is claimed, so a spent
        budget cannot strand that trial; losing the trial to a concurrent
        caller refunds the charge.
        """
        if not self.breaker.available:
            raise ProviderUnavailable(provider=self.provider, reason="circuit open")
        if self.budget is not None:
            self.budget.take()
        if not self.breaker.allow():
            if self.budget is not None:
                self.budget.refund()
            raise ProviderUnavailable(provider=self.provider, reason="circuit open")

        started = time.monotonic()
        try:
            response = await self.http.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json,
                headers={"User-Agent": user_agent(), "Accept": "application/json"},
                timeout=self.timeout,
            )
        # `from None` throughout: httpx exception text can carry the request
        # URL, and with it the query string, into a logged traceback.
        except httpx.TimeoutException as exc:
            self.breaker.record_failure()
            log.warning("%s %s timed out (%s)", self.provider, op, type(exc).__name__)
            raise ProviderTimeout(provider=self.provider, reason=type(exc).__name__) from None
        except httpx.HTTPError as exc:
            self.breaker.record_failure()
            log.warning("%s %s failed (%s)", self.provider, op, type(exc).__name__)
            raise ProviderUnavailable(provider=self.provider, reason=type(exc).__name__) from None
        except BaseException:
            # Anything else (httpx.InvalidURL from a bad setting, cancellation
            # at shutdown) says nothing about the provider. Hand back the
            # trial and the charge, or a half-open breaker would wait forever
            # for this call to report.
            self.breaker.release()
            if self.budget is not None:
                self.budget.refund()
            raise

        elapsed_ms = (time.monotonic() - started) * 1000
        status = response.status_code
        log.info("%s %s -> %d in %.0f ms", self.provider, op, status, elapsed_ms)

        # 429 is the provider asking us to back off, which is what an open
        # breaker does; 3xx is never expected (redirects are not followed).
        if status >= 500 or status == 429 or 300 <= status < 400:
            self.breaker.record_failure()
            raise ProviderUnavailable(provider=self.provider, reason=f"HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if status >= 400:
            # The provider is up and said no: that is a working provider.
            self.breaker.record_success()
            raise self._error_for(status, payload)
        if not isinstance(payload, dict):
            self.breaker.record_failure()
            raise ProviderUnavailable(provider=self.provider, reason="unreadable body")
        self.breaker.record_success()
        return payload


# ---------------------------------------------------------------------------
# Valhalla
# ---------------------------------------------------------------------------

COSTING = {"drive": "auto", "walk": "pedestrian", "bike": "bicycle"}

# Every Valhalla error is HTTP 400 with an `error_code` (R1). The ones that
# mean something to a visitor:
_VALHALLA_NO_EDGES = 171  # no road or path near a location: off the graph
_VALHALLA_TOO_FAR = 154  # over the service's distance limit, checked before snapping


def valhalla_error(status: int, payload: Any) -> RoutingError:
    code = payload.get("error_code") if isinstance(payload, dict) else None
    reason = f"HTTP {status} code {code}"
    if not isinstance(code, int):
        return BadRequest(provider="valhalla", reason=reason)
    if code == _VALHALLA_NO_EDGES:
        return OutOfCoverage(provider="valhalla", reason=reason)
    if code == _VALHALLA_TOO_FAR:
        return OutOfCoverage("distance", provider="valhalla", reason=reason)
    # 1xx: the request itself (parse, options, locations). Ours are built
    # here, so one of these is a bug or a bound we let through -- not the
    # visitor's fault, but nothing a retry fixes either.
    if 100 <= code < 200:
        return BadRequest(provider="valhalla", reason=reason)
    # 44x: the path search ran and found nothing (442 "No path could be
    # found"), or anything else the router reports about these points.
    return NoRoute(provider="valhalla", reason=reason)


def _valhalla_trip(trip: dict[str, Any], mode: str) -> Route | None:
    summary = trip.get("summary") or {}
    duration = finite(summary.get("time"))
    length = finite(summary.get("length"))
    if duration is None or length is None:
        return None
    # We ask for kilometres; honour miles anyway rather than be off by 1.6x
    # if a config ever changes the default.
    metres = 1609.344 if trip.get("units") == "miles" else 1000.0

    segments: list[list[tuple[float, float]]] = []
    steps: list[Step] = []
    offset = 0
    for leg in trip.get("legs") or []:
        shape = decode(leg.get("shape") or "", 6)
        # Shape indexes are per leg; a joined geometry drops each later leg's
        # first point (the shared via point), so later legs shift by one less.
        shift = offset - (1 if segments and shape else 0)
        for m in leg.get("maneuvers") or []:
            steps.append(
                Step(
                    instruction=clean_text(m.get("instruction"), 240) or "",
                    distance_m=(finite(m.get("length")) or 0.0) * metres,
                    duration_s=finite(m.get("time")) or 0.0,
                    street_names=[
                        n for n in (clean_text(s, 80) for s in m.get("street_names") or []) if n
                    ][:4],
                    maneuver_type=m.get("type") if isinstance(m.get("type"), int) else None,
                    begin_index=max(0, int(m.get("begin_shape_index") or 0) + shift),
                    end_index=max(0, int(m.get("end_shape_index") or 0) + shift),
                )
            )
        segments.append(shape)
        offset = shift + len(shape)

    geometry = _join(segments)
    if len(geometry) < 2:
        return None
    return Route(
        mode=mode, duration_s=duration, distance_m=length * metres, geometry=geometry, steps=steps
    )


def normalise_valhalla(payload: dict[str, Any], mode: str) -> list[Route]:
    """Every trip in a /route answer, fastest first.

    `alternates` is absent, not empty, when Valhalla found none or silently
    dropped the request for them (R1: with date_time, or 3+ locations). And an
    alternate can be faster than the main trip -- the DC fixture's are -- so
    index 0 is not "the fastest" until this has sorted.
    """
    trips = [payload.get("trip")] + [
        alt.get("trip") for alt in payload.get("alternates") or [] if isinstance(alt, dict)
    ]
    routes = [r for r in (_valhalla_trip(t, mode) for t in trips if isinstance(t, dict)) if r]
    if not routes:
        raise NoRoute(provider="valhalla", reason="no usable trip")
    routes.sort(key=lambda r: r.duration_s)
    return routes


class ValhallaClient(_Client):
    provider = "valhalla"

    def __init__(self, http: httpx.AsyncClient, base_url: str | None = None, **kw: Any) -> None:
        super().__init__(http, base_url or settings.valhalla_url, **kw)
        # The /status probe is cached apart from routes: /directions/status is
        # polled by every page that opens the panel, and a probe per poll would
        # be wasted on a container that changes state a few times a year.
        self._probe = TTLCache(maxsize=1, ttl=60)

    def _error_for(self, status: int, payload: Any) -> RoutingError:
        return valhalla_error(status, payload)

    async def route(
        self,
        points: Sequence[Point | Sequence[float]],
        mode: str,
        alternates: int = 0,
        depart_at_local: str | None = None,
    ) -> list[Route]:
        """Routes through `points` in `mode` (drive, walk, bike), fastest first.

        `alternates` is honoured only between exactly two points; Valhalla
        would drop it silently otherwise (R1), and asking anyway would make the
        cache key claim something the answer does not have. `depart_at_local`
        (ISO local time, "YYYY-MM-DDTHH:MM") is sent only when no alternates
        are asked for, for the same reason: the two cannot be combined.
        """
        if mode not in COSTING:
            raise ValueError(f"unknown mode {mode!r}")
        pts = [as_point(p) for p in points]
        if len(pts) < 2:
            raise ValueError("a route needs at least two points")
        alts = max(0, min(int(alternates), 3)) if len(pts) == 2 else 0
        when = None if alts else depart_at_local

        body: dict[str, Any] = {
            # The ends are stops; a via point (a detour, detours.py) is passed
            # through, so the trip has no "arrive" maneuver in the middle.
            "locations": [
                {"lat": p.lat, "lon": p.lng, "type": "break" if i in (0, len(pts) - 1) else "through"}
                for i, p in enumerate(pts)
            ],
            "costing": COSTING[mode],
            "units": "kilometers",
            "directions_options": {"units": "kilometers", "language": "en-US"},
        }
        if alts:
            body["alternates"] = alts
        if when:
            body["date_time"] = {"type": 1, "value": when}

        # 4 dp is ~10 m: requests from the same doorstep share an answer.
        key = ("route", mode, tuple(p.rounded(4) for p in pts), alts, when)
        payload = await self._cached(key, lambda: self._call("route", "POST", "/route", json=body))
        return normalise_valhalla(payload, mode)

    async def probe(self) -> bool:
        """Whether the container answers /status. Cached 60 s; never raises.

        Kept off the breaker: polling status must not be what trips it.
        """

        async def check() -> bool:
            try:
                response = await self.http.get(
                    f"{self.base_url}/status",
                    headers={"User-Agent": user_agent()},
                    timeout=min(self.timeout, 3.0),
                )
            except httpx.HTTPError as exc:
                log.warning("valhalla status probe failed (%s)", type(exc).__name__)
                return False
            if response.status_code != 200:
                log.warning("valhalla status probe -> %d", response.status_code)
                return False
            return True

        return await self._probe.get_or_create("status", check)


# ---------------------------------------------------------------------------
# Transitous (MOTIS)
# ---------------------------------------------------------------------------

# The modes that move on the street network. Anything else -- BUS, TRAM,
# SUBWAY, RAIL, FERRY, and on-demand ODM/FLEX -- is a ride on a vehicle
# someone else drives, which is what "transit" means here (R2).
STREET_MODES = frozenset({"WALK", "BIKE", "CAR", "RENTAL", "CAR_PARKING", "CAR_DROPOFF"})
MAX_ITINERARIES = 5
# One departure bucket for the cache: a plan asked for at 17:02 is served to
# 17:04 as well. Five minutes is the staleness the plan accepts for a
# departure board.
DEPARTURE_BUCKET_S = 300

_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def safe_hex(value: Any) -> str | None:
    """A GTFS route colour as '#rrggbb', or None.

    GTFS gives six hex digits with no '#', and MOTIS passes them through as
    is (R2) -- or omits them. Anything else is dropped, never repaired: a
    colour reaches the page's style attributes and map paint, and the only
    safe string there is one this regex built.
    """
    if not isinstance(value, str):
        return None
    match = _HEX.match(value.strip())
    return f"#{match.group(1).lower()}" if match else None


def route_name(short: Any, long: Any) -> str | None:
    """What to call a line. A one-letter or empty short name is a code, not a
    name -- WMATA's Red Line is routeShortName "R" -- so the long name is
    used instead (R2)."""
    short_text = clean_text(short, 40)
    long_text = clean_text(long, 80)
    if long_text and (not short_text or len(short_text) <= 1):
        return long_text
    return short_text or long_text


def _stop(place: Any) -> TransitStop:
    place = place if isinstance(place, dict) else {}
    lat, lng = finite(place.get("lat")), finite(place.get("lon"))
    if lat is None or lng is None:
        raise ValueError("place without coordinates")
    name = clean_text(place.get("name"), 120)
    # MOTIS names the trip's own ends START and END; those are the visitor's
    # points, which the page labels itself.
    if name in ("START", "END"):
        name = None
    return TransitStop(
        name=name, point=as_point((lat, lng)), stop_id=clean_text(place.get("stopId"), 120)
    )


def _motis_leg(leg: dict[str, Any]) -> TransitLeg:
    mode = (clean_text(leg.get("mode"), 24) or "UNKNOWN").upper()
    transit = mode not in STREET_MODES
    origin, destination = _stop(leg.get("from")), _stop(leg.get("to"))

    # Precision is per leg and varies (6 and 7 both seen, R2). Walking legs
    # are scored on this geometry (Phase 3), never on `steps`.
    geometry_spec = leg.get("legGeometry") or {}
    precision = geometry_spec.get("precision", 6)
    if not isinstance(precision, int) or not 1 <= precision <= 10:
        raise ValueError("bad polyline precision")
    points = decode(geometry_spec.get("points") or "", precision)
    if len(points) < 2:
        points = [
            (origin.point.lat, origin.point.lng),
            (destination.point.lat, destination.point.lng),
        ]

    distance = finite(leg.get("distance"))
    if distance is None:
        distance = line_length_m(points)
    intermediate = leg.get("intermediateStops")
    stops = (len(intermediate) if isinstance(intermediate, list) else 0) + 1
    return TransitLeg(
        mode=mode,
        transit=transit,
        start_time=clean_text(leg.get("startTime"), 40),
        end_time=clean_text(leg.get("endTime"), 40),
        duration_s=finite(leg.get("duration")) or 0.0,
        distance_m=distance,
        origin=origin,
        destination=destination,
        geometry=points,
        route_name=(
            route_name(leg.get("routeShortName"), leg.get("routeLongName")) if transit else None
        ),
        route_long_name=clean_text(leg.get("routeLongName"), 80) if transit else None,
        headsign=clean_text(leg.get("headsign"), 80) if transit else None,
        agency=clean_text(leg.get("agencyName"), 80) if transit else None,
        color=safe_hex(leg.get("routeColor")) if transit else None,
        text_color=safe_hex(leg.get("routeTextColor")) if transit else None,
        num_stops=stops if transit else None,
    )


def _motis_itinerary(itinerary: dict[str, Any]) -> Route | None:
    try:
        legs = [_motis_leg(leg) for leg in itinerary.get("legs") or [] if isinstance(leg, dict)]
    except ValueError:
        # One malformed leg spoils the itinerary, not the whole plan.
        return None
    rides = [leg for leg in legs if leg.transit]
    if not rides:
        # A walk-only answer is not a transit route; walking is its own mode.
        return None
    duration = finite(itinerary.get("duration"))
    if duration is None:
        duration = sum(leg.duration_s for leg in legs)
    transfers = itinerary.get("transfers")
    return Route(
        mode="transit",
        duration_s=duration,
        distance_m=sum(leg.distance_m or 0.0 for leg in legs),
        geometry=_join(leg.geometry for leg in legs),
        legs=legs,
        start_time=clean_text(itinerary.get("startTime"), 40),
        end_time=clean_text(itinerary.get("endTime"), 40),
        transfers=transfers if isinstance(transfers, int) else max(0, len(rides) - 1),
    )


def _stop_sequence(route: Route) -> tuple:
    return tuple((leg.origin.stop_id, leg.destination.stop_id) for leg in route.legs if leg.transit)


def normalise_motis(payload: dict[str, Any]) -> list[Route]:
    """Up to five distinct itineraries, fastest first.

    MOTIS returns itineraries in departure order, and at least -- not at
    most -- numItineraries of them (R2: 8 for Philadelphia and Seattle). Many
    are the same journey: Philadelphia's T2, T3 and T4 trolleys run between the
    same two stops, and DC's Red Line repeats every four minutes. Itineraries
    that board and alight at the same stops are one journey here: the earliest
    is kept and the others ride along as `also_departs`.
    """
    candidates = [
        r
        for r in (
            _motis_itinerary(it) for it in payload.get("itineraries") or [] if isinstance(it, dict)
        )
        if r
    ]
    # ISO-8601 UTC strings in one format sort chronologically.
    candidates.sort(key=lambda r: r.start_time or "~")
    kept: dict[tuple, Route] = {}
    for route in candidates:
        key = _stop_sequence(route)
        first = kept.get(key)
        if first is None:
            kept[key] = route
        else:
            first.also_departs.append(
                {
                    "start_time": route.start_time,
                    "route_names": [leg.route_name for leg in route.legs if leg.transit],
                }
            )
    routes = sorted(kept.values(), key=lambda r: (r.duration_s, r.start_time or "~"))
    if not routes:
        raise NoRoute(provider="transitous", reason="no transit itinerary")
    return routes[:MAX_ITINERARIES]


def _utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("depart_at_utc must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TransitousClient(_Client):
    provider = "transitous"

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str | None = None,
        plan_path: str | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(http, base_url or settings.transitous_url, **kw)
        self.plan_path = plan_path or settings.transitous_plan_path

    def _error_for(self, status: int, payload: Any) -> RoutingError:
        # Errors are {"error": "..."} with 400, 404, 422 or 500 (R2). A 404 or
        # 422 is "nothing found for these places"; 400 is our request.
        reason = f"HTTP {status}"
        if status in (404, 422):
            return NoRoute(provider=self.provider, reason=reason)
        return BadRequest(provider=self.provider, reason=reason)

    async def plan(
        self,
        origin: Point | Sequence[float],
        destination: Point | Sequence[float],
        depart_at_utc: datetime,
        *,
        before_call: Callable[[], None] | None = None,
        after_failure: Callable[[], None] | None = None,
    ) -> list[Route]:
        """Itineraries from `origin`. `before_call` runs only when the plan
        is actually requested upstream (not on a cache hit) and may raise to
        stop it; `after_failure` runs if that request then fails."""
        a, b = as_point(origin), as_point(destination)
        when = _utc_iso(depart_at_utc)
        params = {
            "fromPlace": f"{a.lat:.6f},{a.lng:.6f}",
            "toPlace": f"{b.lat:.6f},{b.lng:.6f}",
            "time": when,
            "arriveBy": "false",
            # A minimum, not a cap (R2); normalise_motis() caps.
            "numItineraries": str(MAX_ITINERARIES),
            # Walking legs between platforms come back with real geometry,
            # which is what Phase 3 scores.
            "detailedTransfers": "true",
        }
        bucket = int(depart_at_utc.timestamp()) // DEPARTURE_BUCKET_S
        # Keyed on the exact points sent, not rounded ones: the walking legs
        # start and end at fromPlace and toPlace, so a payload shared across
        # nearby requests would show one visitor where another stood.
        key = ("plan", params["fromPlace"], params["toPlace"], bucket)

        async def fetch() -> Any:
            if before_call is not None:
                before_call()
            try:
                return await self._call("plan", "GET", self.plan_path, params=params)
            except BaseException:
                if after_failure is not None:
                    after_failure()
                raise

        payload = await self._cached(key, fetch)
        return normalise_motis(payload)


# ---------------------------------------------------------------------------
# Photon
# ---------------------------------------------------------------------------

# Too coarse to route to: a search for "Philadelphia" should offer places in
# it, not the county polygon's centroid (R3).
_DROP_TYPES = frozenset({"county", "state", "country"})

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}  # fmt: skip
_STATE_CODES = frozenset(US_STATES.values())


def state_abbrev(value: Any) -> str | None:
    """'Pennsylvania' and 'PA' both -> 'PA': Photon returns either (R3)."""
    text = clean_text(value, 40)
    if not text:
        return None
    if text.upper() in _STATE_CODES:
        return text.upper()
    return US_STATES.get(text.casefold(), text)


def photon_label(props: dict[str, Any]) -> tuple[str, str] | None:
    """(title, subtitle) for one Photon feature, by the R3 rule.

    The first ';'-part of the name; then "housenumber street" (not for a
    street, and not when the name already starts with the house number); then
    the city, or failing that locality, district, county; then the state,
    abbreviated, with the postcode (not for a street -- one street spans many
    postcodes, and keeping it would split one street into several results).
    Empty parts are dropped, and a part repeating the one before it.
    """
    kind = props.get("type")
    is_street = kind == "street"
    name = clean_text(str(props.get("name") or "").split(";")[0], 120)
    number = clean_text(props.get("housenumber"), 20)
    street = clean_text(props.get("street"), 120)

    address = None
    if not is_street and street:
        address = f"{number} {street}" if number else street
        if name and number and name.startswith(number):
            address = None
    place = next(
        (
            v
            for v in (
                clean_text(props.get(k), 80) for k in ("city", "locality", "district", "county")
            )
            if v
        ),
        None,
    )
    postcode = None if is_street else clean_text(props.get("postcode"), 12)
    region = " ".join(p for p in (state_abbrev(props.get("state")), postcode) if p)

    parts: list[str] = []
    for part in (name, address, place, region or None):
        if part and (not parts or part.casefold() != parts[-1].casefold()):
            parts.append(part)
    if not parts:
        return None
    return parts[0], ", ".join(parts[1:])


def _in_bbox(lat: float, lng: float, bbox: Sequence[float]) -> bool:
    west, south, east, north = bbox
    return west <= lng <= east and south <= lat <= north


def normalise_photon(payload: dict[str, Any], bbox: Sequence[float] | None = None) -> list[Place]:
    """Photon features as Places: coarse types dropped, outside `bbox`
    dropped, labelled, and deduplicated on the label (case-insensitive).

    The bbox is also sent with the request, but checked again here: it is what
    keeps a Philadelphia search out of New Jersey (R3), and a response is not
    a guarantee. Bus stops are excluded upstream; dropped here too, in case.
    """
    places: list[Place] = []
    seen: set[str] = set()
    for feature in payload.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        if not isinstance(props, dict) or props.get("type") in _DROP_TYPES:
            continue
        if props.get("osm_key") == "highway" and props.get("osm_value") == "bus_stop":
            continue
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lng, lat = finite(coords[0]), finite(coords[1])
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        if bbox is not None and not _in_bbox(lat, lng, bbox):
            continue
        label = photon_label(props)
        if label is None:
            continue
        title, subtitle = label
        full = f"{title}, {subtitle}" if subtitle else title
        if full.casefold() in seen:
            continue
        seen.add(full.casefold())
        places.append(
            Place(
                label=full,
                title=title,
                subtitle=subtitle,
                point=Point(lat, lng),
                kind=clean_text(props.get("type"), 24),
            )
        )
    return places


def normalise_query(q: str) -> str:
    """The cache key form of a query: whitespace collapsed, case folded."""
    return " ".join(q.split()).casefold()


class PhotonClient(_Client):
    provider = "photon"

    def __init__(self, http: httpx.AsyncClient, base_url: str | None = None, **kw: Any) -> None:
        super().__init__(http, base_url or settings.photon_url, **kw)

    async def search(
        self,
        q: str,
        city_bbox: Sequence[float],
        centre: Sequence[float],
        limit: int = 5,
    ) -> list[Place]:
        """Places matching `q` inside `city_bbox` (west, south, east, north),
        biased towards `centre` (lat, lng).

        Returns up to limit + 3 results, not `limit`: the caller drops more
        (served-cell check, safety/api/directions.py) and truncates after.
        """
        request_limit = min(int(limit) + 3, 10)
        bbox = tuple(round(float(v), 6) for v in city_bbox)
        lat, lng = (round(float(v), 5) for v in centre)
        params = [
            ("q", " ".join(q.split())),
            ("bbox", ",".join(f"{v:.6f}" for v in bbox)),
            ("lat", f"{lat:.5f}"),
            ("lon", f"{lng:.5f}"),
            ("zoom", "12"),
            ("location_bias_scale", "0.2"),
            ("limit", str(request_limit)),
            ("lang", "en"),
            # Without it, partial street queries return mostly bus stops (R3).
            # `exclude` rather than the deprecated osm_tag=!highway:bus_stop.
            ("exclude", "osm.highway.bus_stop"),
        ]
        key = ("search", normalise_query(q), bbox, request_limit)
        payload = await self._cached(
            key, lambda: self._call("search", "GET", "/api", params=params)
        )
        return normalise_photon(payload, bbox)

    async def reverse(self, lat: float, lng: float) -> Place | None:
        """The nearest house or street to a point within ~200 m, or None."""
        point = as_point((lat, lng))
        params = [
            ("lat", f"{point.lat:.6f}"),
            ("lon", f"{point.lng:.6f}"),
            ("radius", "0.2"),
            ("limit", "1"),
            ("lang", "en"),
            ("layer", "house"),
            ("layer", "street"),
        ]
        key = ("reverse", point.rounded(4))
        payload = await self._cached(
            key, lambda: self._call("reverse", "GET", "/reverse", params=params)
        )
        places = normalise_photon(payload)
        return places[0] if places else None
