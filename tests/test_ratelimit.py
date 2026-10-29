"""The in-process rate limiter (F4, F19): keyed on the proxy-resolved client
address, never on a client-supplied header, and bounded by LRU eviction."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from safety.api import ratelimit


def testclient_key_ignores_x_forwarded_for():
    scope = {
        "client": ("203.0.113.9", 1),
        "headers": [(b"x-forwarded-for", b"198.51.100.1")],
    }
    assert ratelimit.client_key(scope) == "203.0.113.9"


def testclient_key_without_client():
    assert ratelimit.client_key({"headers": []}) == "unknown"


def _limited_app() -> Starlette:
    async def cells(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/v1/cells", cells)])
    app.add_middleware(ratelimit.RateLimitMiddleware)
    return app


def test_spoofed_forwarded_for_does_not_buy_a_new_bucket():
    client = TestClient(_limited_app())
    for i in range(ratelimit._CELLS_BURST):
        r = client.get("/api/v1/cells", headers={"X-Forwarded-For": f"198.51.100.{i}"})
        assert r.status_code == 200, i
    r = client.get("/api/v1/cells", headers={"X-Forwarded-For": "198.51.100.250"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_eviction_drops_the_oldest_not_everything(monkeypatch):
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED_CLIENTS", 3)
    for i in range(5):
        ratelimit._consume((f"10.0.0.{i}", "api"), 10.0, 20)
    assert list(ratelimit._buckets) == [(f"10.0.0.{i}", "api") for i in (2, 3, 4)]


def test_recent_use_protects_from_eviction(monkeypatch):
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED_CLIENTS", 3)
    for i in range(3):
        ratelimit._consume((f"10.0.0.{i}", "api"), 10.0, 20)
    ratelimit._consume(("10.0.0.0", "api"), 10.0, 20)  # touch the oldest
    ratelimit._consume(("10.0.0.9", "api"), 10.0, 20)
    assert ("10.0.0.0", "api") in ratelimit._buckets
    assert ("10.0.0.1", "api") not in ratelimit._buckets


# ---------------------------------------------------------------- directions zones


def test_zone_for_each_path():
    zone = ratelimit._zone_for
    assert zone("/api/v1/cells")[0] == "cells"
    assert zone("/api/v1/directions") == ("directions", 0.5, 6)
    assert zone("/api/v1/geocode") == ("geocode", 3.0, 10)
    assert zone("/api/v1/geocode/reverse") == ("geocode", 3.0, 10)
    # Exact paths only, as nginx's `location =`: the status poll and anything
    # under these prefixes stay in the general zone.
    assert zone("/api/v1/directions/status")[0] == "api"
    assert zone("/api/v1/geocode/other")[0] == "api"
    assert zone("/index.html") is None


def _provider_app() -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/api/v1/directions", ok, methods=["POST"]),
            Route("/api/v1/directions/status", ok),
            Route("/api/v1/geocode", ok, methods=["POST"]),
            Route("/api/v1/geocode/reverse", ok, methods=["POST"]),
        ]
    )
    app.add_middleware(ratelimit.RateLimitMiddleware)
    return app


def test_directions_burst_then_429():
    client = TestClient(_provider_app())
    for i in range(ratelimit._DIRECTIONS_BURST):
        assert client.post("/api/v1/directions").status_code == 200, i
    r = client.post("/api/v1/directions")
    assert r.status_code == 429
    # 0.5 r/s: the next token is up to two seconds away.
    assert int(r.headers["retry-after"]) == 2
    # The status poll is in its own (general) zone and still answers.
    assert client.get("/api/v1/directions/status").status_code == 200


def test_geocode_and_reverse_share_one_bucket():
    client = TestClient(_provider_app())
    for i in range(ratelimit._GEOCODE_BURST):
        path = "/api/v1/geocode" if i % 2 else "/api/v1/geocode/reverse"
        assert client.post(path).status_code == 200, i
    assert client.post("/api/v1/geocode").status_code == 429
    assert client.post("/api/v1/geocode/reverse").status_code == 429
    # Spending geocode does not touch directions.
    assert client.post("/api/v1/directions").status_code == 200
