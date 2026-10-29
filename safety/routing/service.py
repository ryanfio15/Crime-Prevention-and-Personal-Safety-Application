"""Routing: everything the directions endpoints share for the life of the process.

One instance per app, built in the API's lifespan around the shared
httpx.AsyncClient and kept on app.state. Everything stateful lives here -- the
provider caches, the daily budgets, the circuit breakers, the cities cache --
so a test builds a fresh one around an httpx.MockTransport and starts clean,
and nothing leaks between instances the way module globals would.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from safety.config import settings
from safety.routing.breaker import CircuitBreaker
from safety.routing.budget import DailyBudget
from safety.routing.errors import TooSoon
from safety.routing.providers import PhotonClient, TransitousClient, ValhallaClient
from safety.routing.ttlcache import TTLCache

# Provider cache sizes and lifetimes (plan S2). Sized by what one entry costs:
# a Valhalla answer with alternates is 20-200 KB of JSON, a MOTIS plan ~40 KB,
# a Photon answer a few KB.
PHOTON_TTL_S, PHOTON_ENTRIES = 24 * 3600, 4096
VALHALLA_TTL_S, VALHALLA_ENTRIES = 6 * 3600, 256
TRANSIT_TTL_S, TRANSIT_ENTRIES = 300, 256
# City bboxes and centres move only when a city is added or rebuilt.
CITIES_TTL_S = 300
# One transit plan per client per this many seconds (plan S2), on top of the
# directions rate-limit zone: the page keeps a result per mode, so switching
# tabs back to Transit does not ask again.
TRANSIT_CLIENT_INTERVAL_S = 20.0


class ClientThrottle:
    """At most one call per `interval` seconds per client key.

    Bounded like the rate limiter's buckets (safety/api/ratelimit.py): the
    least recently seen client is forgotten first, which can only ever let a
    forgotten client through early, never hold one back.
    """

    def __init__(
        self, interval: float, maxsize: int = 4096, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.interval = interval
        self.maxsize = maxsize
        self._clock = clock
        self._last: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record a call for `key`, or raise TooSoon without recording it."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.interval:
                raise TooSoon(retry_after=max(1, math.ceil(self.interval - (now - last))))
            self._last[key] = now
            self._last.move_to_end(key)
            while len(self._last) > self.maxsize:
                self._last.popitem(last=False)

    def forget(self, key: str) -> None:
        """Undo `key`'s last check(): the call it allowed did not happen or
        did not answer, so it should not hold the client back."""
        with self._lock:
            self._last.pop(key, None)


@dataclass
class Routing:
    http: httpx.AsyncClient
    valhalla: ValhallaClient
    transitous: TransitousClient
    photon: PhotonClient
    cities: TTLCache
    transit_throttle: ClientThrottle = field(
        default_factory=lambda: ClientThrottle(TRANSIT_CLIENT_INTERVAL_S)
    )

    @classmethod
    def build(cls, http: httpx.AsyncClient) -> Routing:
        return cls(
            http=http,
            valhalla=ValhallaClient(
                http,
                cache=TTLCache(VALHALLA_ENTRIES, VALHALLA_TTL_S),
                breaker=CircuitBreaker(),
            ),
            transitous=TransitousClient(
                http,
                cache=TTLCache(TRANSIT_ENTRIES, TRANSIT_TTL_S),
                budget=DailyBudget(settings.transit_daily_budget, "transitous"),
                breaker=CircuitBreaker(),
            ),
            photon=PhotonClient(
                http,
                cache=TTLCache(PHOTON_ENTRIES, PHOTON_TTL_S),
                budget=DailyBudget(settings.geocode_daily_budget, "photon"),
                breaker=CircuitBreaker(),
            ),
            cities=TTLCache(1, CITIES_TTL_S),
        )


def new_http_client() -> httpx.AsyncClient:
    """The one outbound client. No redirects (a provider cannot bounce us to
    a host settings did not name), no proxy from the environment, bounded
    connections."""
    return httpx.AsyncClient(
        timeout=settings.routing_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
    )
