-- 020: per-cell daily rollup, so the map can be drawn for any date range.
--
-- Every other gold layer is precomputed per trailing window (018). A range the
-- user picks on a calendar -- two dates, or a single day -- matches none of
-- them, so the serving layer ranks it on request instead: it sums this table
-- over the range and runs the same ranking SQL the ETL runs for the stored
-- windows (safety/ranking_sql.py). The read path still touches gold only.
--
-- Sparse: a row only where a cell had at least one incident that day. The
-- zero-incident cells the ranking needs come from gold.cell_geometry, exactly
-- as they do for the stored windows.
--
-- Resolutions 8 and 9 only (safety.etl.gold.DAILY_RESOLUTIONS). At resolution
-- 10 nearly every incident is its own (cell, day), so it would roughly double
-- the table for a cell size that is only served over a year or more anyway;
-- there the map keeps to the stored windows.
--
-- Expand-only. Nothing earlier reads or writes it; until a city's gold is
-- rebuilt it holds no rows, gold.city_snapshot.selectable_start stays NULL,
-- and the API refuses a custom range for that city rather than serving zeros.

CREATE TABLE IF NOT EXISTS gold.cell_daily (
    source_id          text     NOT NULL,
    h3_res             smallint NOT NULL,
    day                date     NOT NULL,
    h3_index           text     NOT NULL,

    c_all              integer  NOT NULL CHECK (c_all > 0),
    c_violent          integer  NOT NULL,
    c_property         integer  NOT NULL,
    c_quality_of_life  integer  NOT NULL,
    c_other            integer  NOT NULL,

    -- Severity-weighted sums under gold.city_snapshot.daily_scheme_version.
    w_violent          double precision NOT NULL,
    w_non_violent      double precision NOT NULL,

    -- Day before cell: a range is one contiguous slice of the index per
    -- resolution, whatever the cells.
    PRIMARY KEY (source_id, h3_res, day, h3_index)
);

COMMENT ON TABLE gold.cell_daily IS
    'Sparse per-cell per-day incident counts and severity-weighted sums (res 8, 9), summed by the API to rank a custom date range.';

ALTER TABLE gold.city_snapshot
    -- The oldest date a custom range may start on: the oldest stored incident
    -- on or after the city's history floor (safety.etl.gold.history_floor), so
    -- a cold case published with a decades-old date does not stretch the
    -- calendar back to it. NULL until the city is rebuilt with the daily rollup.
    ADD COLUMN IF NOT EXISTS selectable_start date,
    -- The scheme gold.cell_daily's weights were summed under: the city's active
    -- scheme at build time, NULL if it had none. The API ranks a custom range
    -- only while this still matches the active scheme.
    ADD COLUMN IF NOT EXISTS daily_scheme_version text;
