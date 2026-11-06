"""How far a city's first load has got, for the progress bar on the live site.

The table is etl.load_progress (migration 024), one row per city. A load is
recorded from its first fetch to the gold refresh that makes the city servable:

    fetch (chunk n of N) -> promote -> census -> gold (layer n of N) -> done

Writes go over their own autocommit connection, never the caller's. The fetch
loop holds no transaction, but the gold refresh is one long transaction, and a
progress row written inside it would only become visible when the whole refresh
commits. That is too late to be worth showing.

Only a load writes a row: a backfill, or an incremental with no watermark yet.
`phase` and `step` update a row only while its load is unfinished, so the
scheduled incrementals and refreshes that follow never touch it. Progress is
best effort throughout: a failure to record it is logged and the load carries
on regardless.

`summarize` is the arithmetic the API serves (GET /api/v1/progress). It lives
here, next to the phase weights it depends on.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import psycopg

log = logging.getLogger(__name__)

# Phases in order, with the share of a city's load each one accounts for.
# Fetching from the city's portal is most of the wall-clock time; promotion,
# the census download and the gold build share the rest.
PHASES = ("fetch", "promote", "census", "gold")
WEIGHTS = {"fetch": 0.60, "promote": 0.10, "census": 0.10, "gold": 0.20}
# How far into its share an indeterminate phase (no total) is drawn.
INDETERMINATE = 0.5
# No estimate until this share is done and this long has passed: the first
# chunks are too noisy to extrapolate from.
ETA_MIN_SHARE = 0.03
ETA_MIN_SECONDS = 60.0
# A city that finished within this long before another began is part of the
# same load (summarize).
CHAIN_GAP = timedelta(minutes=30)

_conn: psycopg.Connection | None = None
# After a failed write, stay quiet for a while rather than paying a connect
# timeout on every chunk of a load whose database cannot take the row.
_retry_after = 0.0
RETRY_SECONDS = 60.0


def _writer() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        from safety.config import settings

        _conn = psycopg.connect(settings.dsn, autocommit=True)
    return _conn


def _execute(query: str, params: dict[str, Any]) -> None:
    global _conn, _retry_after
    if time.monotonic() < _retry_after:
        return
    try:
        _writer().execute(query, params)
    except Exception as exc:  # never let the bar break the load
        _retry_after = time.monotonic() + RETRY_SECONDS
        log.warning("could not record load progress: %s", exc)
        try:
            if _conn is not None:
                _conn.close()
        except Exception:
            pass
        _conn = None


def begin(source_id: str, total: int | None) -> None:
    """A load starts fetching: reset the city's row."""
    _execute(
        """
        INSERT INTO etl.load_progress
            (source_id, phase, phase_done, phase_total, load_started_at,
             phase_started_at, updated_at, finished_at)
        VALUES (%(s)s, 'fetch', 0, %(total)s, now(), now(), now(), NULL)
        ON CONFLICT (source_id) DO UPDATE SET
            phase = 'fetch', phase_done = 0, phase_total = EXCLUDED.phase_total,
            load_started_at = now(), phase_started_at = now(), updated_at = now(),
            finished_at = NULL
        """,
        {"s": source_id, "total": total},
    )


def phase(conn: Any, source_id: str, name: str, total: int | None = None) -> None:
    """Move an unfinished load to the next phase. Does nothing outside a load.

    `conn` is accepted for call-site symmetry with the rest of the ETL and is
    not used: the write goes over this module's own connection.
    """
    _execute(
        """
        UPDATE etl.load_progress SET
            phase = %(phase)s, phase_done = 0, phase_total = %(total)s,
            phase_started_at = now(), updated_at = now()
        WHERE source_id = %(s)s AND finished_at IS NULL
        """,
        {"s": source_id, "phase": name, "total": total},
    )


def step(conn: Any, source_id: str, done: int) -> None:
    """Record `done` units of the current phase. Does nothing outside a load."""
    _execute(
        """
        UPDATE etl.load_progress SET phase_done = %(done)s, updated_at = now()
        WHERE source_id = %(s)s AND finished_at IS NULL
        """,
        {"s": source_id, "done": max(int(done), 0)},
    )


def finish(conn: Any, source_id: str) -> None:
    """The city is servable: close its load."""
    _execute(
        """
        UPDATE etl.load_progress SET
            phase = 'done', phase_done = 0, phase_total = NULL,
            updated_at = now(), finished_at = now()
        WHERE source_id = %(s)s AND finished_at IS NULL
        """,
        {"s": source_id},
    )


