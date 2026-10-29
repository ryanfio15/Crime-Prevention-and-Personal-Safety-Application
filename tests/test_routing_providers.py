"""Provider clients against the answers captured from the live services
(tests/fixtures/routing/), through httpx.MockTransport: no network.

Covers normalisation, ordering, the request each client sends, error mapping,
the User-Agent, the cache/budget/breaker path, and city_at()."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from safety import PIPELINE_VERSION
from safety.config import settings
from safety.routing.breaker import CircuitBreaker
from safety.routing.budget import DailyBudget
from safety.routing.cities import city_at
from safety.routing.errors import (
    BadRequest,
    BudgetExhausted,
    NoRoute,
    OutOfCoverage,
    ProviderTimeout,
    ProviderUnavailable,
)
from safety.routing.polyline import decode, encode
from safety.routing.providers import (
    PhotonClient,
    TransitousClient,
    ValhallaClient,
    normalise_motis,
    normalise_photon,
    photon_label,
    route_name,
    safe_hex,
    state_abbrev,
)
from safety.routing.ttlcache import TTLCache

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
PHL_BBOX = (-75.28, 39.86, -74.95, 40.14)
PHL_CENTRE = (39.9526, -75.1652)
DEPART = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class Upstream:
    """A MockTransport that answers with one canned response (or raises) and
    records every request it sees."""

    def __init__(self, body=None, status=200, raises=None):
        self.body, self.status, self.raises = body, status, raises
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if isinstance(self.body, (dict, list)):
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(self.status, text=self.body or "")

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def run(coro):
    return asyncio.run(coro)


A, B = (38.9096, -77.0434), (38.8973, -77.0063)


# ---------------------------------------------------------------------------
# Valhalla
# ---------------------------------------------------------------------------


def test_valhalla_alternates_are_flattened_and_sorted_by_duration():
    upstream = Upstream(fixture("valhalla_route_alt.json"))
    routes = run(ValhallaClient(upstream.client()).route([A, B], "drive", alternates=2))
    # The main trip (675 s) is the slowest of the three in this fixture.
    assert [round(r.duration_s) for r in routes] == [637, 658, 675]
    assert [round(r.distance_m) for r in routes] == [6060, 6301, 6106]
    for r in routes:
        assert r.mode == "drive"
        assert len(r.geometry) > 100
        assert r.steps and r.steps[0].instruction
        assert all(0 <= s.begin_index <= s.end_index < len(r.geometry) for s in r.steps)
    first = routes[-1].steps[0]
    assert first.instruction == "Drive northwest on Connecticut Avenue Northwest."
    assert first.distance_m == pytest.approx(587)
    assert first.street_names == ["Connecticut Avenue Northwest"]


def test_valhalla_missing_alternates_key_is_one_route():
    upstream = Upstream(fixture("valhalla_route_pedestrian.json"))
    routes = run(ValhallaClient(upstream.client()).route([A, B], "walk", alternates=2))
    assert len(routes) == 1
    assert routes[0].mode == "walk"
    assert routes[0].distance_m == pytest.approx(3657)


def test_valhalla_request_body():
    upstream = Upstream(fixture("valhalla_route_pedestrian.json"))
    client = ValhallaClient(upstream.client(), base_url="http://valhalla.test")
    run(client.route([A, B], "walk", alternates=2, depart_at_local="2026-11-02T17:00"))
    request = upstream.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://valhalla.test/route"
    body = json.loads(request.content)
    assert body["costing"] == "pedestrian"
    assert body["alternates"] == 2
    # Valhalla silently drops alternates when date_time is set (R1).
    assert "date_time" not in body
    assert body["locations"][0] == {"lat": A[0], "lon": A[1], "type": "break"}


def test_valhalla_date_time_only_without_alternates():
    upstream = Upstream(fixture("valhalla_route_pedestrian.json"))
    run(ValhallaClient(upstream.client()).route([A, B], "bike", depart_at_local="2026-11-02T17:00"))
    body = json.loads(upstream.requests[0].content)
    assert body["costing"] == "bicycle"
    assert body["date_time"] == {"type": 1, "value": "2026-11-02T17:00"}
    assert "alternates" not in body


def test_valhalla_no_alternates_through_a_via_point():
    upstream = Upstream(fixture("valhalla_route_pedestrian.json"))
    run(ValhallaClient(upstream.client()).route([A, (38.90, -77.02), B], "walk", alternates=2))
    body = json.loads(upstream.requests[0].content)
    assert len(body["locations"]) == 3
    assert "alternates" not in body
    # A detour's via point is passed through, not stopped at.
    assert [loc["type"] for loc in body["locations"]] == ["break", "through", "break"]


def test_valhalla_sends_the_user_agent():
    upstream = Upstream(fixture("valhalla_route_pedestrian.json"))
    run(ValhallaClient(upstream.client()).route([A, B], "walk"))
    ua = upstream.requests[0].headers["user-agent"]
    assert ua == f"SafetyMap/{PIPELINE_VERSION} (+{settings.routing_contact})"


@pytest.mark.parametrize(
    ("body", "exc", "code"),
    [
        (fixture("valhalla_no_route.json"), OutOfCoverage, "out_of_coverage"),
        (fixture("valhalla_no_path.json"), NoRoute, "no_route"),
        ({"error_code": 154, "error": "Path distance exceeds the max distance limit"}, OutOfCoverage, "too_far"),
        ({"error_code": 130, "error": "Failed to parse location"}, BadRequest, "bad_request"),
        ({"error_code": 120, "error": "Insufficient number of locations provided"}, BadRequest, "bad_request"),
        ("not json", BadRequest, "bad_request"),
    ],
)
def test_valhalla_error_mapping(body, exc, code):
    upstream = Upstream(body, status=400)
    client = ValhallaClient(upstream.client())
    with pytest.raises(exc) as info:
        run(client.route([A, B], "drive"))
    assert info.value.code == code
    # The visitor's message is ours, never the provider's text.
    if isinstance(body, dict):
        assert body["error"] not in info.value.message
    # A 400 is a working provider: it must not count towards the breaker.
    assert client.breaker.available and client.breaker._failures == 0


def test_error_status_codes():
    assert OutOfCoverage().status == 422
    assert NoRoute().status == 422
    assert BadRequest().status == 400
    assert ProviderUnavailable().status == 503
    assert ProviderTimeout().status == 504
    assert BudgetExhausted().status == 503


@pytest.mark.parametrize(
    ("upstream", "exc"),
    [
        (Upstream("boom", status=500), ProviderUnavailable),
        (Upstream("slow down", status=429), ProviderUnavailable),
        (Upstream(raises=httpx.ConnectError("refused")), ProviderUnavailable),
        (Upstream(raises=httpx.ReadTimeout("slow")), ProviderTimeout),
        (Upstream("<html>gateway</html>", status=200), ProviderUnavailable),
    ],
)
def test_valhalla_outages(upstream, exc):
    client = ValhallaClient(upstream.client())
    with pytest.raises(exc):
        run(client.route([A, B], "drive"))
    assert client.breaker._failures == 1


def test_breaker_stops_calling_after_three_failures():
    upstream = Upstream(raises=httpx.ConnectError("refused"))
    client = ValhallaClient(upstream.client(), breaker=CircuitBreaker(threshold=3))
    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            run(client.route([A, B], "drive"))
    with pytest.raises(ProviderUnavailable) as info:
        run(client.route([A, B], "drive"))
    assert info.value.reason == "circuit open"
    assert len(upstream.requests) == 3


def test_valhalla_cache_serves_the_second_call():
    upstream = Upstream(fixture("valhalla_route_alt.json"))
    client = ValhallaClient(upstream.client(), cache=TTLCache(8, 60))
    first = run(client.route([A, B], "drive", alternates=2))
    # A few metres away rounds to the same 4 dp key.
    second = run(client.route([(A[0] + 0.00001, A[1]), B], "drive", alternates=2))
    assert len(upstream.requests) == 1
    assert [r.duration_s for r in first] == [r.duration_s for r in second]
    assert first[0] is not second[0]  # normalised per read: nothing shared to mutate


def test_valhalla_unknown_mode_is_a_programming_error():
    with pytest.raises(ValueError):
        run(ValhallaClient(Upstream({}).client()).route([A, B], "fly"))


def test_valhalla_probe():
    up = Upstream({"version": "3.9.1"})
    client = ValhallaClient(up.client())
    assert run(client.probe()) is True
    assert up.requests[0].url.path == "/status"
    down = ValhallaClient(Upstream(raises=httpx.ConnectError("refused")).client())
    assert run(down.probe()) is False
    # Probing never touches the breaker.
    assert down.breaker._failures == 0


# ---------------------------------------------------------------------------
# Transitous (MOTIS)
# ---------------------------------------------------------------------------


def test_motis_trolleys_over_the_same_stops_are_one_journey():
    routes = normalise_motis(fixture("motis_plan.json"))
    # T3, T2 and T4 all run 13th St -> 40th St Portal: one journey, earliest kept.
    assert len(routes) == 1
    route = routes[0]
    assert route.mode == "transit"
    assert route.start_time == "2026-11-02T22:00:00Z"
    assert route.duration_s == 960
    assert route.transfers == 0
    assert [a["route_names"] for a in route.also_departs] == [["T2"], ["T4"]]
    assert [a["start_time"] for a in route.also_departs] == [
        "2026-11-02T22:01:00Z",
        "2026-11-02T22:04:00Z",
    ]
    walk, ride, walk_off = route.legs
    assert (walk.mode, walk.transit, walk.route_name, walk.color) == ("WALK", False, None, None)
    assert ride.transit and ride.mode == "TRAM"
    assert ride.route_name == "T3"
    assert ride.color == "#5a960a" and ride.text_color == "#ffffff"
    assert ride.headsign == "Yeadon" and ride.agency == "SEPTA"
    assert ride.origin.name == "13th St" and ride.destination.name == "40th St Portal"
    assert walk.origin.name is None  # MOTIS's "START" is the visitor's own point
    assert not walk_off.transit
    # Joined geometry runs from the first leg's start to the last leg's end.
    assert route.geometry[0] == walk.geometry[0]
    assert route.geometry[-1] == walk_off.geometry[-1]


def test_wmata_one_letter_short_name_uses_the_long_name():
    routes = normalise_motis(fixture("motis_plan_dc.json"))
    assert len(routes) == 1  # the same Red Line trip every four minutes
    ride = next(leg for leg in routes[0].legs if leg.transit)
    assert ride.route_name == "Red"
    assert ride.color == "#c80f2d"
    assert ride.num_stops == 4
    assert len(routes[0].also_departs) == 2


def test_route_name_rule():
    assert route_name("R", "Red") == "Red"
    assert route_name("", "Green Line") == "Green Line"
    assert route_name(None, "Blue") == "Blue"
    assert route_name("T3", "13th St to Yeadon") == "T3"
    assert route_name("7", None) == "7"
    assert route_name(None, None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5a960a", "#5a960a"),
        ("C80F2D", "#c80f2d"),
        ("#00ff00", "#00ff00"),
        (" 123abc ", "#123abc"),
        ("fff", None),
        ("red", None),
        ("5a960a;background:url(x)", None),
        ("#5a960a\"><script>", None),
        ("", None),
        (None, None),
        (0x5A960A, None),
    ],
)
def test_colour_sanitising(raw, expected):
    assert safe_hex(raw) == expected


def test_motis_precision_is_read_per_leg():
    plan = copy.deepcopy(fixture("motis_plan_dc.json"))
    itinerary = plan["itineraries"][0]
    ride = itinerary["legs"][1]
    points = decode(ride["legGeometry"]["points"], 6)
    ride["legGeometry"] = {"points": encode(points, 7), "precision": 7, "length": len(points)}
    plan["itineraries"] = [itinerary]
    (route,) = normalise_motis(plan)
    decoded = route.legs[1].geometry
    assert len(decoded) == len(points)
    assert all(abs(a - c) < 1e-6 and abs(b - d) < 1e-6 for (a, b), (c, d) in zip(decoded, points))
    assert route.legs[0].geometry == decode(itinerary["legs"][0]["legGeometry"]["points"], 6)


def _itinerary(start, duration, stop):
    walk = {"lat": 38.9, "lon": -77.0}
    return {
        "duration": duration,
        "startTime": start,
        "endTime": start,
        "transfers": 0,
        "legs": [
            {
                "mode": "BUS",
                "from": {**walk, "name": f"Stop {stop}", "stopId": f"s{stop}"},
                "to": {"lat": 38.91, "lon": -77.01, "name": "End", "stopId": "end"},
                "duration": duration,
                "routeShortName": f"{stop}0",
                "routeColor": "zzzzzz",
                "legGeometry": {"points": encode([(38.9, -77.0), (38.91, -77.01)], 6), "precision": 6},
            }
        ],
    }


def test_motis_sorted_by_duration_and_capped_at_five():
    plan = {
        "itineraries": [
            _itinerary(f"2026-11-02T22:0{i}:00Z", duration, i)
            for i, duration in enumerate([900, 600, 1200, 700, 800, 650, 1000])
        ]
    }
    routes = normalise_motis(plan)
    assert [r.duration_s for r in routes] == [600, 650, 700, 800, 900]
    assert routes[0].legs[0].color is None  # "zzzzzz" is not a colour


def test_motis_walk_only_is_not_transit():
    walk_only = _itinerary("2026-11-02T22:00:00Z", 600, 1)
    walk_only["legs"][0]["mode"] = "WALK"
    with pytest.raises(NoRoute):
        normalise_motis({"itineraries": [walk_only], "direct": []})


def test_transitous_request():
    upstream = Upstream(fixture("motis_plan.json"))
    client = TransitousClient(upstream.client(), base_url="https://transit.test", plan_path="/api/v6/plan")
    run(client.plan((39.9524, -75.1636), (39.9496, -75.2033), DEPART))
    request = upstream.requests[0]
    assert request.method == "GET"
    assert request.url.host == "transit.test" and request.url.path == "/api/v6/plan"
    params = request.url.params
    assert params["fromPlace"] == "39.952400,-75.163600"
    assert params["toPlace"] == "39.949600,-75.203300"
    assert params["time"] == "2026-11-02T22:00:00Z"
    assert params["numItineraries"] == "5"
    assert params["arriveBy"] == "false"
    assert request.headers["user-agent"].startswith(f"SafetyMap/{PIPELINE_VERSION} (+")


def test_transitous_budget_is_charged_only_on_a_miss():
    upstream = Upstream(fixture("motis_plan.json"))
    budget = DailyBudget(1, "transitous")
    client = TransitousClient(upstream.client(), cache=TTLCache(8, 300), budget=budget)
    run(client.plan((39.9524, -75.1636), (39.9496, -75.2033), DEPART))
    # Same trip, two minutes later: the same 5-minute bucket, a cache hit.
    run(client.plan((39.9524, -75.1636), (39.9496, -75.2033), DEPART.replace(minute=2)))
    assert len(upstream.requests) == 1
    with pytest.raises(BudgetExhausted):
        run(client.plan((39.9524, -75.1636), (39.96, -75.2033), DEPART))
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(("status", "exc"), [(400, BadRequest), (404, NoRoute), (422, NoRoute), (500, ProviderUnavailable)])
def test_transitous_errors(status, exc):
    upstream = Upstream({"error": "internal detail at 39.95,-75.16"}, status=status)
    with pytest.raises(exc) as info:
        run(TransitousClient(upstream.client()).plan((39.95, -75.16), (39.94, -75.2), DEPART))
    assert "39.95" not in info.value.message


def test_transitous_needs_an_aware_time():
    with pytest.raises(ValueError):
        run(TransitousClient(Upstream({}).client()).plan(A, B, datetime(2026, 11, 2, 22, 0)))


# ---------------------------------------------------------------------------
# Photon
# ---------------------------------------------------------------------------


def test_photon_search_request():
    upstream = Upstream(fixture("photon_search.json"))
    client = PhotonClient(upstream.client(), base_url="https://photon.test")
    run(client.search("1400  JFK Blvd", PHL_BBOX, PHL_CENTRE, limit=5))
    request = upstream.requests[0]
    assert request.url.host == "photon.test" and request.url.path == "/api"
    params = request.url.params
    assert params["q"] == "1400 JFK Blvd"
    assert params["bbox"] == "-75.280000,39.860000,-74.950000,40.140000"
    assert (params["lat"], params["lon"]) == ("39.95260", "-75.16520")
    assert params["zoom"] == "12"
    assert params["location_bias_scale"] == "0.2"
    assert params["limit"] == "8"
    assert params["lang"] == "en"
    assert params["exclude"] == "osm.highway.bus_stop"
    assert "osm_tag" not in params
    assert request.headers["user-agent"].startswith("SafetyMap/")


def test_photon_limit_is_capped_at_ten():
    upstream = Upstream(fixture("photon_search.json"))
    run(PhotonClient(upstream.client()).search("market", PHL_BBOX, PHL_CENTRE, limit=9))
    assert upstream.requests[0].url.params["limit"] == "10"


def test_photon_house_label():
    (place,) = normalise_photon(fixture("photon_search.json"), PHL_BBOX)
    assert place.label == "Dilworth Park Café, 1400 John F. Kennedy Boulevard, Philadelphia, PA 19107"
    assert place.title == "Dilworth Park Café"
    assert place.subtitle == "1400 John F. Kennedy Boulevard, Philadelphia, PA 19107"
    assert (place.point.lat, place.point.lng) == (39.953206, -75.1643762)
    assert place.kind == "house"


def test_photon_streets_drop_the_postcode_and_dedupe():
    places = normalise_photon(fixture("photon_search_partial.json"), PHL_BBOX)
    labels = [p.label for p in places]
    # Three Market Street segments, three postcodes, "Pennsylvania" spelled out:
    # one result, abbreviated, no postcode.
    assert labels.count("Market Street, Philadelphia, PA") == 1
    assert labels[0] == "Market Street, Philadelphia, PA"
    assert "24th st Market & Deli, Oakford Street, Philadelphia, PA 19146" in labels
    assert len(labels) == len(set(labels))


def test_photon_bbox_drops_new_jersey_and_bus_stops():
    places = normalise_photon(fixture("photon_search_unbounded.json"), PHL_BBOX)
    assert [p.title for p in places] == ["Dilworth Park Café"]
    # Without a bbox only the bus stops go (excluded upstream, and again here).
    unbounded = normalise_photon(fixture("photon_search_unbounded.json"))
    assert [p.title for p in unbounded] == ["Dilworth Park Café", "3079 John F. Kennedy Blvd"]


def test_photon_coarse_types_are_dropped():
    payload = {
        "features": [
            {"properties": {"type": t, "name": f"A {t}", "state": "PA"}, "geometry": {"coordinates": [-75.16, 39.95]}}
            for t in ("county", "state", "country", "city")
        ]
    }
    assert [p.kind for p in normalise_photon(payload, PHL_BBOX)] == ["city"]


@pytest.mark.parametrize(
    ("props", "title", "subtitle"),
    [
        # The house number already leads the name: no second address line.
        ({"type": "other", "name": "3079 John F. Kennedy Blvd", "housenumber": "3079", "street": "John F. Kennedy Boulevard", "city": "Jersey City", "state": "New Jersey"}, "3079 John F. Kennedy Blvd", "Jersey City, NJ"),
        # Only the first ';'-part of a multi-valued name.
        ({"type": "house", "name": "Reading Terminal Market;RTM", "housenumber": "51", "street": "North 12th Street", "city": "Philadelphia", "state": "PA", "postcode": "19107"}, "Reading Terminal Market", "51 North 12th Street, Philadelphia, PA 19107"),
        # No name: the address line is the title.
        ({"type": "house", "housenumber": "1100", "street": "Congress Avenue", "city": "Austin", "state": "Texas", "postcode": "78701"}, "1100 Congress Avenue", "Austin, TX 78701"),
        # No city: locality, then district, then county.
        ({"type": "house", "name": "Cafe", "district": "Capitol Hill", "county": "King", "state": "WA"}, "Cafe", "Capitol Hill, WA"),
        ({"type": "street", "name": "K Street Northwest", "city": "Washington", "state": "District of Columbia", "postcode": "20001"}, "K Street Northwest", "Washington, DC"),
    ],
)
def test_photon_label_rule(props, title, subtitle):
    assert photon_label(props) == (title, subtitle)


def test_state_abbreviations():
    assert state_abbrev("Pennsylvania") == "PA"
    assert state_abbrev("pa") == "PA"
    assert state_abbrev("District of Columbia") == "DC"
    assert state_abbrev("Ontario") == "Ontario"
    assert state_abbrev(None) is None


def test_photon_strings_are_cleaned():
    payload = {
        "features": [
            {
                "properties": {"type": "house", "name": "Evil\x00  <b>Name</b>", "city": "Philadelphia"},
                "geometry": {"coordinates": [-75.16, 39.95]},
            }
        ]
    }
    (place,) = normalise_photon(payload)
    assert place.title == "Evil <b>Name</b>"  # controls gone; markup is the page's job (textContent)


def test_photon_reverse():
    upstream = Upstream(fixture("photon_reverse.json"))
    place = run(PhotonClient(upstream.client()).reverse(39.94946, -75.15028))
    assert place.label == "Liberty Bell, 526 Market Street, Philadelphia, PA 19106"
    request = upstream.requests[0]
    assert request.url.path == "/reverse"
    params = request.url.params
    assert params["radius"] == "0.2" and params["limit"] == "1" and params["lang"] == "en"
    assert params.get_list("layer") == ["house", "street"]


def test_photon_reverse_nothing_found():
    upstream = Upstream({"type": "FeatureCollection", "features": []})
    assert run(PhotonClient(upstream.client()).reverse(39.9, -75.1)) is None


def test_photon_cache_folds_case_and_spacing():
    upstream = Upstream(fixture("photon_search.json"))
    client = PhotonClient(upstream.client(), cache=TTLCache(8, 60))
    run(client.search("Dilworth Park", PHL_BBOX, PHL_CENTRE, 5))
    run(client.search("  dilworth   PARK ", PHL_BBOX, PHL_CENTRE, 5))
    assert len(upstream.requests) == 1


# ---------------------------------------------------------------------------
# city_at: the port of web/app.js cityAt
# ---------------------------------------------------------------------------

CITIES = [
    {"source_id": "a", "bbox_west": 0, "bbox_south": 0, "bbox_east": 2, "bbox_north": 2, "center_lat": 1, "center_lng": 1},
    {"source_id": "b", "bbox_west": 1.5, "bbox_south": 0, "bbox_east": 4, "bbox_north": 2, "center_lat": 1, "center_lng": 2.5},
    {"source_id": "nobox", "bbox_west": None, "bbox_south": None, "bbox_east": None, "bbox_north": None, "center_lat": 0, "center_lng": 0},
]


def test_city_at():
    assert city_at(CITIES, 1, 0.5)["source_id"] == "a"
    assert city_at(CITIES, 1, 3.5)["source_id"] == "b"
    # In both boxes: the nearer centre wins.
    assert city_at(CITIES, 1, 1.9)["source_id"] == "b"
    assert city_at(CITIES, 1, 1.6)["source_id"] == "a"
    assert city_at(CITIES, 5, 5) is None
    assert city_at(CITIES, 0, 0)["source_id"] == "a"  # edges are inside


def test_open_breaker_does_not_spend_the_budget():
    upstream = Upstream(raises=httpx.ConnectError("refused"))
    budget = DailyBudget(10, "transitous")
    client = TransitousClient(upstream.client(), budget=budget, breaker=CircuitBreaker(threshold=3))
    for _ in range(5):
        with pytest.raises(ProviderUnavailable):
            run(client.plan((39.95, -75.16), (39.94, -75.2), DEPART))
    assert len(upstream.requests) == 3
    assert budget.remaining() == 7  # three calls made, two refused for free


def test_transit_cache_is_not_shared_between_nearby_points():
    # 60 m apart: once one bucket, now two calls. A cached plan's walking legs
    # start at the first asker's exact point, so it must not reach the second.
    upstream = Upstream(fixture("motis_plan.json"))
    client = TransitousClient(upstream.client(), cache=TTLCache(8, 300))
    run(client.plan((39.95240, -75.1636), (39.9496, -75.2033), DEPART))
    run(client.plan((39.95294, -75.1636), (39.9496, -75.2033), DEPART))
    assert len(upstream.requests) == 2
    run(client.plan((39.95240, -75.1636), (39.9496, -75.2033), DEPART))
    assert len(upstream.requests) == 2


def test_transit_hooks_run_only_on_an_upstream_call():
    upstream = Upstream(fixture("motis_plan.json"))
    client = TransitousClient(upstream.client(), cache=TTLCache(8, 300))
    calls = []
    hooks = {"before_call": lambda: calls.append("before"), "after_failure": lambda: calls.append("failed")}
    run(client.plan((39.95, -75.16), (39.94, -75.2), DEPART, **hooks))
    run(client.plan((39.95, -75.16), (39.94, -75.2), DEPART, **hooks))  # cache hit
    assert calls == ["before"]
    failing = TransitousClient(Upstream({"error": "x"}, status=500).client())
    with pytest.raises(ProviderUnavailable):
        run(failing.plan((39.95, -75.16), (39.94, -75.2), DEPART, **hooks))
    assert calls == ["before", "before", "failed"]


def test_unexpected_error_on_the_half_open_trial_hands_it_back():
    clock = [0.0]
    breaker = CircuitBreaker(threshold=1, cooldown=10, clock=lambda: clock[0])
    breaker.record_failure()
    clock[0] = 11.0  # half open
    budget = DailyBudget(5, "transitous")
    upstream = Upstream(raises=RuntimeError("not an httpx error"))
    client = TransitousClient(upstream.client(), budget=budget, breaker=breaker)
    with pytest.raises(RuntimeError):
        run(client.plan((39.95, -75.16), (39.94, -75.2), DEPART))
    # The trial is free again and the charge refunded: the next call goes out.
    assert budget.remaining() == 5
    assert breaker.allow()
