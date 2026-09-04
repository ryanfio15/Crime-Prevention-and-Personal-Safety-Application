"""Schema migrations and reference-data loading.

Run with:  python -m safety.migrate
"""

from __future__ import annotations

import csv
import logging
import sys

import psycopg

from safety.config import CROSSWALK_DIR, MIGRATIONS_DIR
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    wait_for_db()
    with connect() as conn:
        applied = apply_migrations(conn)
        crosswalk_rows = load_crosswalks(conn)

    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Schema already up to date.")
    print(f"Crosswalk rows loaded/refreshed: {crosswalk_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
