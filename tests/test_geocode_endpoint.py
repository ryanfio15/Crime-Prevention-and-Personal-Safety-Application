"""POST /api/v1/geocode, POST /api/v1/geocode/reverse and GET
/api/v1/directions/status, end to end through the app with the providers on
httpx.MockTransport and the two repository calls monkeypatched.

TestClient(app) without `with` (conftest): the lifespan is not run, so each
test installs its own app.state.routing and a fake pool whose connection()
yields a dummy -- the endpoints borrow a connection only around repo calls.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from safety.api import main
from safety.config import settings
from safety.h3grid import cell_for
from safety.routing.budget import DailyBudget
from safety.routing.service import Routing

FIXTURES = Path(__file__).parent / "fixtures" / "routing"

CITIES = [
    {
        "source_id": "phl", "city_name": "Philadelphia",
        "bbox_west": -75.28, "bbox_south": 39.86, "bbox_east": -74.95, "bbox_north": 40.14,
        "center_lat": 39.9526, "center_lng": -75.1652, "timezone": "America/New_York",
    },
    {
        "source_id": "sea", "city_name": "Seattle",
        "bbox_west": -122.46, "bbox_south": 47.49, "bbox_east": -122.22, "bbox_north": 47.74,
        "center_lat": 47.6062, "center_lng": -122.3321, "timezone": "America/Los_Angeles",
    },
]  # fmt: skip

# Mercer Island: inside Seattle's bbox, outside its served cells (R3).
MERCER = (47.5707, -122.2221)
PIKE_PLACE = (47.6097, -122.3422)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakePool:
    def __init__(self):
        self.borrowed = 0

    @contextmanager
    def connection(self):
        self.borrowed += 1
        yield object()


class Env:
    """The upstream double and the knobs a test turns."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.search_body = fixture("photon_search.json")
        self.reverse_body = fixture("photon_reverse.json")
        self.photon_status = 200
        self.valhalla_up = True
        self.served: set[str] | None = None  # None: every cell is served
        self.cells_asked: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/status":
            if not self.valhalla_up:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"version": "3.9.1"})
        if path in ("/api", "/reverse"):
            if self.photon_status != 200:
                return httpx.Response(
                    self.photon_status, text="upstream stack trace: secret-internal-host"
                )
            return httpx.Response(200, json=self.search_body if path == "/api" else self.reverse_body)
        return httpx.Response(404)

    def upstream(self, path):
        return [r for r in self.requests if r.url.path == path]

    def cells_exist(self, conn, source_id, cells):
        self.cells_asked.append(list(cells))
        return set(cells) if self.served is None else set(cells) & self.served


@pytest.fixture
def env(monkeypatch):
    e = Env()
    pool = FakePool()
    monkeypatch.setattr(main, "pool", pool)
    monkeypatch.setattr(main.repo, "list_cities", lambda conn: [dict(c) for c in CITIES])
    monkeypatch.setattr(main.repo, "cells_exist", e.cells_exist)
    http = httpx.AsyncClient(transport=httpx.MockTransport(e.handler))
    main.app.state.routing = Routing.build(http)
    e.pool = pool
    e.routing = main.app.state.routing
    return e


def client():
    return TestClient(main.app)


def geocode(body):
    return client().post("/api/v1/geocode", json=body)


# ---------------------------------------------------------------- geocode


def test_geocode_returns_labelled_places(env):
    r = geocode({"q": "Dilworth Park", "city": "phl", "limit": 5})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["city"] == "phl"
    assert "Photon" in body["attribution"] and "OpenStreetMap" in body["attribution"]
    (result,) = body["results"]
    assert result["title"] == "Dilworth Park Café"
    assert result["subtitle"] == "1400 John F. Kennedy Boulevard, Philadelphia, PA 19107"
    assert result["lat"] == pytest.approx(39.953206)
    assert result["lng"] == pytest.approx(-75.164376)


def test_geocode_sends_the_city_bbox_and_centre(env):
    geocode({"q": "Dilworth Park", "city": "phl", "limit": 3})
    (request,) = env.upstream("/api")
    params = request.url.params
    assert params["bbox"] == "-75.280000,39.860000,-74.950000,40.140000"
    assert (params["lat"], params["lon"]) == ("39.95260", "-75.16520")
    assert params["limit"] == "6"
    assert params["exclude"] == "osm.highway.bus_stop"
    assert request.url.host == httpx.URL(settings.photon_url).host


