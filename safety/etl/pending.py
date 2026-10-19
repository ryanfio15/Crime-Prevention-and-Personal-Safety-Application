"""Which cities have silver changes their gold layers do not reflect yet.

Backed by etl.gold_pending (migration 021). Every write that changes silver
marks the city here in its own transaction, with the earliest occurrence date it
touched; a gold refresh clears the mark in its. An incremental reads it to
decide whether gold needs rebuilding at all, and from which day the daily
rollup does.

A mark with no date means "rebuild everything" and always wins over a dated
one: once anything undated is pending, narrowing it back to a date range would
lose it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

_MARK_SQL = """
INSERT INTO etl.gold_pending (source_id, dirty_from, marked_at)
VALUES (%(source_id)s, %(dirty_from)s, now())
ON CONFLICT (source_id) DO UPDATE SET
    dirty_from = CASE
        WHEN gold_pending.dirty_from IS NULL OR EXCLUDED.dirty_from IS NULL THEN NULL
        ELSE LEAST(gold_pending.dirty_from, EXCLUDED.dirty_from)
    END,
    marked_at = now()
"""


@dataclass(frozen=True, slots=True)
class Pending:
    # None: rebuild everything. A date: nothing before it changed.
    dirty_from: date | None

    @property
    def full(self) -> bool:
        return self.dirty_from is None


def mark(conn: psycopg.Connection, source_id: str, dirty_from: date | None) -> None:
    """Record that silver changed from `dirty_from` on (None: anywhere). Does not commit."""
    with conn.cursor() as cur:
        cur.execute(_MARK_SQL, {"source_id": source_id, "dirty_from": dirty_from})


def get(conn: psycopg.Connection, source_id: str) -> Pending | None:
    """The city's pending change, or None when gold is up to date with silver."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dirty_from FROM etl.gold_pending WHERE source_id = %s", (source_id,)
        )
        row = cur.fetchone()
    return Pending(row["dirty_from"]) if row else None


def clear(conn: psycopg.Connection, source_id: str) -> None:
    """Gold now reflects silver. Does not commit: call inside the refresh's transaction."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM etl.gold_pending WHERE source_id = %s", (source_id,))
