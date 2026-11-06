"""One rolling year per city, and nothing older.

The product serves the last year of incidents and nothing else, so nothing older
is kept. A row whose local occurrence date is `settings.retention_days` (365) or
more days before the city's local today is deleted. In other words, an incident
366 or more days old is gone. Old data is also kept out on the way in: the
fetch windows are clamped to the cutoff (safety.etl.windows), and promotion
drops staged records older than it (safety.etl.transform), so a source that
filters on report date cannot smuggle a decades-old cold case into silver.

`prune` works on silver:
* It drops whole year partitions (silver.incident_<city>_<year>) that lie
  entirely before the cutoff. Dropping a partition is instant and gives the disk
  space back.
* It deletes the rows older than the cutoff from the one year partition the
  cutoff falls in.

The withdrawn-upstream archive gets the same cutoff. Gold is then marked for a
full rebuild (safety.etl.pending), so the next refresh reflects the shorter
history.

`prune_bronze` removes raw pull directories (bronze `pull_date=` folders) dated
before the cutoff.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from safety.config import settings
from safety.etl import pending

log = logging.getLogger(__name__)

_PULL_DATE = re.compile(r"^pull_date=(\d{4}-\d{2}-\d{2})$")


def local_today(timezone: str | None, now: datetime | None = None) -> date:
    """Today in the city's own time zone (UTC for an unknown one)."""
    try:
        zone = ZoneInfo(timezone or "UTC")
    except Exception:  # an unknown or malformed zone name
        zone = ZoneInfo("UTC")
    moment = now or datetime.now(tz=ZoneInfo("UTC"))
    return moment.astimezone(zone).date()


def cutoff(timezone: str | None = None, now: datetime | None = None, days: int | None = None) -> date:
    """The oldest local date a city keeps. Anything before it is deleted.

    With the default 365 days this is today - 365: today and the 365 days
    before it are kept, and the 366th day back is the first one deleted.
    """
    keep = settings.retention_days if days is None else days
    return local_today(timezone, now) - timedelta(days=max(int(keep), 1))


def city_timezone(conn: psycopg.Connection, source_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timezone FROM reference.source_registry WHERE source_id = %s", (source_id,)
        )
        row = cur.fetchone()
    return row["timezone"] if row else None


def city_cutoff(conn: psycopg.Connection, source_id: str, now: datetime | None = None) -> date:
    return cutoff(city_timezone(conn, source_id), now)


def _year_partitions(conn: psycopg.Connection, source_id: str) -> list[tuple[str, int]]:
    """(table name, year) for each existing silver.incident_<city>_<year> partition."""
    prefix = f"incident_{source_id}_"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname AS name
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace n ON n.oid = p.relnamespace
            WHERE n.nspname = 'silver' AND p.relname = %s
            """,
            (f"incident_{source_id}",),
        )
        rows = cur.fetchall()
    found = []
    for row in rows:
        name = row["name"]
        suffix = name[len(prefix):]
        if name.startswith(prefix) and suffix.isdigit():
            found.append((name, int(suffix)))
    return sorted(found, key=lambda pair: pair[1])


def prune(conn: psycopg.Connection, source_id: str, keep_from: date | None = None) -> dict:
    """Delete silver rows (and archived withdrawn rows) older than the cutoff.

    Does not commit. Returns what was removed. Marks gold for a full rebuild
    when anything was.
    """
    keep_from = keep_from or city_cutoff(conn, source_id)
    dropped: list[str] = []
    deleted = 0
    with conn.cursor() as cur:
        for name, year in _year_partitions(conn, source_id):
            if year < keep_from.year:
                cur.execute(sql.SQL("DROP TABLE silver.{}").format(sql.Identifier(name)))
                dropped.append(name)
        cur.execute(
            """
            DELETE FROM silver.incident
            WHERE source_id = %(s)s
              AND occurred_year = %(y)s
              AND occurred_local_date < %(cut)s
            """,
            {"s": source_id, "y": keep_from.year, "cut": keep_from},
        )
        # occurred_year is the year of occurred_local_date, so the later years'
        # partitions cannot hold a date before the cutoff: this one is enough.
        deleted += cur.rowcount
        cur.execute(
            """
            DELETE FROM etl.withdrawn_incident
            WHERE source_id = %(s)s AND occurred_local_date < %(cut)s
            """,
            {"s": source_id, "cut": keep_from},
        )
        archived = cur.rowcount
    if dropped or deleted:
        pending.mark(conn, source_id, None)
    if dropped or deleted or archived:
        log.info(
            "%s: kept incidents from %s; dropped %s, deleted %s rows, %s archived rows",
            source_id,
            keep_from,
            ", ".join(dropped) or "no partitions",
            deleted,
            archived,
        )
    return {
        "keep_from": keep_from,
        "partitions_dropped": dropped,
        "rows_deleted": deleted,
        "archived_deleted": archived,
    }


def prune_bronze(source_id: str, keep_from: date, root: Path | None = None) -> list[Path]:
    """Remove this city's raw pull directories dated before `keep_from`.

    Covers every dataset the city has pulled (incidents, boundary, census). The
    bronze layout is <root>/source_id=<id>/dataset=<name>/pull_date=<date>/.
    """
    base = Path(root or settings.bronze_root) / f"source_id={source_id}"
    removed: list[Path] = []
    if not base.is_dir():
        return removed
    for dataset in sorted(base.iterdir()):
        if not dataset.is_dir():
            continue
        for day in sorted(dataset.iterdir()):
            match = _PULL_DATE.match(day.name)
            if not match or not day.is_dir():
                continue
            try:
                pulled = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if pulled < keep_from:
                shutil.rmtree(day, ignore_errors=True)
                removed.append(day)
    if removed:
        log.info("%s: removed %s bronze pull folder(s) dated before %s", source_id, len(removed), keep_from)
    return removed


def oldest_kept(conn: psycopg.Connection, source_id: str) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(occurred_local_date) AS d FROM silver.incident WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    return row["d"] if row else None


def apply(conn: psycopg.Connection, source_id: str) -> dict:
    """Prune silver and bronze for one city in their own transaction. Never raises."""
    try:
        keep_from = city_cutoff(conn, source_id)
        outcome = prune(conn, source_id, keep_from)
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("retention prune for %s failed; continuing", source_id)
        return {"error": True}
    try:
        outcome["bronze_removed"] = len(prune_bronze(source_id, keep_from))
    except Exception:
        log.exception("bronze prune for %s failed; continuing", source_id)
        outcome["bronze_removed"] = None
    return outcome
