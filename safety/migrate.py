"""Schema migrations and reference-data loading.

Run with:  python -m safety.migrate
"""

from __future__ import annotations

import csv
import logging
import sys

import psycopg

from safety.config import CROSSWALK_DIR, MIGRATIONS_DIR, SEVERITY_DIR
from safety.db import connect, wait_for_db
from safety.h3grid import RESOLUTIONS, cell_for

log = logging.getLogger(__name__)

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migration (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply every unapplied db/migrations/*.sql in filename order."""
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(_MIGRATION_TABLE)
        cur.execute("SELECT filename FROM public.schema_migration")
        already = {r["filename"] for r in cur.fetchall()}
    conn.commit()

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already:
            continue
        log.info("applying migration %s", path.name)
        # Each migration is its own transaction, so a failure leaves the
        # preceding migrations applied and this one fully rolled back.
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO public.schema_migration (filename) VALUES (%s)", (path.name,)
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
    """Upsert every reference/crosswalk/*.csv into reference.offense_crosswalk."""
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
    "eb_prior_km2",
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
            float(row["eb_prior_km2"]),
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
                eb_prior_km2       = EXCLUDED.eb_prior_km2,
                self_weight        = EXCLUDED.self_weight,
                enabled            = EXCLUDED.enabled,
                notes              = EXCLUDED.notes
            """,
            payload,
        )
    conn.commit()
    log.info("loaded %s severity scheme(s) from %s", len(payload), path.name)
    return len(payload)


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
        WHERE occurred_local_hour IS NULL
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
WHERE occurred_local_hour IS NOT NULL
GROUP BY 1 ORDER BY 1
"""


def backfill_incident_hour(conn: psycopg.Connection) -> int:
    """Fill occurred_local_hour on rows loaded before the column existed.

    Reports what it measured and what it decided in every branch. A backfill
    that declines to run is a legitimate outcome here, but a silent one leaves
    the hourly layer empty with nothing to explain it.
    """
    with conn.cursor() as cur:
        cur.execute(_HOUR_AUDIT_SQL)
        audit = cur.fetchone() or {}

    total = audit.get("total") or 0
    if not total:
        log.info("no silver rows yet; nothing to backfill a clock hour onto")
        return 0
    if audit["already_filled"] == total:
        return 0

    def pct(n: int | None) -> float:
        return (n or 0) / total * 100

    log.info(
        "clock-hour audit over %s rows: %s already filled, %.1f%% flagged "
        "occurred_precision='exact', %.1f%% whose UTC date matches the local "
        "date, %.1f%% stamped exactly midnight, %s distinct hours present",
        total,
        audit["already_filled"],
        pct(audit["precision_exact"]),
        pct(audit["date_aligned"]),
        pct(audit["at_midnight"]),
        audit["distinct_hours"],
    )

    if (audit["distinct_hours"] or 0) <= 1:
        log.error(
            "occurred_at carries no time of day at all -- every row sits on the "
            "same hour -- so the clock hour cannot be recovered from it. Replay "
            "the stored snapshots (python -m safety.etl.run reprocess --city "
            "<city> --pull-id <id>) to read the hour from the source instead."
        )
        return 0

    aligned = (audit["date_aligned"] or 0) / total
    if aligned < _HOUR_ALIGNMENT_FLOOR:
        # Refusing is the right outcome. A wrong hour is worse than a missing
        # one: the hourly layer would render confidently and be shifted whole
        # hours, and nothing downstream could detect it.
        log.error(
            "occurred_at does not look like local wall-clock for %.1f%% of "
            "incidents, so the clock hour cannot be recovered from it. Leaving "
            "occurred_local_hour NULL; replay the bronze snapshots "
            "(python -m safety.etl.run reprocess) to read the hour from the source.",
            (1 - aligned) * 100,
        )
        return 0

    skip_midnight = (audit["at_midnight"] or 0) / total > _MIDNIGHT_SHARE_CEILING
    if skip_midnight:
        log.warning(
            "%.1f%% of incidents are stamped exactly midnight, far above the "
            "~4%% a real clock produces: that value is standing in for a time "
            "the source did not publish. Those rows stay NULL and are absent "
            "from the hourly layers rather than counted at 00:00.",
            pct(audit["at_midnight"]),
        )

    filled = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                _HOUR_BACKFILL_SQL,
                {"skip_midnight": skip_midnight, "batch": _BACKFILL_BATCH},
            )
            written = cur.rowcount
        conn.commit()
        if not written:
            break
        filled += written
        log.info("backfilled occurred_local_hour for %s row(s)", written)

    if not filled:
        log.warning("clock-hour backfill matched no rows; the hourly layers stay empty")
        return 0

    # Print the day the backfill produced. A plausible one dips through the
    # small hours and rises into the evening; a flat line, or one spike holding
    # everything, means the hour is not what it claims to be -- and this is the
    # only place that shape can be checked before it reaches a user.
    with conn.cursor() as cur:
        cur.execute(_HOUR_HISTOGRAM_SQL)
        rows = cur.fetchall()
    log.info(
        "hour distribution: %s",
        " ".join(f"{r['hour']:02d}:{r['n']}" for r in rows),
    )
    log.info(
        "clock-hour backfill filled %s row(s); re-run the gold rollups "
        "(python -m safety.etl.run hourly --city <city>) to build the hourly layers",
        filled,
    )
    return filled


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    wait_for_db()
    with connect() as conn:
        applied = apply_migrations(conn)
        # Schemes before weights (foreign key), and both before the registry
        # pointer they are referenced by.
        scheme_rows = load_severity_schemes(conn)
        weight_rows = load_severity_weights(conn)
        crosswalk_rows = load_crosswalks(conn)
        point_sources_at_scheme(conn)
        backfilled = backfill_h3_cells(conn)
        hours_filled = backfill_incident_hour(conn)

    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Schema already up to date.")
    print(f"Crosswalk rows loaded/refreshed: {crosswalk_rows}")
    print(f"Severity schemes: {scheme_rows}, severity weights: {weight_rows}")
    if backfilled:
        print(f"H3 cells backfilled: {backfilled} (re-run the gold rollups)")
    if hours_filled:
        print(f"Clock hours backfilled: {hours_filled} (re-run the gold rollups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
