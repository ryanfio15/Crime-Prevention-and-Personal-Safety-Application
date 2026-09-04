"""Bronze -> silver promotion (design doc S6, S7, S8.3).

Two deliberate placements:

* H3 indexes at both stored resolutions are computed **here**, in Python, once
  per record at ingestion -- never at query time (S6, S3.2).
* The offense crosswalk is applied by a SQL join against the versioned
  reference.offense_crosswalk table, not by Python dictionaries, because S7.2
  requires the mapping to be a maintained reference dataset rather than
  application code -- and because S7.3 requires selecting the mapping that was
  in effect on the incident's own date.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Jsonb

from safety.db import ensure_partitions
from safety.etl.adapters.base import NormalizedIncident
from safety.h3grid import cells_for_point

log = logging.getLogger(__name__)

_STAGING_COLUMNS = (
    "pull_id",
    "source_id",
    "source_incident_id",
    "incident_key",
    "source_dataset",
    "occurred_at",
    "occurred_local_date",
    "occurred_precision",
    "occurred_basis",
    "reported_at",
    "latitude",
    "longitude",
    "coordinate_source",
    "h3_r8",
    "h3_r9",
    "raw_offense_code",
    "raw_offense_text",
    "raw_source_category",
    "location_type",
    "location_block",
    "district",
)


def stage_records(
    conn: psycopg.Connection,
    *,
    pull_id: int,
    source_id: str,
    dataset: str,
    records: list[NormalizedIncident],
) -> int:
    """COPY validated records into the staging table, computing H3 en route."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM etl.staging_incident WHERE pull_id = %s", (pull_id,))

        copy_sql = (
            f"COPY etl.staging_incident ({', '.join(_STAGING_COLUMNS)}) FROM STDIN"
        )
        with cur.copy(copy_sql) as copy:
            for record in records:
                # Validation guarantees coordinates are present by this point.
                cells = cells_for_point(record.latitude, record.longitude)
                copy.write_row(
                    (
                        pull_id,
                        source_id,
                        record.source_incident_id,
                        f"{source_id}:{record.source_incident_id}",
                        dataset,
                        record.occurred_at,
                        record.occurred_local_date,
                        record.occurred_precision,
                        record.occurred_basis,
                        record.reported_at,
                        record.latitude,
                        record.longitude,
                        record.coordinate_source,
                        cells[8],
                        cells[9],
                        record.raw_offense_code,
                        record.raw_offense_text,
                        record.raw_source_category,
                        record.location_type,
                        record.location_block,
                        record.district,
                    )
                )
    log.info("staged %s records for pull %s", len(records), pull_id)
    return len(records)


_PROMOTE_SQL = """
INSERT INTO silver.incident (
    source_id, occurred_year, incident_key, source_incident_id, source_dataset,
    source_pull_id, occurred_at, occurred_local_date, occurred_precision,
    occurred_basis, reported_at, latitude, longitude, coordinate_source, geom,
    h3_r8, h3_r9, raw_offense_code, raw_offense_text, raw_source_category,
    nibrs_code, nibrs_offense_name, nibrs_group, nibrs_crime_against, ucr_part,
    severity_bucket, product_category, mapping_confidence,
    location_type, location_block, district,
    crosswalk_version, pipeline_version, ingested_at
)
SELECT
    s.source_id,
    EXTRACT(YEAR FROM s.occurred_local_date)::smallint,
    s.incident_key,
    s.source_incident_id,
    s.source_dataset,
    s.pull_id,
    s.occurred_at,
    s.occurred_local_date,
    s.occurred_precision,
    s.occurred_basis,
    s.reported_at,
    s.latitude,
    s.longitude,
    s.coordinate_source,
    ST_SetSRID(ST_MakePoint(s.longitude, s.latitude), 4326),
    s.h3_r8,
    s.h3_r9,
    s.raw_offense_code,
    s.raw_offense_text,
    s.raw_source_category,
    x.nibrs_code,
    x.nibrs_offense_name,
    x.nibrs_group,
    x.nibrs_crime_against,
    x.ucr_part,
    -- S8.5: an unmapped code is flagged, never dropped, and never guessed at.
    COALESCE(x.severity_bucket, 'unknown'),
    COALESCE(x.product_category, 'other'),
    COALESCE(x.mapping_confidence, 'unmapped'),
    s.location_type,
    s.location_block,
    s.district,
    %(crosswalk_version)s,
    %(pipeline_version)s,
    now()
FROM etl.staging_incident s
LEFT JOIN LATERAL (
    -- S7.3: pick the crosswalk row that was in effect on the incident's own
    -- date, so an upstream RMS migration does not retroactively rewrite how
    -- older records were classified.
    SELECT c.*
    FROM reference.offense_crosswalk c
    WHERE c.source_id           = s.source_id
      AND c.crosswalk_version   = %(crosswalk_version)s
      AND c.raw_offense_code    = s.raw_offense_code
      AND c.raw_offense_text_key = upper(btrim(s.raw_offense_text))
      AND c.effective_from      <= s.occurred_local_date
      AND (c.effective_to IS NULL OR c.effective_to > s.occurred_local_date)
    ORDER BY c.effective_from DESC
    LIMIT 1
) x ON true
WHERE s.pull_id = %(pull_id)s
ON CONFLICT (source_id, occurred_year, incident_key) DO UPDATE SET
    -- S8.3: loads are idempotent and agencies revise records after publication,
    -- so a re-seen incident overwrites rather than duplicating.
    source_incident_id  = EXCLUDED.source_incident_id,
    source_dataset      = EXCLUDED.source_dataset,
    source_pull_id      = EXCLUDED.source_pull_id,
    occurred_at         = EXCLUDED.occurred_at,
    occurred_local_date = EXCLUDED.occurred_local_date,
    occurred_precision  = EXCLUDED.occurred_precision,
    occurred_basis      = EXCLUDED.occurred_basis,
    reported_at         = EXCLUDED.reported_at,
    latitude            = EXCLUDED.latitude,
    longitude           = EXCLUDED.longitude,
    coordinate_source   = EXCLUDED.coordinate_source,
    geom                = EXCLUDED.geom,
    h3_r8               = EXCLUDED.h3_r8,
    h3_r9               = EXCLUDED.h3_r9,
    raw_offense_code    = EXCLUDED.raw_offense_code,
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
    location_type       = EXCLUDED.location_type,
    location_block      = EXCLUDED.location_block,
    district            = EXCLUDED.district,
    crosswalk_version   = EXCLUDED.crosswalk_version,
    pipeline_version    = EXCLUDED.pipeline_version,
    ingested_at         = EXCLUDED.ingested_at
"""