def fail(source_id: str) -> None:
    """The load stopped with an error; the next attempt calls begin() again."""
    _execute(
        """
        UPDATE etl.load_progress SET phase = 'failed', updated_at = now()
        WHERE source_id = %(s)s AND finished_at IS NULL
        """,
        {"s": source_id},
    )


# ---------------------------------------------------------------------------
# Reading it back (the API)
# ---------------------------------------------------------------------------

# One row per enabled city on a database: whether it is served, and its load row.
STATE_SQL = """
SELECT r.source_id, r.city_name,
       (g.source_id IS NOT NULL) AS served,
       p.phase, p.phase_done, p.phase_total,
       p.load_started_at, p.phase_started_at, p.updated_at, p.finished_at,
       now() AS now
FROM reference.source_registry r
LEFT JOIN gold.city_snapshot g ON g.source_id = r.source_id
LEFT JOIN etl.load_progress p ON p.source_id = r.source_id
WHERE r.enabled
ORDER BY r.source_id
"""


def city_fraction(row: dict[str, Any]) -> float:
    """How much of one city's load is done, 0..1."""
    if row.get("finished_at") is not None or row.get("phase") == "done":
        return 1.0
    name = row.get("phase")
    if name is None:
        # No load recorded: served from before progress was tracked, or queued.
        return 1.0 if row.get("served") else 0.0
    if name == "failed":
        name = "fetch"
    base = 0.0
    for earlier in PHASES:
        if earlier == name:
            break
        base += WEIGHTS[earlier]
    weight = WEIGHTS.get(name, 0.0)
    total = row.get("phase_total")
    done = row.get("phase_done") or 0
    if total:
        within = min(done / total, 0.99)
    else:
        within = INDETERMINATE if done else 0.0
    return min(base + weight * within, 0.999)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The bar's numbers across every database's cities.

    A city is part of the load in progress when it is not served yet, when its
    load is unfinished, or when it finished during the current load. Cities
    that were served before the current load began are left out of the
    percentage, so a redeploy that loads one new city reads 0 to 100% for that
    city alone.

    The estimate is the elapsed time since the load began, scaled by the share
    still to do. It holds whether the databases load in parallel or the cities
    on one database load one after another.
    """
    def is_active(row: dict[str, Any]) -> bool:
        if row.get("phase") is not None and row.get("finished_at") is None:
            return True
        return not row.get("served")

    active = [r for r in rows if is_active(r)]
    now = next((r["now"] for r in rows if r.get("now") is not None), None)
    cities = [
        {
            "id": r["source_id"],
            "name": r.get("city_name") or r["source_id"],
            "phase": r.get("phase") if is_active(r) else "done",
            "percent": round(city_fraction(r) * 100, 1) if is_active(r) else 100.0,
            "ready": bool(r.get("served")) and not is_active(r),
        }
        for r in rows
    ]
    if not active:
        return {
            "active": False,
            "percent": 100.0,
            "eta_seconds": None,
            "cities_ready": sum(1 for c in cities if c["ready"]),
            "cities_total": len(cities),
            "cities": cities,
        }

    starts = [r["load_started_at"] for r in active if r.get("load_started_at") is not None]
    started: datetime | None = min(starts) if starts else None
    # The cities on one database load one after another, so a city that
    # finished shortly before the current one began is part of the same load.
    # Walk back through any such chain to where the load really began.
    while started is not None:
        earlier = [
            r["load_started_at"]
            for r in rows
            if r.get("finished_at") is not None
            and r.get("load_started_at") is not None
            and r["load_started_at"] < started
            and r["finished_at"] >= started - CHAIN_GAP
        ]
        if not earlier:
            break
        started = min(earlier)
    in_load = [
        r
        for r in rows
        if is_active(r)
        or (
            started is not None
            and r.get("finished_at") is not None
            and r["finished_at"] >= started
        )
    ]
    share = sum(city_fraction(r) for r in in_load) / len(in_load)
    eta: float | None = None
    if started is not None and now is not None:
        elapsed = (now - started).total_seconds()
        if share >= ETA_MIN_SHARE and elapsed >= ETA_MIN_SECONDS:
            eta = round(elapsed * (1 - share) / share)
    return {
        "active": True,
        "percent": round(share * 100, 1),
        "eta_seconds": eta,
        "started_at": started.isoformat() if started else None,
        "cities_ready": sum(1 for c in cities if c["ready"]),
        "cities_total": len(cities),
        "cities": cities,
    }
