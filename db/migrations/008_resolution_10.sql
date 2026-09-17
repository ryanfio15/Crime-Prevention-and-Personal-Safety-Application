-- 008: add H3 resolution 10 as a third, finer drill-down (design doc S3.2).
--
-- Resolution 8 (~530 m) is the product resolution and 9 (~200 m) the existing
-- drill-down. Resolution 10 is ~76 m across, roughly half a city block, and is
-- deliberately the last one added: Philadelphia publishes block-level
-- coordinates, so anything finer would render the rounding in the source data
-- rather than the distribution of crime, which S13 rules out. Resolutions 11
-- (~29 m) and 12 (~11 m) were measured and rejected on those grounds, and
-- because a single whole-city map request at those sizes is 97 MB and 681 MB
-- respectively against 14 MB here.
--
-- The column is nullable on purpose. H3 is computed in Python (there is no H3
-- extension in the database), so existing rows are filled by the backfill in
-- safety/migrate.py rather than by this migration.

ALTER TABLE etl.staging_incident
    ADD COLUMN IF NOT EXISTS h3_r10 text;

ALTER TABLE silver.incident
    ADD COLUMN IF NOT EXISTS h3_r10 text;

-- Declared on the parent, so it cascades to every partition, existing and
-- future -- matching incident_h3_r8_idx / incident_h3_r9_idx in 004.
CREATE INDEX IF NOT EXISTS incident_h3_r10_idx
    ON silver.incident (source_id, h3_r10, occurred_local_date);

COMMENT ON COLUMN silver.incident.h3_r10 IS
    'S3.2 fine drill-down cell (~76 m). Nullable only until the one-time backfill in safety/migrate.py has run.';

ALTER TABLE gold.city_snapshot
    ADD COLUMN IF NOT EXISTS cell_count_r10 integer NOT NULL DEFAULT 0;
