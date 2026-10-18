"""Schema migrations and reference-data loading.

Run with:  python -m safety.migrate
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

from safety.config import CROSSWALK_DIR, MIGRATIONS_DIR, SEVERITY_DIR, settings
from safety.db import connect, wait_for_db
from safety.h3grid import RESOLUTIONS, cell_for

log = logging.getLogger(__name__)

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migration (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""

# Arbitrary constant: one migrate per database at a time. Deploys are already
# serialised by install.sh's flock; this also covers a manual run racing one (F16).
_MIGRATE_LOCK_KEY = 0x5AFE_0016


class MigrationChecksumError(RuntimeError):
    """An applied migration file was edited after it ran."""


def migration_checksum(path: Path) -> str:
    # CRLF-normalised: a checkout with autocrlf must not look like an edit.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _ensure_migration_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_MIGRATION_TABLE)
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'schema_migration'
              AND column_name = 'checksum'
            """
        )
        if cur.fetchone() is None:
            # Expand-only: the previous release's INSERT (filename only) still
            # works and leaves NULL, which the next run of this code adopts.
            cur.execute("ALTER TABLE public.schema_migration ADD COLUMN checksum text")
    conn.commit()


def verify_checksums(conn: psycopg.Connection, paths: Iterable[Path]) -> int:
    """Raise on an edited applied migration; record checksums still missing.

    Commits only after a clean check. Returns how many checksums it recorded.
    """
    files = {p.name: p for p in paths}
    rows = conn.execute("SELECT filename, checksum FROM public.schema_migration").fetchall()
    bad = [
        r["filename"]
        for r in rows
        if r["checksum"]
        and r["filename"] in files
        and migration_checksum(files[r["filename"]]) != r["checksum"]
    ]
    if bad:
        conn.rollback()
        raise MigrationChecksumError(
            f"applied migration(s) edited since they ran: {', '.join(sorted(bad))}. "
            "Revert the edit and add a new migration instead; after review, "
            "`UPDATE public.schema_migration SET checksum = NULL WHERE filename = ...` "
            "re-adopts the file as it is now."
        )
    adopt = [
        (migration_checksum(files[r["filename"]]), r["filename"])
        for r in rows
        if r["checksum"] is None and r["filename"] in files
    ]
    if adopt:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE public.schema_migration SET checksum = %s "
                "WHERE filename = %s AND checksum IS NULL",
                adopt,
            )
        log.info("recorded checksums for %s previously applied migration(s)", len(adopt))
    conn.commit()
    gone = sorted(r["filename"] for r in rows if r["filename"] not in files)
    if gone:
        log.warning("applied migrations no longer in the tree: %s", ", ".join(gone))
    return len(adopt)


