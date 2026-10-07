"""The in-process rate limiter (F4, F19): keyed on the proxy-resolved client
address, never on a client-supplied header, and bounded by LRU eviction."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from safety.api import ratelimit


def test_client_key_ignores_x_forwarded_for():
    scope = {
        "client": ("203.0.113.9", 1),
        "headers": [(b"x-forwarded-for", b"198.51.100.1")],
    }
    assert ratelimit._client_key(scope) == "203.0.113.9"


def test_client_key_without_client():
    assert ratelimit._client_key({"headers": []}) == "unknown"


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
