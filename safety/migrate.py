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

    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Schema already up to date.")
    print(f"Crosswalk rows loaded/refreshed: {crosswalk_rows}")
    print(f"Severity schemes: {scheme_rows}, severity weights: {weight_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
