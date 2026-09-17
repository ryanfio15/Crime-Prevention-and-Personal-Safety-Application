-- 009: time of day -- hour-block rollups and a per-hour safety ranking.
--
-- The existing layers answer "how does this cell compare to the rest of the
-- city over the last N months". This one answers "and what about at 2am",
-- which is the question the resident/commuter segment in S2 actually asks
-- about a route home.
--
-- Two ratings come out of it, per S3.3's rule that a number shown to a user is
-- always a comparison against a named reference class:
--
--   1. the cell's safety percentile *within that hour*, ranked against every
--      other cell at the same hour, and
--   2. how that differs from the cell's own all-hours percentile -- whether
--      this place gets relatively better or worse at this time of day.
--
-- Everything is materialized here. The read path still never aggregates.

-- ---------------------------------------------------------------------------
-- The clock hour, carried on the incident
--
-- Nullable on purpose, and that is the whole design of this column. A record
-- with occurred_precision = 'date' has no clock time at all; defaulting those
-- to 0 would manufacture a midnight spike out of missing data and put it in
-- the one place a user is most likely to be looking. They stay NULL and are
-- excluded from every hourly rollup, with the excluded share published on the
-- city snapshot so the omission is visible rather than silent.
--
-- Philadelphia publishes the hour directly (`hour`, alongside `dispatch_time`),
-- so the adapter reads it from the source rather than deriving it. See
-- safety/etl/adapters/philadelphia.py::_parse_hour for why deriving it from
-- occurred_at would be wrong.
-- ---------------------------------------------------------------------------

ALTER TABLE etl.staging_incident
    ADD COLUMN IF NOT EXISTS occurred_local_hour smallint;

ALTER TABLE silver.incident
    ADD COLUMN IF NOT EXISTS occurred_local_hour smallint;

ALTER TABLE silver.incident
    ADD CONSTRAINT incident_local_hour_range
    CHECK (occurred_local_hour IS NULL OR occurred_local_hour BETWEEN 0 AND 23);

COMMENT ON COLUMN silver.incident.occurred_local_hour IS
    'Local clock hour 0-23 of occurred_at, as published by the source. NULL where the source gave no clock time (occurred_precision = ''date''); those rows are excluded from the hourly layers, never defaulted to 0.';


-- ---------------------------------------------------------------------------
-- Per-hour safety ranking
--
-- Same statistic as gold.cell_safety -- severity-weighted total, credibility
-- prior, ring-1 blend, Hazen midrank -- recomputed independently inside each
-- hour block, so a cell is ranked against other cells *at that hour* rather
-- than against the all-hours distribution.
--
-- That re-normalization is the point and also the trap. Everywhere is quieter
-- at 4am, so a cell holding its rank at 4am is not getting safer in absolute
-- terms; it is staying in the same place while the whole city gets quieter.
-- hour_index carries the absolute reading that the percentile cannot.
--
-- The time_window and h3_res CHECKs deliberately allow every value the
-- all-hours layer allows, even though safety/etl/gold.py currently builds only
-- the two widest windows at resolutions 8 and 9. Splitting 30 days across 24
-- buckets at 76 m leaves a median of zero, so it is not built -- but that is a
-- judgement about data volume, which belongs in the pipeline where it can be
-- changed, not frozen into DDL.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_hour_safety (
    source_id            text     NOT NULL,
    h3_index             text     NOT NULL,
    h3_res               smallint NOT NULL,
    time_window          text     NOT NULL
        CHECK (time_window IN ('last_30d', 'last_90d', 'last_12m', 'last_24m')),
    -- Block h covers [h:00, h+1:00) local. 23 is 23:00-24:00.
    hour_block           smallint NOT NULL CHECK (hour_block BETWEEN 0 AND 23),
    track                text     NOT NULL CHECK (track IN ('violent', 'non_violent')),
    scheme_version       text     NOT NULL REFERENCES reference.severity_scheme (scheme_version),

    window_start         date     NOT NULL,
    window_end           date     NOT NULL,

    incident_count       integer  NOT NULL,
    weighted_total       double precision NOT NULL,
    weighted_per_km2     double precision NOT NULL,
    smoothed_per_km2     double precision NOT NULL,

    -- Rating 1: against every other cell at this hour. 1.0 = safest.
    safety_percentile    double precision NOT NULL CHECK (safety_percentile BETWEEN 0 AND 1),
    safety_rank          integer  NOT NULL,
    city_cell_total      integer  NOT NULL,
    safety_tier          smallint NOT NULL CHECK (safety_tier BETWEEN 0 AND 4),

    -- Rating 2: against this cell's own all-hours standing.
    --
    -- Denormalized from gold.cell_safety rather than joined at read time, so
    -- serving one hour of the map stays a single-table scan. Nullable because
    -- the all-hours ranking may not have been built for this cell yet.
    baseline_percentile  double precision,
    -- safety_percentile - baseline_percentile, in percentile points. Positive
    -- means this cell ranks better at this hour than it usually does.
    percentile_delta     double precision,
    -- This cell's weighted total at this hour over its own mean hour, from the
    -- raw figures: 1.0 is an ordinary hour here, 2.5 is two and a half times
    -- its usual load. NULL where the cell carries too little over the window
    -- for the ratio to mean anything (safety.etl.gold.MIN_HOUR_EVIDENCE).
    hour_index           double precision,

    refreshed_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res, time_window, hour_block, track, scheme_version)
);