def test_connection_is_borrowed_only_for_queries(env):
    geocode({"q": "Dilworth Park", "city": "phl"})
    # list_cities (once; cached afterwards) and cells_exist.
    assert env.pool.borrowed == 2
    geocode({"q": "Dilworth Park", "city": "phl"})
    assert env.pool.borrowed == 3


def test_second_identical_query_is_a_cache_hit(env):
    geocode({"q": "Dilworth Park", "city": "phl"})
    r = geocode({"q": "  dilworth   park", "city": "phl"})
    assert r.status_code == 200
    assert len(env.upstream("/api")) == 1
    assert r.json()["results"][0]["title"] == "Dilworth Park Café"


def test_results_outside_the_bbox_are_dropped(env):
    env.search_body = fixture("photon_search_unbounded.json")
    r = geocode({"q": "JFK Blvd", "city": "phl", "limit": 6})
    assert [p["title"] for p in r.json()["results"]] == ["Dilworth Park Café"]


def test_results_outside_served_cells_are_dropped(env):
    # Mercer Island is inside Seattle's bbox; only Pike Place's cell is served.
    env.served = {cell_for(*PIKE_PLACE, 8)}
    env.search_body = {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {"type": "house", "name": "Mercer Island Library", "city": "Mercer Island", "state": "WA"},
                "geometry": {"coordinates": [MERCER[1], MERCER[0]]},
            },
            {
                "properties": {"type": "house", "name": "Pike Place Market", "city": "Seattle", "state": "WA"},
                "geometry": {"coordinates": [PIKE_PLACE[1], PIKE_PLACE[0]]},
            },
        ],
    }
    r = geocode({"q": "library market", "city": "sea"})
    assert [p["title"] for p in r.json()["results"]] == ["Pike Place Market"]
    assert sorted(env.cells_asked[0]) == sorted({cell_for(*MERCER, 8), cell_for(*PIKE_PLACE, 8)})


def test_results_are_truncated_to_the_limit(env):
    env.search_body = fixture("photon_search_partial.json")
    r = geocode({"q": "Market", "city": "phl", "limit": 2})
    assert len(r.json()["results"]) == 2


def test_control_characters_are_stripped_before_the_call(env):
    r = geocode({"q": "Dilworth\x00\x07  Park\n", "city": "phl"})
    assert r.status_code == 200
    assert env.upstream("/api")[0].url.params["q"] == "Dilworth Park"


@pytest.mark.parametrize(
    "body",
    [
        {"q": "ab", "city": "phl"},
        {"q": "\x00\x01ab  ", "city": "phl"},
        {"q": "x" * 121, "city": "phl"},
        {"q": "x" * 501, "city": "phl"},
        {"q": "Market", "city": "phl", "limit": 0},
        {"q": "Market", "city": "phl", "limit": 7},
        {"q": "Market", "city": "PHL; DROP"},
        {"q": "Market", "city": "phl", "extra": 1},
        {"q": "Market"},
        {"city": "phl"},
    ],
)
def test_geocode_validation(env, body):
    r = geocode(body)
    assert r.status_code == 422
    assert env.requests == []


def test_unknown_city_is_rejected_without_an_upstream_call(env):
    r = geocode({"q": "Market", "city": "zzz"})
    assert r.status_code == 422
    assert env.requests == []


def test_geocode_is_post_only(env):
    # A GET falls through to the static mount (404): a query string, which
    # access logs record, never reaches Photon.
    assert client().get("/api/v1/geocode?q=Market&city=phl").status_code in (404, 405)
    assert env.requests == []


def test_upstream_failure_is_503_without_the_providers_body(env):
    env.photon_status = 500
    r = geocode({"q": "Dilworth Park", "city": "phl"})
    assert r.status_code == 503
    assert r.json()["code"] == "provider_unavailable"
    assert "secret-internal-host" not in r.text
    assert r.headers["retry-after"] == "30"
    assert r.headers["cache-control"] == "no-store"


def test_spent_budget_is_503_with_the_reset_time(env):
    env.routing.photon.budget = DailyBudget(0, "photon")
    r = geocode({"q": "Dilworth Park", "city": "phl"})
    assert r.status_code == 503
    assert r.json()["code"] == "budget_exhausted"
    assert int(r.headers["retry-after"]) >= 1
    assert env.requests == []


# ---------------------------------------------------------------- reverse