@contextmanager
def migration_lock(
    conn: psycopg.Connection, wait_seconds: float | None = None, poll: float = 2.0
) -> Iterator[None]:
    """Session-level advisory lock, held across this run's commits.

    pg_try_advisory_lock never waits, so the deploy's lock_timeout does not
    apply; this polls up to `wait_seconds`. Closing the session releases it too.
    """
    wait = settings.migrate_lock_wait_seconds if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + wait
    while True:
        ok = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (_MIGRATE_LOCK_KEY,)
        ).fetchone()["ok"]
        conn.commit()
        if ok:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"another safety.migrate has held the migration lock for {wait:.0f}s"
            )
        log.info("another safety.migrate is running; waiting for it")
        time.sleep(poll)
    try:
        yield
    finally:
        try:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATE_LOCK_KEY,))
            conn.commit()
        except psycopg.Error:
            pass


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply every unapplied db/migrations/*.sql in filename order.

    Refuses (MigrationChecksumError) if an already-applied file was edited.
    """
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    _ensure_migration_table(conn)
    verify_checksums(conn, paths)
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM public.schema_migration")
        already = {r["filename"] for r in cur.fetchall()}
    conn.commit()

    applied: list[str] = []
    for path in paths:
        if path.name in already:
            continue
        log.info("applying migration %s", path.name)
        # Each migration is its own transaction, so a failure leaves the
        # preceding migrations applied and this one fully rolled back.
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO public.schema_migration (filename, checksum) VALUES (%s, %s)",
                (path.name, migration_checksum(path)),
            )
        conn.commit()
        applied.append(path.name)

    return applied


# ---------------------------------------------------------------------------
# Crosswalk loading (design doc S7.2: a maintained reference dataset, not code)
# ---------------------------------------------------------------------------

_CROSSWALK_COLUMNS = (
    "crosswalk_version",
    "source_id",
    "raw_offense_code",
    "raw_offense_text",
    "raw_offense_text_key",
    "raw_source_category",
    "nibrs_code",
    "nibrs_offense_name",
    "nibrs_group",
    "nibrs_crime_against",
    "ucr_part",
    "severity_bucket",
    "product_category",
    "mapping_confidence",
    "effective_from",
    "effective_to",
    "notes",
)


def load_crosswalks(conn: psycopg.Connection) -> int:
    """Upsert every reference/crosswalk/*.csv into reference.offense_crosswalk.

    A row whose `raw_offense_text` is the single character `*` is a code-only
    fallback: it matches any description published under that offense code, and
    is consulted only when no exact code/text row matches (see the two-tier
    lookup in safety/etl/transform.py). Nothing special happens here -- `*`
    uppercases to itself -- but it is worth naming, because a file full of them
    means a crosswalk that has given up on the source's text entirely.
    """
    total = 0
    for path in sorted(CROSSWALK_DIR.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        payload = []
        for row in rows:
            raw_text = (row["raw_offense_text"] or "").strip()
            payload.append(
                (
                    row["crosswalk_version"],
                    row["source_id"],
                    (row["raw_offense_code"] or "").strip(),
                    raw_text,
                    raw_text.upper(),
                    row.get("raw_source_category") or None,
                    row.get("nibrs_code") or None,
                    row.get("nibrs_offense_name") or None,
                    row.get("nibrs_group") or None,
                    row.get("nibrs_crime_against") or None,
                    row.get("ucr_part") or None,
                    row["severity_bucket"],
                    row["product_category"],
                    row["mapping_confidence"],
                    row["effective_from"],
                    row.get("effective_to") or None,
                    row.get("notes") or None,
                )
            )

        with conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO reference.offense_crosswalk ({", ".join(_CROSSWALK_COLUMNS)})
                VALUES ({", ".join(["%s"] * len(_CROSSWALK_COLUMNS))})
                ON CONFLICT (crosswalk_version, source_id, raw_offense_code,
                             raw_offense_text_key, effective_from)
                DO UPDATE SET
                    raw_offense_text    = EXCLUDED.raw_offense_text,
                    raw_source_category = EXCLUDED.raw_source_category,
                    nibrs_code          = EXCLUDED.nibrs_code,
                    nibrs_offense_name  = EXCLUDED.nibrs_offense_name,
                    nibrs_group         = EXCLUDED.nibrs_group,
                    nibrs_crime_against = EXCLUDED.nibrs_crime_against,
                    ucr_part            = EXCLUDED.ucr_part,
                    severity_bucket     = EXCLUDED.severity_bucket,
                    product_category    = EXCLUDED.product_category,
                    mapping_confidence  = EXCLUDED.mapping_confidence,
                    effective_to        = EXCLUDED.effective_to,
                    notes               = EXCLUDED.notes
                """,
                payload,
            )
        conn.commit()
        log.info("loaded %s crosswalk rows from %s", len(payload), path.name)
        total += len(payload)

    return total


# ---------------------------------------------------------------------------
# Severity schemes and weights (design doc S3.3)
#
# Same rule as the crosswalk: the numbers behind the safety ranking are a
# maintained reference dataset, so retuning the statistic is a CSV edit and a
# reload, never a code change.
# ---------------------------------------------------------------------------

_SCHEME_COLUMNS = (
    "scheme_version",
    "description",
    "source_citation",
    "exposure_kind",
    "eb_prior_km2",
    "eb_prior_persons",
    "jobs_weight",
    "self_weight",
    "enabled",
    "notes",
)

_WEIGHT_COLUMNS = (
    "scheme_version",
    "track",
    "key_type",
    "key_value",
    "weight",
    "sourced",
    "source_item",
    "notes",
)


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "t", "true", "yes", "y"}


def _as_float(value: str | None) -> float | None:
    """An empty CSV cell is "this scheme does not use that parameter"."""
    if value is None or value.strip() == "":
        return None
    return float(value)


def load_severity_schemes(conn: psycopg.Connection) -> int:
    """Upsert reference/severity/schemes.csv into reference.severity_scheme."""
    path = SEVERITY_DIR / "schemes.csv"
    if not path.exists():
        log.warning("no severity schemes at %s; safety ranking will not build", path)
        return 0

    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    payload = [
        (
            row["scheme_version"].strip(),
            row["description"],
            row["source_citation"],
            (row.get("exposure_kind") or "area_km2").strip(),
            float(row["eb_prior_km2"]),
            _as_float(row.get("eb_prior_persons")),
            _as_float(row.get("jobs_weight")) or 1.0,
            float(row["self_weight"]),
            _as_bool(row.get("enabled")),
            row.get("notes") or None,
        )
        for row in rows
    ]

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO reference.severity_scheme ({", ".join(_SCHEME_COLUMNS)})
            VALUES ({", ".join(["%s"] * len(_SCHEME_COLUMNS))})
            ON CONFLICT (scheme_version) DO UPDATE SET
                description        = EXCLUDED.description,
                source_citation    = EXCLUDED.source_citation,
                exposure_kind      = EXCLUDED.exposure_kind,
                eb_prior_km2       = EXCLUDED.eb_prior_km2,
                eb_prior_persons   = EXCLUDED.eb_prior_persons,
                jobs_weight        = EXCLUDED.jobs_weight,
                self_weight        = EXCLUDED.self_weight,
                enabled            = EXCLUDED.enabled,
                notes              = EXCLUDED.notes
            """,
            payload,
        )
    conn.commit()
    log.info("loaded %s severity scheme(s) from %s", len(payload), path.name)
    return len(payload)


def copy_inherited_weights(conn: psycopg.Connection) -> int:
    """Give a scheme the weight table of the one it declares it inherits from.

    A scheme that changes only the denominator must carry *identical* severity
    weights to the one it is being compared against, or safety-compare stops
    being a read on the denominator and becomes a read on both at once. Copying
    is how that identity is guaranteed; duplicating two hundred CSV rows would
    let the two drift apart silently on the next edit.

    Must run after load_severity_weights, since the parent's rows have to exist
    before they can be copied.
    """
    path = SEVERITY_DIR / "schemes.csv"
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    copied = 0
    for row in rows:
        parent = (row.get("inherits_weights_from") or "").strip()
        if not parent:
            continue
        child = row["scheme_version"].strip()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reference.offense_severity_weight
                    (scheme_version, track, key_type, key_value, weight,
                     sourced, source_item, notes)
                SELECT %s, track, key_type, key_value, weight,
                       sourced, source_item, notes
                FROM reference.offense_severity_weight
                WHERE scheme_version = %s
                ON CONFLICT (scheme_version, track, key_type, key_value)
                DO UPDATE SET
                    weight      = EXCLUDED.weight,
                    sourced     = EXCLUDED.sourced,
                    source_item = EXCLUDED.source_item,
                    notes       = EXCLUDED.notes
                """,
                (child, parent),
            )
            written = cur.rowcount
        conn.commit()
        copied += written
        if not written:
            log.warning(
                "scheme '%s' inherits from '%s', which has no weights loaded; "
                "the ranking will fall back to a weight of 1.0 for every offense",
                child,
                parent,
            )
        else:
            log.info("scheme '%s' inherits %s weight(s) from '%s'", child, written, parent)
    return copied


def load_severity_weights(conn: psycopg.Connection) -> int:
    """Upsert every reference/severity/weights_*.csv into the weight table."""
    total = 0
    for path in sorted(SEVERITY_DIR.glob("weights_*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

        payload = [
            (
                row["scheme_version"].strip(),
                row["track"].strip(),
                row["key_type"].strip(),
                # Raw-text keys are matched against the crosswalk's uppercased
                # form, so casing drift upstream cannot break the lookup.
                row["key_value"].strip().upper()
                if row["key_type"].strip() == "raw_offense_text_key"
                else row["key_value"].strip(),
                float(row["weight"]),
                _as_bool(row.get("sourced")),
                row.get("source_item") or None,
                row.get("notes") or None,
            )
            for row in rows
        ]

        with conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO reference.offense_severity_weight ({", ".join(_WEIGHT_COLUMNS)})
                VALUES ({", ".join(["%s"] * len(_WEIGHT_COLUMNS))})
                ON CONFLICT (scheme_version, track, key_type, key_value)
                DO UPDATE SET
                    weight      = EXCLUDED.weight,
                    sourced     = EXCLUDED.sourced,
                    source_item = EXCLUDED.source_item,
                    notes       = EXCLUDED.notes
                """,
                payload,
            )
        conn.commit()
        log.info("loaded %s severity weight(s) from %s", len(payload), path.name)
        total += len(payload)

    return total


def point_sources_at_scheme(conn: psycopg.Connection) -> int:
    """Give every source a severity scheme, without overwriting an explicit one.

    The registry column has to be nullable -- migrations run before the CSVs
    load, so there is no scheme to reference at DDL time. This closes that gap
    on the first load and then leaves the column alone, so pinning one city to
    an older scheme survives a reload.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scheme_version FROM reference.severity_scheme WHERE enabled ORDER BY scheme_version"
        )
        enabled = [r["scheme_version"] for r in cur.fetchall()]
        if len(enabled) != 1:
            log.info(
                "%s enabled severity scheme(s); leaving source_registry pointers alone",
                len(enabled),
            )
            return 0

        cur.execute(
            """
            UPDATE reference.source_registry
               SET severity_scheme_version = %s, updated_at = now()
             WHERE severity_scheme_version IS NULL
            """,
            (enabled[0],),
        )
        updated = cur.rowcount
    conn.commit()
    if updated:
        log.info("pointed %s source(s) at severity scheme '%s'", updated, enabled[0])
    return updated


# Gold tables keyed by scheme_version, widest first so the hourly layer goes
# before the all-hours ranking its baseline came from.
_SCHEME_KEYED_TABLES = ("gold.cell_hour_safety", "gold.cell_safety")


def prune_disabled_schemes(conn: psycopg.Connection) -> int:
    """Reclaim the gold rows of a scheme that has been switched off.

    scheme_version is part of the primary key of both tables above, so an extra
    enabled scheme is an extra complete copy of the safety ranking -- and of the
    hourly layer, which is that same ranking recomputed 24 times per window.
    Setting `enabled` to false in schemes.csv stops the pipeline building one,
    but the rows it has already written are not touched by any later refresh:
    every rebuild is scoped to the scheme being rebuilt, which is what makes a
    refresh of one scheme leave the others alone. So they would sit there
    indefinitely, unreadable and unrefreshed.

    A scheme still named by reference.source_registry is never pruned, even when
    disabled. Those rows are what that city's map is drawn from, and deleting
    them would blank it. Disabled-but-serving is a misconfiguration worth
    reporting rather than acting on -- the fix is `--activate`, not a DELETE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scheme_version FROM reference.severity_scheme WHERE NOT enabled"
        )
        disabled = {r["scheme_version"] for r in cur.fetchall()}
        if not disabled:
            return 0

        cur.execute(
            """
            SELECT DISTINCT severity_scheme_version AS v
            FROM reference.source_registry
            WHERE severity_scheme_version IS NOT NULL
            """
        )
        in_use = {r["v"] for r in cur.fetchall()}

    serving = sorted(disabled & in_use)
    for version in serving:
        log.warning(
            "severity scheme '%s' is disabled but is still the scheme one or more "
            "cities serve; keeping its rows. Point them at an enabled scheme with "
            "`python -m safety.migrate --activate <scheme>`",
            version,
        )

    prunable = sorted(disabled - in_use)
    if not prunable:
        return 0

    removed = 0
    with conn.cursor() as cur:
        for table in _SCHEME_KEYED_TABLES:
            cur.execute(
                f"DELETE FROM {table} WHERE scheme_version = ANY(%s)", (prunable,)
            )
            if cur.rowcount:
                log.info("pruned %s row(s) from %s", cur.rowcount, table)
            removed += cur.rowcount
    conn.commit()

    if removed:
        log.info(
            "reclaimed %s gold row(s) for disabled scheme(s) %s",
            removed,
            ", ".join(prunable),
        )
        _vacuum(conn, _SCHEME_KEYED_TABLES)
    return removed


def _vacuum(conn: psycopg.Connection, tables: tuple[str, ...]) -> None:
    """Plain VACUUM over tables a large DELETE has just been run against.

    Plain and not FULL, deliberately. FULL would hand the space back to the
    filesystem, but it takes an ACCESS EXCLUSIVE lock and needs free disk for a
    whole rewritten copy -- the wrong thing to do unprompted on the small volume
    that motivates pruning in the first place. Plain VACUUM marks the space
    reusable by the same table, which is exactly where it goes: both of these are
    rebuilt by delete-then-insert on every refresh, so they reuse it immediately
    instead of growing the file again.

    If the space is needed by something else -- a new city's partitions -- the
    operator wants VACUUM FULL, and the log says so rather than guessing.
    """
    prior = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"VACUUM (ANALYZE) {table}")
    finally:
        conn.autocommit = prior
    log.info(
        "vacuumed %s; the freed pages are reusable by those tables. To hand them "
        "back to the filesystem instead, run VACUUM FULL on them during a quiet "
        "window (it locks the table and needs room for a second copy).",
        ", ".join(tables),
    )


def activate_scheme(conn: psycopg.Connection, scheme_version: str, source_id: str | None) -> int:
    """Promote a scheme to the one a city actually serves.

    Deliberately explicit rather than automatic. point_sources_at_scheme only
    ever fills a NULL, because promoting changes what every safety number in the
    product means -- a per-capita ranking and an area ranking are different
    quantities wearing the same label. That is a decision to be made after
    looking at safety-compare, not a side effect of re-running the loader.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled FROM reference.severity_scheme WHERE scheme_version = %s",
            (scheme_version,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(
                f"no severity scheme '{scheme_version}'; check reference/severity/schemes.csv"
            )
        if not row["enabled"]:
            raise ValueError(
                f"severity scheme '{scheme_version}' is disabled, so the pipeline "
                "would never build it; enable it in schemes.csv first"
            )

        if source_id:
            cur.execute(
                """
                UPDATE reference.source_registry
                   SET severity_scheme_version = %s, updated_at = now()
                 WHERE source_id = %s
                """,
                (scheme_version, source_id),
            )
        else:
            cur.execute(
                """
                UPDATE reference.source_registry
                   SET severity_scheme_version = %s, updated_at = now()
                 WHERE severity_scheme_version IS DISTINCT FROM %s
                """,
                (scheme_version, scheme_version),
            )
        updated = cur.rowcount
    conn.commit()
    log.info(
        "activated severity scheme '%s' for %s source(s); "
        "re-run the safety and hourly layers for the change to reach the map",
        scheme_version,
        updated,
    )
    return updated


# ---------------------------------------------------------------------------
# H3 backfill
#
# H3 lives in Python, not in the database, so a migration that adds a cell
# column cannot populate it. This fills whatever is missing, for any resolution
# in h3grid.RESOLUTIONS, and is a no-op once every row is covered -- which is
# what lets it sit in the deploy path rather than needing a manual step.
# ---------------------------------------------------------------------------

_BACKFILL_BATCH = 50_000


def backfill_h3_cells(conn: psycopg.Connection) -> int:
    """Fill any NULL H3 cell column on silver.incident from its coordinates."""
    filled = 0
    for res in RESOLUTIONS:
        column = f"h3_r{res}"
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT source_id, occurred_year, incident_key, latitude, longitude
                    FROM silver.incident
                    WHERE {column} IS NULL
                    LIMIT %s
                    """,
                    (_BACKFILL_BATCH,),
                )
                rows = cur.fetchall()
            if not rows:
                break

            # A temp table plus one UPDATE ... FROM, rather than a statement per
            # row: the first backfill covers every record the pipeline has ever
            # loaded.
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _h3_fill "
                    "(source_id text, occurred_year smallint, incident_key text, cell text) "
                    "ON COMMIT DROP"
                )
                with cur.copy(
                    "COPY _h3_fill (source_id, occurred_year, incident_key, cell) FROM STDIN"
                ) as copy:
                    for row in rows:
                        copy.write_row(
                            (
                                row["source_id"],
                                row["occurred_year"],
                                row["incident_key"],
                                cell_for(row["latitude"], row["longitude"], res),
                            )
                        )
                cur.execute(
                    f"""
                    UPDATE silver.incident i
                       SET {column} = f.cell
                      FROM _h3_fill f
                     WHERE i.source_id     = f.source_id
                       AND i.occurred_year = f.occurred_year
                       AND i.incident_key  = f.incident_key
                    """
                )
                filled += cur.rowcount
                cur.execute("DROP TABLE _h3_fill")
            conn.commit()
            log.info("backfilled %s for %s row(s)", column, len(rows))

    if filled:
        log.info(
            "H3 backfill filled %s cell value(s); re-run the gold rollups to "
            "pick up the new resolution",
            filled,
        )
    return filled


# ---------------------------------------------------------------------------
# Clock-hour backfill
#
# New loads carry occurred_local_hour from the source's own `hour` column. Rows
# already in silver predate that column and cannot be re-read without a bronze
# replay, so they are filled from occurred_at instead -- which works only
# because of a quirk worth stating plainly rather than relying on silently.
#
# The Carto API renders dispatch_date_time with a '+00' suffix on what is a
# local wall-clock value, and the adapter stores it as UTC accordingly. Reading
# the hour back out at UTC therefore returns the published local clock. If the
# timestamp were a genuine UTC instant the same expression would be wrong by
# four or five hours, so the assumption is tested before anything is written.
# ---------------------------------------------------------------------------

# Everything the decision rests on, measured before any of it is written. An
# earlier version gated on occurred_precision = 'exact' and returned silently
# when nothing matched, which is how a source that never populates
# `dispatch_time` produced an empty hourly layer and no explanation for it.
# The precision flag is the wrong question anyway: what matters is whether the
# stored timestamp carries a time of day, and that can be measured directly.
#
# Scoped to one source. Every statement below is, and that is not cosmetic: the
# decision rests on a *share* of rows, and each source has its own timestamp
# semantics. Measured across a pooled six-city table, one source whose
# occurred_at is a genuine UTC instant would drag `date_aligned` below the floor
# and refuse the backfill for every city -- logging a diagnosis that is true of
# none of them.
_HOUR_AUDIT_SQL = """
SELECT
    count(*)                                                     AS total,
    count(occurred_local_hour)                                   AS already_filled,
    count(*) FILTER (WHERE occurred_precision = 'exact')         AS precision_exact,
    count(*) FILTER (
        WHERE (occurred_at AT TIME ZONE 'UTC')::date = occurred_local_date
    )                                                            AS date_aligned,
    count(*) FILTER (
        WHERE (occurred_at AT TIME ZONE 'UTC')::time = '00:00:00'
    )                                                            AS at_midnight,
    count(DISTINCT EXTRACT(hour FROM occurred_at AT TIME ZONE 'UTC'))
                                                                 AS distinct_hours
FROM silver.incident
WHERE source_id = %(source_id)s
"""

# If the stored timestamp really were UTC, every incident from 19:00 local
# onwards would carry the *next* day's UTC date -- roughly a fifth of them. A
# near-total match is only possible if the timestamp holds local wall-clock.
_HOUR_ALIGNMENT_FLOOR = 0.99

# A real clock puts about 1 in 24 incidents (4.2%) in any given hour. Midnight
# runs somewhat above that in dispatch data -- round-hour reporting is a real
# habit -- but far above it means the value is standing in for "no time was
# published" rather than recording one. Above this share, exact-midnight rows
# are treated as unknown and left NULL.
_MIDNIGHT_SHARE_CEILING = 0.10

_HOUR_BACKFILL_SQL = """
UPDATE silver.incident i
   SET occurred_local_hour =
           EXTRACT(hour FROM i.occurred_at AT TIME ZONE 'UTC')::smallint
 WHERE (i.source_id, i.occurred_year, i.incident_key) IN (
        SELECT source_id, occurred_year, incident_key
        FROM silver.incident
        WHERE source_id = %(source_id)s
          AND occurred_local_hour IS NULL
          AND (
                NOT %(skip_midnight)s::boolean
                OR (occurred_at AT TIME ZONE 'UTC')::time <> '00:00:00'
              )
        LIMIT %(batch)s
 )
"""

_HOUR_HISTOGRAM_SQL = """
SELECT occurred_local_hour AS hour, count(*) AS n
FROM silver.incident
WHERE source_id = %(source_id)s AND occurred_local_hour IS NOT NULL
GROUP BY 1 ORDER BY 1
"""


def backfill_incident_hour(conn: psycopg.Connection) -> int:
    """Fill occurred_local_hour on rows loaded before the column existed.

    One source at a time. The audit's verdict is a share of that source's own
    rows, and the sources do not share timestamp semantics -- so a pooled
    measurement would let one city's data decide another city's outcome.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT source_id FROM silver.incident ORDER BY source_id
            """
        )
        sources = [r["source_id"] for r in cur.fetchall()]

    if not sources:
        log.info("no silver rows yet; nothing to backfill a clock hour onto")
        return 0

    return sum(_backfill_hour_for_source(conn, source_id) for source_id in sources)


def _backfill_hour_for_source(conn: psycopg.Connection, source_id: str) -> int:
    """Audit, then backfill, one source's clock hours.

    Reports what it measured and what it decided in every branch. A backfill
    that declines to run is a legitimate outcome here, but a silent one leaves
    the hourly layer empty with nothing to explain it.
    """
    with conn.cursor() as cur:
        cur.execute(_HOUR_AUDIT_SQL, {"source_id": source_id})
        audit = cur.fetchone() or {}

    total = audit.get("total") or 0
    if not total:
        return 0
    if audit["already_filled"] == total:
        return 0

    def pct(n: int | None) -> float:
        return (n or 0) / total * 100

    log.info(
        "%s clock-hour audit over %s rows: %s already filled, %.1f%% flagged "
        "occurred_precision='exact', %.1f%% whose UTC date matches the local "
        "date, %.1f%% stamped exactly midnight, %s distinct hours present",
        source_id,
        total,
        audit["already_filled"],
        pct(audit["precision_exact"]),
        pct(audit["date_aligned"]),
        pct(audit["at_midnight"]),
        audit["distinct_hours"],
    )

    if (audit["distinct_hours"] or 0) <= 1:
        log.error(
            "%s: occurred_at carries no time of day at all -- every row sits on "
            "the same hour -- so the clock hour cannot be recovered from it. "
            "Replay the stored snapshots (python -m safety.etl.run reprocess "
            "--city %s --pull-id <id>) to read the hour from the source instead.",
            source_id,
            source_id,
        )
        return 0

    aligned = (audit["date_aligned"] or 0) / total
    if aligned < _HOUR_ALIGNMENT_FLOOR:
        # Refusing is the right outcome. A wrong hour is worse than a missing
        # one: the hourly layer would render confidently and be shifted whole
        # hours, and nothing downstream could detect it.
        log.error(
            "%s: occurred_at does not look like local wall-clock for %.1f%% of "
            "incidents, so the clock hour cannot be recovered from it. Leaving "
            "occurred_local_hour NULL; replay the bronze snapshots "
            "(python -m safety.etl.run reprocess --city %s) to read the hour "
            "from the source.",
            source_id,
            (1 - aligned) * 100,
            source_id,
        )
        return 0

    skip_midnight = (audit["at_midnight"] or 0) / total > _MIDNIGHT_SHARE_CEILING
    if skip_midnight:
        log.warning(
            "%s: %.1f%% of incidents are stamped exactly midnight, far above the "
            "~4%% a real clock produces: that value is standing in for a time "
            "the source did not publish. Those rows stay NULL and are absent "
            "from the hourly layers rather than counted at 00:00.",
            source_id,
            pct(audit["at_midnight"]),
        )

    filled = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                _HOUR_BACKFILL_SQL,
                {
                    "source_id": source_id,
                    "skip_midnight": skip_midnight,
                    "batch": _BACKFILL_BATCH,
                },
            )
            written = cur.rowcount
        conn.commit()
        if not written:
            break
        filled += written
        log.info("%s: backfilled occurred_local_hour for %s row(s)", source_id, written)

    if not filled:
        log.warning(
            "%s: clock-hour backfill matched no rows; the hourly layers stay empty",
            source_id,
        )
        return 0

    # Print the day the backfill produced. A plausible one dips through the
    # small hours and rises into the evening; a flat line, or one spike holding
    # everything, means the hour is not what it claims to be -- and this is the
    # only place that shape can be checked before it reaches a user.
    with conn.cursor() as cur:
        cur.execute(_HOUR_HISTOGRAM_SQL, {"source_id": source_id})
        rows = cur.fetchall()
    log.info(
        "%s hour distribution: %s",
        source_id,
        " ".join(f"{r['hour']:02d}:{r['n']}" for r in rows),
    )
    log.info(
        "%s: clock-hour backfill filled %s row(s); re-run the gold rollups "
        "(python -m safety.etl.run hourly --city %s) to build the hourly layers",
        source_id,
        filled,
        source_id,
    )
    return filled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="safety.migrate", description=__doc__)
    parser.add_argument(
        "--activate",
        metavar="SCHEME",
        default=None,
        help=(
            "promote a severity scheme to the one cities serve "
            "(e.g. nscs_v2_percapita). Changes what the safety numbers mean, so "
            "it is never done automatically"
        ),
    )
    parser.add_argument(
        "--city",
        default=None,
        help="limit --activate to one city; default is every source",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    wait_for_db()
    try:
        with connect() as conn, migration_lock(conn):
            return _run(conn, args)
    except (MigrationChecksumError, TimeoutError) as exc:
        print(f"migrate refused: {exc}", file=sys.stderr)
        return 1


def _run(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    # Everything under the migration lock, the data loaders included: they
    # DELETE and VACUUM, and must not race another migrate either.
    applied = apply_migrations(conn)
    # Schemes before weights (foreign key), and both before the registry
    # pointer they are referenced by.
    scheme_rows = load_severity_schemes(conn)
    weight_rows = load_severity_weights(conn)
    # After the weight files, so an inheriting scheme copies a table that
    # actually exists.
    copy_inherited_weights(conn)
    crosswalk_rows = load_crosswalks(conn)
    point_sources_at_scheme(conn)
    if args.activate:
        activate_scheme(conn, args.activate, args.city)
    # After --activate, so a scheme being promoted in this same run is never
    # a candidate, and after the loader, so `enabled` reflects the CSV.
    pruned = prune_disabled_schemes(conn)
    backfilled = backfill_h3_cells(conn)
    hours_filled = backfill_incident_hour(conn)

    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Schema already up to date.")
    print(f"Crosswalk rows loaded/refreshed: {crosswalk_rows}")
    print(f"Severity schemes: {scheme_rows}, severity weights: {weight_rows}")
    if pruned:
        print(f"Gold rows reclaimed from disabled scheme(s): {pruned}")
    if backfilled:
        print(f"H3 cells backfilled: {backfilled} (re-run the gold rollups)")
    if hours_filled:
        print(f"Clock hours backfilled: {hours_filled} (re-run the gold rollups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
