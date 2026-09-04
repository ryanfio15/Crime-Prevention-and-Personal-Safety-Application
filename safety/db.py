"""Database access helpers and on-demand partition management."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from safety.config import settings

log = logging.getLogger(__name__)


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection with dict rows, closing it on exit."""
    conn = psycopg.connect(settings.dsn, row_factory=dict_row, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def wait_for_db(attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Block until the database accepts connections, or raise the last error."""
    import time

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with connect(autocommit=True) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # pragma: no cover - startup race only
            last = exc
            log.info("database not ready (attempt %s/%s)", attempt, attempts)
            time.sleep(delay_seconds)
    raise RuntimeError(f"database never became ready: {last}")


# ---------------------------------------------------------------------------
# Partition management (design doc S9.1)
#
# silver.incident is partitioned LIST (source_id) -> RANGE (occurred_year).
# Partitions are created here rather than in DDL so that adding a seventh city,
# or extending the backfill window into a new year, needs no migration (S11).
# ---------------------------------------------------------------------------


def ensure_partitions(conn: psycopg.Connection, source_id: str, years: Iterable[int]) -> None:
    """Create the city partition and any missing year subpartitions."""
    city_table = f"incident_{source_id}"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass(%s) IS NOT NULL AS exists
            """,
            (f"silver.{city_table}",),
        )
        row = cur.fetchone()
        if not row["exists"]:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE silver.{city} PARTITION OF silver.incident
                        FOR VALUES IN ({source})
                        PARTITION BY RANGE (occurred_year)
                    """
                ).format(
                    city=sql.Identifier(city_table),
                    source=sql.Literal(source_id),
                )
            )
            log.info("created city partition silver.%s", city_table)

        for year in sorted(set(years)):
            year_table = f"{city_table}_{year}"
            cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"silver.{year_table}",))
            if cur.fetchone()["exists"]:
                continue
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE silver.{year_tbl} PARTITION OF silver.{city}
                        FOR VALUES FROM ({lo}) TO ({hi})
                    """
                ).format(
                    year_tbl=sql.Identifier(year_table),
                    city=sql.Identifier(city_table),
                    lo=sql.Literal(year),
                    hi=sql.Literal(year + 1),
                )
            )
            log.info("created year partition silver.%s", year_table)
