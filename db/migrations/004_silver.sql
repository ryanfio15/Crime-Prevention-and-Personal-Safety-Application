-- 004: silver layer -- the standardized incident table (design doc S6).
--
-- Partitioned by city (LIST) and then by year (RANGE), per S9.1: keeps
-- partitions small, makes reprocessing one city's history low-risk, and
-- matches the natural per-year partitioning several upstream sources already
-- publish. Partitions are created on demand by safety/db.py::ensure_partitions
-- so that onboarding a seventh city needs no DDL change (S11).

CREATE TABLE silver.incident (
    -- Partition keys first.
    source_id            text        NOT NULL REFERENCES reference.source_registry (source_id),
    occurred_year        smallint    NOT NULL,

    -- S6: synthetic globally unique key, since raw IDs collide across cities.
    incident_key         text        NOT NULL,
    source_incident_id   text        NOT NULL,
    source_dataset       text        NOT NULL,
    source_pull_id       bigint      NOT NULL,

    -- S6: UTC-normalized timestamp with an explicit precision flag.
    occurred_at          timestamptz NOT NULL,
    occurred_local_date  date        NOT NULL,
    occurred_precision   text        NOT NULL
        CHECK (occurred_precision IN ('exact', 'date', 'period')),
    -- What the timestamp actually measures. Philadelphia publishes dispatch
    -- time, not observed occurrence time -- worth carrying explicitly rather
    -- than letting the UI imply more than the data supports (S13).
    occurred_basis       text        NOT NULL
        CHECK (occurred_basis IN ('occurrence', 'dispatch', 'report')),
    reported_at          timestamptz,

    -- S6: coordinates as published (already block-truncated upstream), in WGS84.
    latitude             double precision NOT NULL,
    longitude            double precision NOT NULL,
    geom                 geometry(Point, 4326) NOT NULL,
    -- Provenance for the coordinate pair. A small share of Philadelphia records
    -- are published in PA State Plane feet rather than WGS84; the adapter
    -- reprojects them, and this column records that it did so rather than
    -- letting a silently converted value look like a published one.
    coordinate_source    text        NOT NULL DEFAULT 'published_wgs84',

    -- S6/S3.2: both resolutions computed once at ingestion, never at query time.
    h3_r8                text        NOT NULL,
    h3_r9                text        NOT NULL,

    -- S7.4: the source's own classification, always preserved.
    raw_offense_code     text,
    raw_offense_text     text,
    raw_source_category  text,

    -- S7: standardized classification, as of crosswalk_version.
    nibrs_code           text,
    nibrs_offense_name   text,
    nibrs_group          text,
    nibrs_crime_against  text,
    ucr_part             text,
    severity_bucket      text        NOT NULL,
    product_category     text        NOT NULL
        CHECK (product_category IN ('violent', 'property', 'quality_of_life', 'other')),
    mapping_confidence   text        NOT NULL,

    -- S7.5: lower-confidence, rules-based bucketing; 'unknown' where the source
    -- publishes no location field at all (Philadelphia does not).
    location_type        text        NOT NULL DEFAULT 'unknown',
    location_block       text,
    district             text,

    -- S6: reproducibility metadata.
    crosswalk_version    text        NOT NULL,
    pipeline_version     text        NOT NULL,
    ingested_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT incident_pk PRIMARY KEY (source_id, occurred_year, incident_key)
) PARTITION BY LIST (source_id);

-- Indexes declared on the parent cascade to every partition, existing and future.
CREATE INDEX incident_h3_r8_idx     ON silver.incident (source_id, h3_r8, occurred_local_date);
CREATE INDEX incident_h3_r9_idx     ON silver.incident (source_id, h3_r9, occurred_local_date);
CREATE INDEX incident_occurred_idx  ON silver.incident (source_id, occurred_at);
-- Gold time windows are expressed in the city's local dates, so the rollup
-- scans filter on occurred_local_date rather than the UTC timestamp.
CREATE INDEX incident_local_date_idx ON silver.incident (source_id, occurred_local_date)
    INCLUDE (h3_r8, h3_r9, product_category);
CREATE INDEX incident_category_idx  ON silver.incident (source_id, product_category, occurred_local_date);
-- Lookup by key alone, for the cross-year revision cleanup in transform.py
-- (the primary key leads with occurred_year, so it cannot serve this).
CREATE INDEX incident_key_idx       ON silver.incident (source_id, incident_key);
CREATE INDEX incident_pull_idx      ON silver.incident (source_id, source_pull_id);
CREATE INDEX incident_geom_gix      ON silver.incident USING GIST (geom);

COMMENT ON TABLE silver.incident IS
    'S6 canonical incident. Everything downstream of here is written in terms of this schema and H3 cells only (S11).';
COMMENT ON COLUMN silver.incident.incident_key IS
    'Synthetic global key, "<source_id>:<source_incident_id>". Upsert target for idempotent loads (S8.3).';
COMMENT ON COLUMN silver.incident.mapping_confidence IS
    'exact/approximate/ambiguous per crosswalk, or "unmapped" when the raw code was absent from the crosswalk version (S8.5).';
