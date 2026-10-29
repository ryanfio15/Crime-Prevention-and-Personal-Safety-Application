"""POST /api/v1/directions end to end through the app, with the providers on
httpx.MockTransport (the captured DC fixtures) and the repository calls
monkeypatched. Same harness shape as test_geocode_endpoint.py.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import h3
import httpx
import pytest
from fastapi.testclient import TestClient

from safety.api import main
from safety.config import settings
from safety.routing import scoring
from safety.routing.service import Routing

FIXTURES = Path(__file__).parent / "fixtures" / "routing"

CITIES = [
    {
        "source_id": "dc", "city_name": "Washington",
        "bbox_west": -77.12, "bbox_south": 38.79, "bbox_east": -76.91, "bbox_north": 39.0,
        "center_lat": 38.9072, "center_lng": -77.0369, "timezone": "America/New_York",
    },
    {
        "source_id": "phl", "city_name": "Philadelphia",
        "bbox_west": -75.28, "bbox_south": 39.86, "bbox_east": -74.95, "bbox_north": 40.14,
        "center_lat": 39.9526, "center_lng": -75.1652, "timezone": "America/New_York",
    },
]  # fmt: skip

WINDOWS = [{"id": "last_1y", "label": "Last 12 months", "safety": True, "hourly": True}]

# The two ends the Valhalla and MOTIS fixtures were captured between.
DUPONT = {"lat": 38.9096, "lng": -77.0434}
UNION_STATION = {"lat": 38.8973, "lng": -77.0063}
UNION_STATION_NEARBY = {"lat": 38.8978, "lng": -77.0063}
CITY_HALL_PHL = {"lat": 39.9526, "lng": -75.1652}


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakePool:
    @contextmanager
    def connection(self):
        yield object()


class Env:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.route_body = fixture("valhalla_route_alt.json")
        self.via_body = fixture("valhalla_route_pedestrian.json")
        self.plan_body = fixture("motis_plan_dc.json")
        self.plan_status = 200
        self.cells_asked: list[list[str]] = []
        # Every cell rated at the city average, unless a test says otherwise.
        self.cell_row = {"sm": 0.1, "tier": 2}
        # Each cell's day factor on every weekday; None for a city whose
        # factor (022) is not built.
        self.day_factor: float | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json={})
        if path == "/route":
            body = json.loads(request.content)
            return httpx.Response(
                200, json=self.route_body if len(body["locations"]) == 2 else self.via_body
            )
        if path == settings.transitous_plan_path:
            return httpx.Response(self.plan_status, json=self.plan_body)
        return httpx.Response(404)

    def upstream(self, path):
        return [r for r in self.requests if r.url.path == path]

    def route_cells(self, conn, source_id, h3_res, window, cells, hours, days=()):
        self.cells_asked.append(list(cells))
        if self.cell_row is None:
            return {t: {} for t in scoring.TRACKS}
        row = dict(self.cell_row)
        for h in hours:
            row[f"hsm.{h}"] = row["sm"] / 24
            row[f"htier.{h}"] = row["tier"]
        if self.day_factor is not None:
            for d in days:
                row[f"df.{d}"] = self.day_factor
        return {t: {c: dict(row) for c in cells} for t in scoring.TRACKS}

    def baseline(self, conn, source_id, h3_res, window, hourly, days=()):
        rows = [
            {"track": t, "hour_block": None, "dow": None, "weighted": 100.0, "people": 1000.0, "total": 0, "painted": 10}
            for t in scoring.TRACKS
        ]
        if hourly:
            rows += [
                {"track": t, "hour_block": h, "dow": None, "weighted": 100.0 / 24, "people": 1000.0, "total": 0, "painted": 10}
                for t in scoring.TRACKS
                for h in range(24)
            ]
        if hourly and self.day_factor is not None:
            # The city's average on each weekday stays the hour's: the factor
            # moves this cell against it.
            rows += [
                {"track": t, "hour_block": h, "dow": d, "weighted": 100.0 / 24, "people": 1000.0, "total": 0, "painted": 10}
                for t in scoring.TRACKS
                for h in range(24)
                for d in days
            ]
        return rows


@pytest.fixture
def env(monkeypatch):
    e = Env()
    monkeypatch.setattr(main, "pool", FakePool())
    monkeypatch.setattr(main.repo, "list_cities", lambda conn: [dict(c) for c in CITIES])
    monkeypatch.setattr(main, "city_windows", lambda conn, source_id: WINDOWS)
    monkeypatch.setattr(main, "cached_json", lambda conn, key, producer, source_id: producer())
    monkeypatch.setattr(main.repo, "route_baseline", e.baseline)
    monkeypatch.setattr(main.repo, "route_cells", e.route_cells)
    http = httpx.AsyncClient(transport=httpx.MockTransport(e.handler))
    main.app.state.routing = Routing.build(http)
    return e


def post(body):
    return TestClient(main.app).post("/api/v1/directions", json=body)


def trip(mode, a=DUPONT, b=UNION_STATION, **extra):
    return post({"from": a, "to": b, "mode": mode, **extra})


# ---------------------------------------------------------------- shape


def test_drive_response_shape(env):
    r = trip("drive")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["city"] == "dc" and body["mode"] == "drive"
    assert body["window"] == {"id": "last_1y", "label": "Last 12 months"}
    assert body["hourly"] is True
    routes = body["routes"]
    assert len(routes) == 3
    durations = [x["duration_s"] for x in routes]
    assert durations == sorted(durations)
    for x in routes:
        assert x["geometry"]["type"] == "LineString" and x["steps"]
        for track in scoring.TRACKS:
            a = x["activity"][track]
            # Every cell at the city average: the route is at the average.
            assert a["comparable"] and a["mean_ratio"] == pytest.approx(1.0)
            assert a["sense"] == "about"
            assert a["segments"] and a["segments"][0]["coordinates"]
    assert set(body["picks"]) == set(scoring.TRACKS)
    # Identical activity everywhere: nothing is lower.
    assert body["picks"]["violent"]["lower"] is None
    assert body["picks"]["violent"]["why"] == "not_lower"
    assert body["caveats"] and body["attribution"]["transit_sources_url"]
    # One Valhalla call, with alternates; no depart time alongside them.
    (call,) = env.upstream("/route")
    sent = json.loads(call.content)
    assert sent["alternates"] == 2 and "date_time" not in sent


def test_one_batch_of_cells_for_all_routes(env):
    trip("drive")
    assert len(env.cells_asked) == 1
    assert all(h3.get_resolution(c) == scoring.SCORE_RES for c in env.cells_asked[0])


def test_walk_adds_detours_when_alternates_are_not_distinct(env):
    # The pedestrian fixture has one trip and no alternates.
    env.route_body = fixture("valhalla_route_pedestrian.json")
    r = trip("walk")
    assert r.status_code == 200, r.text
    calls = env.upstream("/route")
    assert len(calls) <= 1 + 3
    vias = [json.loads(c.content) for c in calls[1:]]
    assert vias and all(len(v["locations"]) == 3 for v in vias)
    # The detours came back as the same route, so only one is offered.
    assert len(r.json()["routes"]) == 1
    assert r.json()["picks"]["violent"]["why"] == "single_route"


def test_transit(env):
    r = trip("transit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["routes"] and all(x["legs"] for x in body["routes"])
    assert any("time on board" in c for c in body["caveats"])
    (call,) = env.upstream(settings.transitous_plan_path)
    assert call.url.params["time"].endswith("Z")


def test_transit_is_one_per_client_per_interval(env):
    assert trip("transit").status_code == 200
    again = trip("transit", b=UNION_STATION_NEARBY)
    assert again.status_code == 429
    assert again.json()["code"] == "too_soon"
    assert 1 <= int(again.headers["retry-after"]) <= 20
    # The street modes are not held back by it.
    assert trip("walk").status_code == 200


def test_transit_throttle_is_not_charged_for_a_cache_hit(env):
    assert trip("transit").status_code == 200
    # The same trip again (a swap back, a tab switch): no call, no wait.
    assert trip("transit").status_code == 200
    assert len(env.upstream(settings.transitous_plan_path)) == 1


def test_transit_throttle_is_not_charged_for_a_failed_plan(env):
    env.plan_status = 500
    assert trip("transit").status_code == 503
    env.plan_status = 200
    assert trip("transit").status_code == 200


def test_hourly_rows_are_fetched_for_each_new_hour(env):
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from safety.api import directions

    scorer = directions._Scorer(
        "dc", WINDOWS[0], True, {}, datetime(2026, 11, 2, 17, 50, tzinfo=ZoneInfo("America/New_York"))
    )
    cell = h3.latlng_to_cell(*DUPONT.values(), scoring.SCORE_RES)

    async def both():
        await scorer.load({cell}, {(17, 0)})
        await scorer.load({cell}, {(18, 0)})

    asyncio.run(both())
    assert env.cells_asked == [[cell], [cell]]
    row = scorer.cells["violent"][cell]
    assert "htier.17" in row and "htier.18" in row and "tier" in row


# ---------------------------------------------------------------- refusals


def test_points_in_different_cities(env):
    r = trip("walk", b=CITY_HALL_PHL)
    assert r.status_code == 422
    assert r.json()["code"] == "different_cities"
    assert r.json()["cities"] == ["dc", "phl"]
    assert env.requests == []


def test_point_outside_every_city(env):
    r = trip("walk", b={"lat": 40.7, "lng": -74.0})
    assert r.status_code == 422 and r.json()["code"] == "outside"


def test_distance_cap(env, monkeypatch):
    monkeypatch.setattr(settings, "route_max_km", 1.0)
    r = trip("drive")
    assert r.status_code == 422 and r.json()["code"] == "too_far"
    assert env.requests == []


def test_kill_switch(env, monkeypatch):
    monkeypatch.setattr(settings, "directions_enabled", False)
    assert trip("drive").status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"from": DUPONT, "to": UNION_STATION, "mode": "fly"},
        {"from": DUPONT, "to": UNION_STATION, "mode": "walk", "extra": 1},
        {"from": {"lat": 91, "lng": 0}, "to": UNION_STATION, "mode": "walk"},
        {"from": DUPONT, "to": UNION_STATION, "mode": "walk", "depart_at": "tomorrow"},
        {"from": DUPONT, "to": UNION_STATION, "mode": "walk", "window": "x'; drop"},
    ],
)
def test_bad_requests(env, body):
    assert post(body).status_code == 422


def test_departure_bounds(env):
    assert trip("walk", depart_at="2020-01-01T10:00").status_code == 422


def test_unrated_route_claims_no_comparison(env):
    env.cell_row = None
    body = trip("drive").json()
    a = body["routes"][0]["activity"]["violent"]
    assert not a["comparable"] and a["mean_ratio"] is None and a["rated_share"] == 0
    assert body["picks"]["violent"]["why"] == "not_rated"


def test_logs_carry_no_coordinates(env, caplog):
    caplog.set_level(logging.DEBUG)
    trip("drive")
    trip("transit")
    text = caplog.text
    for coordinate in ("38.9096", "-77.0434", "38.8973", "-77.0063", "38.909", "77.04"):
        assert coordinate not in text
    assert "directions drive dc" in text


def test_day_factor_moves_the_route_against_the_city(env):
    # Every cell at the city average by hour; a weekday factor of 1.5 puts the
    # whole route half as much again above it, and the response says the day
    # was used.
    plain = trip("walk").json()
    assert plain["daily"] is False
    env.day_factor = 1.5
    body = trip("walk").json()
    assert body["daily"] is True
    for before, after in zip(plain["routes"], body["routes"]):
        a, b = before["activity"]["violent"], after["activity"]["violent"]
        assert b["mean_ratio"] == pytest.approx(a["mean_ratio"] * 1.5, rel=1e-3)