# A revision can move an incident's date across a year boundary, which would
# leave the old row stranded in the previous year partition (the primary key
# includes occurred_year, so ON CONFLICT cannot see it).
_DEDUPE_ACROSS_YEARS_SQL = """
DELETE FROM silver.incident i
USING etl.staging_incident s
WHERE i.source_id     = s.source_id
  AND i.incident_key  = s.incident_key
  AND s.pull_id       = %(pull_id)s
  AND i.occurred_year <> EXTRACT(YEAR FROM s.occurred_local_date)::smallint
"""


def promote_to_silver(
    conn: psycopg.Connection,
    *,
    pull_id: int,
    source_id: str,
    crosswalk_version: str,
    pipeline_version: str,
) -> int:
    """Apply the crosswalk and upsert staged records into silver."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT EXTRACT(YEAR FROM occurred_local_date)::int AS year
            FROM etl.staging_incident WHERE pull_id = %s
            """,
            (pull_id,),
        )
        years = [row["year"] for row in cur.fetchall()]

    if not years:
        return 0

    ensure_partitions(conn, source_id, years)

    params = {
        "pull_id": pull_id,
        "crosswalk_version": crosswalk_version,
        "pipeline_version": pipeline_version,
    }
    with conn.cursor() as cur:
        cur.execute(_PROMOTE_SQL, params)
        upserted = cur.rowcount
        cur.execute(_DEDUPE_ACROSS_YEARS_SQL, {"pull_id": pull_id})
        moved = cur.rowcount
    if moved:
        log.info("removed %s stale rows whose date moved across a year boundary", moved)
    log.info("promoted %s records to silver", upserted)
    return upserted


def flag_unmapped_offenses(
    conn: psycopg.Connection, *, pull_id: int, source_id: str, crosswalk_version: str
) -> int:
    """S8.5: raise a reviewable warning for codes absent from the crosswalk.

    The records are already in silver -- they are classified as 'other' /
    'unmapped' rather than discarded, so a taxonomy gap never silently deletes
    reported incidents.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_offense_code, raw_offense_text, count(*) AS n
            FROM silver.incident
            WHERE source_id = %s AND source_pull_id = %s AND mapping_confidence = 'unmapped'
            GROUP BY 1, 2
            ORDER BY n DESC
            """,
            (source_id, pull_id),
        )
        unmapped = cur.fetchall()

    if not unmapped:
        return 0

    total = sum(row["n"] for row in unmapped)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO etl.validation_issue
                (pull_id, source_id, check_name, severity, occurrences, detail)
            VALUES (%s, %s, 'unmapped_offense_code', 'warn', %s, %s)
            """,
            (
                pull_id,
                source_id,
                total,
                Jsonb(
                    {
                        "crosswalk_version": crosswalk_version,
                        "action": "records loaded and flagged; add mappings and reprocess",
                        "distinct_codes": len(unmapped),
                        "codes": [
                            {
                                "raw_offense_code": row["raw_offense_code"],
                                "raw_offense_text": row["raw_offense_text"],
                                "count": row["n"],
                            }
                            for row in unmapped[:25]
                        ],
                    }
                ),
            ),
        )
    log.warning(
        "%s records across %s offense code(s) are not in crosswalk %s",
        total,
        len(unmapped),
        crosswalk_version,
    )
    return total


def clear_staging(conn: psycopg.Connection, pull_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM etl.staging_incident WHERE pull_id = %s", (pull_id,))
