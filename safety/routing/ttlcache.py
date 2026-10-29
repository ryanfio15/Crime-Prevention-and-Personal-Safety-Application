"""In-memory TTL + LRU cache with async single flight, for provider answers.

The stamped cache in safety/api/main.py is the wrong tool here: it is keyed on
a city's ETL refresh stamp and holds serialized bytes, while a provider answer
goes stale by the clock (a 5-minute departure board, a 24 h geocode) and is
used as objects. Same eviction policy, though -- one least-recently-used entry
at a time, never a wholesale clear -- for the same reason: one busy key space
(say, a geocode autocomplete storm) must not wipe everyone else's entries.

Thread safety. Every read and write of the entries happens under a
threading.Lock, so get()/set() are safe from the threadpool as well as from the
event loop. get_or_create() is the async single flight and must be called on
the event loop.

Single flight. The first miss for a key starts the producer as its own task;
every concurrent caller for that key -- the first included -- awaits that task
through asyncio.shield(). Two consequences, both wanted: N identical misses
make one upstream call (the autocomplete burst, two tabs, a retry), and a
caller that goes away (the client disconnected) cancels only its own wait,
never the shared call the others are waiting on. A producer that raises is not
cached; everyone waiting gets the exception, and the next caller tries again.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar

T = TypeVar("T")

_MISSING = object()


class TTLCache:
    def __init__(
        self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self.maxsize = maxsize
        self.ttl = ttl
        self._clock = clock
        self._entries: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, asyncio.Task] = {}
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: Hashable) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def get(self, key: Hashable, default: Any = None) -> Any:
        """The live value for `key`, or `default`. An expired entry is dropped
        on the way past rather than by a sweeper: nothing here runs on a timer."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return default
            expires, value = entry
            if self._clock() >= expires:
                del self._entries[key]
                return default
            self._entries.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._entries.pop(key, None)
            while len(self._entries) >= self.maxsize:
                self._entries.popitem(last=False)
                self.stats["evictions"] += 1
            self._entries[key] = (self._clock() + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    async def get_or_create(self, key: Hashable, producer: Callable[[], Awaitable[T]]) -> T:
        """The cached value, or `producer()`'s, made at most once at a time per key.

        None is a value like any other (a reverse lookup that found nothing is
        worth remembering too); only an exception is not cached.
        """
        value = self.get(key, _MISSING)
        if value is not _MISSING:
            with self._lock:
                self.stats["hits"] += 1
            return value

        task = self._inflight.get(key)
        if task is None:
            with self._lock:
                self.stats["misses"] += 1
            task = asyncio.ensure_future(producer())
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._settle(k, t))
        else:
            with self._lock:
                self.stats["hits"] += 1
        return await asyncio.shield(task)

    def _settle(self, key: Hashable, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if task.cancelled():
            return
        # Reading the exception marks it retrieved: if every waiter has gone,
        # asyncio would otherwise log "exception was never retrieved" -- and
        # that log line would carry the exception's text.
        if task.exception() is None:
            self.set(key, task.result())
