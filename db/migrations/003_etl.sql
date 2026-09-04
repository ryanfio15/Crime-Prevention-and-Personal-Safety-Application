-- 003: pipeline bookkeeping -- pull manifests, validation issues, staging.

-- ---------------------------------------------------------------------------
-- Pull run manifest
--
-- The bronze layer itself is object storage (S9.2); this table is the index
-- into it, answering "what did the city publish on this date, and where is the
-- snapshot" for auditability and reprocessing (S5).
-- ---------------------------------------------------------------------------
CREATE TABLE etl.pull_run (
    pull_id            bigserial   PRIMARY KEY,
    source_id          text        NOT NULL REFERENCES reference.source_registry (source_id),
    dataset            text        NOT NULL,
    mode               text        NOT NULL CHECK (mode IN ('backfill', 'incremental', 'boundary')),
    status             text        NOT NULL
        CHECK (status IN ('running', 'succeeded', 'failed', 'blocked', 'no_new_data')),

    -- S8.3: the "since" filter this run used, and the window it actually covered.
    since_watermark    timestamptz,
    window_start       timestamptz,
    window_end         timestamptz,

    -- Pointer to the raw snapshot in the bronze store.
    bronze_uri         text,
    bronze_bytes       bigint,

    records_fetched    integer     NOT NULL DEFAULT 0,
    records_rejected   integer     NOT NULL DEFAULT 0,
    records_valid      integer     NOT NULL DEFAULT 0,
    records_upserted   integer     NOT NULL DEFAULT 0,

    crosswalk_version  text,
    pipeline_version   text        NOT NULL,

    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    duration_seconds   double precision,
    error              text
);

CREATE INDEX pull_run_source_started_idx ON etl.pull_run (source_id, started_at DESC);

COMMENT ON TABLE etl.pull_run IS
    'Manifest of every raw pull. Points at the bronze snapshot (S9.2) that produced a given silver load.';


-- ---------------------------------------------------------------------------
-- Validation issues (design doc S8.5)
--
-- The known failure modes of these six sources, recorded rather than silently
-- swallowed. Note in particular that an offense code missing from the current
-- crosswalk version is flagged for manual review, NOT dropped.
-- ---------------------------------------------------------------------------
CREATE TABLE etl.validation_issue (
    issue_id           bigserial   PRIMARY KEY,
    pull_id            bigint      NOT NULL REFERENCES etl.pull_run (pull_id) ON DELETE CASCADE,
    source_id          text        NOT NULL,
    check_name         text        NOT NULL
        CHECK (check_name IN (
            'missing_coordinates',      -- LA and DC explicitly publish (0,0) rows
            'coordinates_out_of_bounds',
            'coordinates_reprojected',  -- source published a projected CRS, not WGS84
            'missing_timestamp',
            'future_timestamp',
            'duplicate_incident_id',    -- duplicates within a single pull
            'unmapped_offense_code',    -- flag for crosswalk review, keep the record
            'volume_anomaly'            -- record count far outside the norm for this source
        )),
    severity           text        NOT NULL
        CHECK (severity IN ('reject', 'warn', 'block')),
    source_incident_id text,
    occurrences        integer     NOT NULL DEFAULT 1,
    detail             jsonb,
    detected_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX validation_issue_pull_idx  ON etl.validation_issue (pull_id, check_name);
CREATE INDEX validation_issue_check_idx ON etl.validation_issue (source_id, check_name, detected_at DESC);

COMMENT ON COLUMN etl.validation_issue.severity IS
    'reject = row kept out of silver; warn = row loaded but flagged (S8.5 unmapped codes); block = pull aborted.';


-- ---------------------------------------------------------------------------
-- Staging table
--
-- COPY target for a single pull. Deliberately holds raw-ish values: the
-- crosswalk is applied by a SQL join against reference.offense_crosswalk on
-- promotion to silver, so classification logic lives in the versioned
-- reference dataset rather than in Python (S7.2).
-- ---------------------------------------------------------------------------
CREATE UNLOGGED TABLE etl.staging_incident (
    pull_id             bigint  NOT NULL,
    source_id           text    NOT NULL,
    source_incident_id  text    NOT NULL,
    incident_key        text    NOT NULL,
    source_dataset      text    NOT NULL,
    occurred_at         timestamptz NOT NULL,
    occurred_local_date date    NOT NULL,
    occurred_precision  text    NOT NULL,
    occurred_basis      text    NOT NULL,
    reported_at         timestamptz,
    latitude            double precision NOT NULL,
    longitude           double precision NOT NULL,
    coordinate_source   text    NOT NULL,
    h3_r8               text    NOT NULL,
    h3_r9               text    NOT NULL,
    raw_offense_code    text,
    raw_offense_text    text,
    raw_source_category text,
    location_type       text,
    location_block      text,
    district            text
);

CREATE INDEX staging_incident_pull_idx ON etl.staging_incident (pull_id);