CREATE INDEX cell_hour_safety_layer_idx
    ON gold.cell_hour_safety (source_id, h3_res, time_window, hour_block, track, scheme_version)
    INCLUDE (h3_index, safety_percentile, safety_tier, percentile_delta, hour_index);

CREATE INDEX cell_hour_safety_cell_idx
    ON gold.cell_hour_safety (h3_index, time_window, scheme_version);

COMMENT ON COLUMN gold.cell_hour_safety.safety_percentile IS
    '0-1, 1.0 = safest. Hazen midrank against other cells in the same city at the same hour block -- a different reference class from gold.cell_safety.';
COMMENT ON COLUMN gold.cell_hour_safety.percentile_delta IS
    'safety_percentile minus the cell''s all-hours percentile. Relative movement only: the whole city is quieter at 4am, and this measure is blind to that.';
COMMENT ON COLUMN gold.cell_hour_safety.hour_index IS
    'Weighted total at this hour over this cell''s mean hour, on raw figures. The absolute reading percentile_delta cannot give.';


-- ---------------------------------------------------------------------------
-- Hourly profile per cell (sparse: rows only where count > 0)
--
-- Backs the 24-bar time-of-day chart in the detail panel and the "what is
-- actually reported here at this hour" breakdown, without the read path
-- touching silver. Same sparse-by-construction shape as gold.cell_monthly.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_hour_profile (
    source_id      text     NOT NULL,
    h3_index       text     NOT NULL,
    h3_res         smallint NOT NULL,
    time_window    text     NOT NULL,
    hour_block     smallint NOT NULL CHECK (hour_block BETWEEN 0 AND 23),
    category       text     NOT NULL
        CHECK (category IN ('all', 'violent', 'property', 'quality_of_life', 'other')),
    incident_count integer  NOT NULL CHECK (incident_count > 0),
    refreshed_at   timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res, time_window, hour_block, category)
);

CREATE INDEX cell_hour_profile_cell_idx
    ON gold.cell_hour_profile (h3_index, time_window, hour_block);


-- ---------------------------------------------------------------------------
-- How much of the data can answer the question at all.
--
-- S8.5's rule: a gap in the input is surfaced, not absorbed. This is the share
-- of a city's incidents carrying a usable clock hour, and the methodology page
-- reports it next to the hourly view rather than letting the view imply it
-- covers everything.
-- ---------------------------------------------------------------------------
ALTER TABLE gold.city_snapshot
    ADD COLUMN IF NOT EXISTS hour_known_share double precision;

COMMENT ON COLUMN gold.city_snapshot.hour_known_share IS
    'Share of incidents with a published clock hour. The remainder are absent from every hourly rollup.';
