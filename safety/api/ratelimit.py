"""Per-client request limiting for the public API.

This replaces the nginx `limit_req` zones the self-hosted deployment used. On a
platform host there is no proxy layer of our own to configure, and the control
still has to exist somewhere: the API has no authentication, and
`GET /api/v1/cells` assembles the whole city's hexagon layer as one GeoJSON
document (`safety/api/main.py:184`). The in-process cache in front of it
(`main.py:91-113`) absorbs repeated *identical* requests, but `bbox` and
`min_count` are free-form query parameters and the cache holds 256 entries
before clearing, so walking those parameters defeats it and puts every request
back on the database.

Two zones, because the endpoints are not equally expensive:

    GET /api/v1/cells   2 r/s, burst 10   the full-city layer document
    /api/v1/ (rest)     10 r/s, burst 20  cheap, cached, ~5 per page load

The split is by exact path first, then prefix, which mirrors how nginx chose
between `location = /api/v1/cells` and `location /api/v1/`: `/cells/ring` and
`/cells/lookup` land in the loose zone rather than the tight one.

The frontend refetches the layer only on a window, category or resolution change
(`web/app.js:693-707`) and never on pan or zoom, so a person working the
controls cannot reach the tight ceiling; burst 10 covers clicking through every
category in one go. Static assets and `/docs` are not limited at all.

Deliberately not carried over: nginx's `limit_conn` (20 concurrent sockets per
address). Connection-level limiting belongs in a proxy that owns the socket, not
in the application behind it.

State is per-process. That is correct for the single-instance topology this runs
in -- the cache in `main.py` has exactly the same constraint -- but running more
than one replica would give each its own buckets and multiply the effective
limit.
"""

from __future__ import annotations

import math
import time
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Tokens accrue at `rate` per second up to `capacity`. A full bucket is the
# burst allowance; steady-state throughput is `rate`.
_CELLS_RATE, _CELLS_BURST = 2.0, 10
_API_RATE, _API_BURST = 10.0, 20

_CELLS_PATH = "/api/v1/cells"
_API_PREFIX = "/api/v1/"

# Bucket state keyed by (client, zone). Cleared wholesale rather than evicted
# entry by entry, matching the cache in main.py -- a clear hands everyone a full
# bucket, which errs toward letting traffic through rather than blocking it.
_buckets: dict[tuple[str, str], tuple[float, float]] = {}
_MAX_TRACKED_CLIENTS = 4096


def _client_key(scope: Scope) -> str:
    """Identify the caller, preferring the proxy's view of the origin address.

    Only trustworthy because the container is reachable only through the
    platform's proxy and uvicorn runs with `--proxy-headers`; a directly
    reachable process could be handed any value here.
    """
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            first = value.decode("latin-1").split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    return client[0] if client else "unknown"


def _zone_for(path: str) -> tuple[str, float, int] | None:
    """Return (name, rate, capacity), or None when the path is not limited."""
    if path == _CELLS_PATH:
        return ("cells", _CELLS_RATE, _CELLS_BURST)
    if path.startswith(_API_PREFIX):
        return ("api", _API_RATE, _API_BURST)
    return None


def _consume(key: tuple[str, str], rate: float, capacity: int) -> float:
    """Take one token. Returns 0.0 if allowed, else seconds until the next one."""
    now = time.monotonic()
    tokens, last = _buckets.get(key, (float(capacity), now))

    tokens = min(float(capacity), tokens + (now - last) * rate)
    if tokens >= 1.0:
        _buckets[key] = (tokens - 1.0, now)
        return 0.0

    _buckets[key] = (tokens, now)
    return (1.0 - tokens) / rate


class RateLimitMiddleware:
    """ASGI middleware applying the zones above.

    Written against the raw ASGI interface rather than BaseHTTPMiddleware: the
    check is a dict lookup and some arithmetic, and it runs on every request
    including the platform's health probe.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        zone = _zone_for(scope.get("path", ""))
        if zone is None:
            await self.app(scope, receive, send)
            return

        name, rate, capacity = zone

        if len(_buckets) >= _MAX_TRACKED_CLIENTS:
            _buckets.clear()

        wait = _consume((_client_key(scope), name), rate, capacity)
        if wait > 0.0:
            retry_after = max(1, math.ceil(wait))
            response: Any = JSONResponse(
                {
                    "detail": (
                        "Too many requests. This endpoint is rate limited to protect "
                        "the service; retry shortly."
                    )
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
