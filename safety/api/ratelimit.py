"""Per-client request limiting for the public API.

This is the second of two layers. On the host deployment, nginx's `limit_req`
zones are the first: the prod and dev sites (`/etc/nginx/sites-available/safety`
and `deploy/nginx/safety-dev.conf`) apply the same two zones below per remote
address before a request reaches uvicorn. This in-process copy exists because
the API has no authentication and `GET /api/v1/cells` assembles the whole
city's hexagon layer as one GeoJSON document (`cells()` in safety/api/main.py),
so the control has to hold wherever the app runs -- including behind a proxy we
do not configure (the Docker image, below), or if a request reaches uvicorn
without passing through nginx's zones.

The response cache in front of `/cells` (`cached()` in main.py, an LRU bounded
by entry count and a byte budget) absorbs repeated *identical* requests, but
`bbox` and `min_count` are free-form query parameters, so walking them still
reaches the database. The limit is what bounds that.

Two zones, because the endpoints are not equally expensive:

    GET /api/v1/cells   2 r/s, burst 10   the full-city layer document
    /api/v1/ (rest)     10 r/s, burst 20  cheap, cached, ~5 per page load

The split is by exact path first, then prefix, which mirrors how nginx chooses
between `location = /api/v1/cells` and `location /api/v1/`: `/cells/ring` and
`/cells/lookup` land in the loose zone rather than the tight one.

The frontend refetches the layer only on a window, category or resolution change
and never on pan or zoom, so a person working the controls cannot reach the
tight ceiling; burst 10 covers clicking through every category in one go.
Static assets and `/docs` are not limited at all.

Who the client is. Buckets are keyed on `scope["client"]`, the address uvicorn
resolved, never on a header. On the host, uvicorn runs with
`--proxy-headers --forwarded-allow-ips=127.0.0.1`
(deploy/systemd/safety-api@.service): nginx appends its peer to
X-Forwarded-For, and uvicorn walks that list from the right, skipping trusted
hops, so `scope["client"]` is nginx's real `$remote_addr`. The Docker image
(Dockerfile) instead trusts `${FORWARDED_ALLOW_IPS:-*}`; with `*` uvicorn takes
the first X-Forwarded-For element, so the per-client limit there is only as
trustworthy as the platform proxy's guarantee that it overwrites the header.
Set FORWARDED_ALLOW_IPS to the platform proxy's address wherever it is known.

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
from collections import OrderedDict
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Tokens accrue at `rate` per second up to `capacity`. A full bucket is the
# burst allowance; steady-state throughput is `rate`.
_CELLS_RATE, _CELLS_BURST = 2.0, 10
_API_RATE, _API_BURST = 10.0, 20

_CELLS_PATH = "/api/v1/cells"
_API_PREFIX = "/api/v1/"

# Bucket state keyed by (client, zone), least recently used first. Bounded by
# evicting the oldest entries one at a time: clearing the whole dict when it
# filled up handed every client -- including one mid-burst -- a fresh bucket, so
# anyone able to present 4096 addresses could reset everyone's limit at will.
# An evicted client is one that has not been seen for longest, so it would have
# refilled most of its bucket anyway.
#
# No lock: the middleware runs only on the event loop thread, and nothing
# between a read and the write that follows it awaits.
_buckets: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()
_MAX_TRACKED_CLIENTS = 4096


def _client_key(scope: Scope) -> str:
    """The caller's address as uvicorn resolved it.

    nginx appends its view of the peer to X-Forwarded-For, and uvicorn
    (--proxy-headers --forwarded-allow-ips=127.0.0.1) walks that list from
    the right, skipping trusted hops, and writes the first untrusted address
    into scope["client"]. The *first* XFF element is whatever the client
    sent, so keying on it let anyone spend another address's bucket.
    """
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
        wait = 0.0
    else:
        _buckets[key] = (tokens, now)
        wait = (1.0 - tokens) / rate

    _buckets.move_to_end(key)
    while len(_buckets) > _MAX_TRACKED_CLIENTS:
        _buckets.popitem(last=False)
    return wait


class RateLimitMiddleware:
    """ASGI middleware applying the zones above.

    Written against the raw ASGI interface rather than BaseHTTPMiddleware: the
    check is a dict lookup and some arithmetic, and it runs on every request
    under /api/v1/, the health check included.
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