def test_reverse_labels_the_visitors_own_point(env):
    r = client().post("/api/v1/geocode/reverse", json={"lat": 39.94946, "lng": -75.15028})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["city"] == "phl"
    assert body["result"]["label"] == "Liberty Bell, 526 Market Street, Philadelphia, PA 19106"
    # The point clicked, not the feature's: a route starts where they are.
    assert (body["result"]["lat"], body["result"]["lng"]) == (39.94946, -75.15028)
    params = env.upstream("/reverse")[0].url.params
    assert params.get_list("layer") == ["house", "street"]


def test_reverse_with_nothing_nearby_falls_back_to_coordinates(env):
    env.reverse_body = {"type": "FeatureCollection", "features": []}
    r = client().post("/api/v1/geocode/reverse", json={"lat": 39.95, "lng": -75.16})
    assert r.json()["result"]["label"] == "39.95000, -75.16000"
    assert r.json()["result"]["kind"] == "point"


def test_reverse_outside_every_city_does_not_call_photon(env):
    r = client().post("/api/v1/geocode/reverse", json={"lat": 51.5, "lng": -0.12})
    assert r.status_code == 200
    assert r.json()["city"] is None
    assert r.json()["result"]["label"] == "51.50000, -0.12000"
    assert env.requests == []


@pytest.mark.parametrize(
    "body",
    [{"lat": 91, "lng": 0}, {"lat": 0, "lng": -181}, {"lat": "NaN", "lng": 0}, {"lat": 1}, {"lat": 1, "lng": 1, "q": "x"}],
)
def test_reverse_validation(env, body):
    assert client().post("/api/v1/geocode/reverse", json=body).status_code == 422
    assert env.requests == []


# ---------------------------------------------------------------- privacy


def test_logs_carry_no_query_text_or_coordinates(env, caplog):
    caplog.set_level(logging.DEBUG)
    geocode({"q": "Dilworth Park", "city": "phl"})
    client().post("/api/v1/geocode/reverse", json={"lat": 39.94946, "lng": -75.15028})
    env.photon_status = 500
    env.routing.photon.cache.clear()
    geocode({"q": "Dilworth Park", "city": "phl"})
    text = caplog.text
    assert "geocode phl" in text  # something was logged...
    for secret in ("Dilworth", "dilworth", "39.94", "75.15", "39.95", "75.16", "secret-internal-host"):
        assert secret not in text, secret  # ...but not what was asked or where


# ---------------------------------------------------------------- kill switch


def test_kill_switch_turns_every_endpoint_off(env, monkeypatch):
    monkeypatch.setattr(settings, "directions_enabled", False)
    c = client()
    responses = [
        c.post("/api/v1/geocode", json={"q": "Dilworth Park", "city": "phl"}),
        c.post("/api/v1/geocode/reverse", json={"lat": 39.95, "lng": -75.16}),
        c.get("/api/v1/directions/status"),
    ]
    assert [r.status_code for r in responses] == [503, 503, 503]
    assert all("switched off" in r.json()["detail"] for r in responses)
    assert env.requests == []


# ---------------------------------------------------------------- status


def test_status_with_everything_up(env):
    r = client().get("/api/v1/directions/status")
    assert r.status_code == 200
    body = r.json()
    assert all(body["modes"][m]["available"] for m in ("drive", "walk", "bike", "transit"))
    assert body["geocode"]["available"] is True
    assert body["transit_budget_left"] == settings.transit_daily_budget
    assert body["attribution"]["transit_sources_url"] == "https://transitous.org/sources"


def test_status_with_valhalla_down(env):
    env.valhalla_up = False
    body = client().get("/api/v1/directions/status").json()
    for m in ("drive", "walk", "bike"):
        assert body["modes"][m] == {"available": False, "reason": "unavailable"}
    assert body["modes"]["transit"]["available"] is True
    assert body["geocode"]["available"] is True


def test_status_probe_is_cached(env):
    c = client()
    c.get("/api/v1/directions/status")
    c.get("/api/v1/directions/status")
    assert len(env.upstream("/status")) == 1
    # Transitous and Photon are never probed: it would spend their budgets.
    assert len(env.requests) == 1


def test_status_reports_a_spent_transit_budget(env):
    env.routing.transitous.budget = DailyBudget(0, "transitous")
    body = client().get("/api/v1/directions/status").json()
    assert body["modes"]["transit"] == {"available": False, "reason": "budget"}
    assert body["transit_budget_left"] == 0


def test_status_reports_an_open_breaker(env):
    for _ in range(3):
        env.routing.photon.breaker.record_failure()
    body = client().get("/api/v1/directions/status").json()
    assert body["geocode"] == {"available": False, "reason": "unavailable"}
