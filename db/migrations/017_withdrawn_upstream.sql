-- 017: incidents withdrawn upstream (F13).
--
-- An incremental pull re-reads each source's revision window. A record that
-- silver holds for that window but the re-pull no longer returns was withdrawn
-- (or moved out of the window) upstream; safety/etl/withdrawn.py records that as
-- a `withdrawn_upstream` validation issue and, where the instance enables it
-- (WITHDRAWN_RECONCILE=delete), deletes it behind an outage guard -- copying
-- every deleted row into etl.withdrawn_incident first, for exact recovery
-- (restore SQL in docs/DEPLOY.md "Operating the data").
--
-- Expand-only: the previous release never writes either, so it keeps working on
-- this schema (migrations are not undone by a rollback).

-- Split DROP and ADD so the new check is added NOT VALID (brief ACCESS
-- EXCLUSIVE lock, no scan) and validated separately (SHARE UPDATE EXCLUSIVE).
ALTER TABLE etl.validation_issue
    DROP CONSTRAINT IF EXISTS validation_issue_check_name_check;
ALTER TABLE etl.validation_issue
    ADD CONSTRAINT validation_issue_check_name_check CHECK (check_name IN (
        'missing_coordinates',
        'coordinates_out_of_bounds',
        'coordinates_reprojected',
        'missing_timestamp',
        'future_timestamp',
        'duplicate_incident_id',
        'unmapped_offense_code',
        'volume_anomaly',
        'withdrawn_upstream'        -- in silver for the revision window, absent from its re-pull (F13)
    )) NOT VALID;
ALTER TABLE etl.validation_issue VALIDATE CONSTRAINT validation_issue_check_name_check;

CREATE TABLE IF NOT EXISTS etl.withdrawn_incident (
    pull_id             bigint      NOT NULL,   -- no FK: the archive must outlive any pull_run pruning
    source_id           text        NOT NULL,
    incident_key        text        NOT NULL,
    occurred_local_date date        NOT NULL,
    withdrawn_at        timestamptz NOT NULL DEFAULT now(),
    row                 jsonb       NOT NULL,   -- to_jsonb(silver row) minus geom (rebuilt from lat/lng on restore)
    PRIMARY KEY (pull_id, incident_key)
);
-- Serves the 90-day retention prune (withdrawn.prune) and per-city lookups.
CREATE INDEX IF NOT EXISTS withdrawn_incident_source_idx
    ON etl.withdrawn_incident (source_id, withdrawn_at DESC);

COMMENT ON TABLE etl.withdrawn_incident IS
    'Silver rows deleted because a revision-window re-pull no longer returned them (F13); '
    'kept WITHDRAWN_RETENTION_DAYS (default 90); restore SQL in docs/DEPLOY.md.';
