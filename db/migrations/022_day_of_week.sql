-- 022: day of the week -- a per-cell day factor, the weekly twin of the hour
-- factor in gold.cell_hour_safety (009, 013).
--
-- The hour layer answers "and what about at 2am". This one answers "and on a
-- Saturday": each cell's weighted offence on each weekday, as a multiple of
-- its own average day, credibility-blended toward the city's weekly curve
-- (safety.etl.gold, _DOW_FACTOR_SQL). The seven factors of any one cell
-- average exactly 1, so multiplying one into a rate redistributes the week
-- without inflating or deflating it.
--
-- Only the factor is stored, not a ranking. The map combines it with the hour
-- (all-hours or hour-block rate x day factor) and ranks the result when asked
-- (safety.api.repository, the `timed` CTEs): storing every hour-and-day pair
-- would multiply gold.cell_hour_safety, already the largest table, by seven.
-- Ranking one city's cells on request is what a custom date range already
-- does (020).
--
-- Every incident has a local date, including the ones published with no clock
-- time, so unlike the hour layer this one loses nothing to missing hours.
--
-- Built where the hour layer is (HOURLY_RESOLUTIONS, HOURLY_WINDOWS) and by
-- the same weekly run. Expand-only: nothing earlier reads it, and until a
-- city's hourly layers are rebuilt it holds no rows, which the serving layer
-- reads as a factor of 1 -- the map without the day.

CREATE TABLE IF NOT EXISTS gold.cell_dow_factor (
    source_id       text     NOT NULL,
    h3_index        text     NOT NULL,
    h3_res          smallint NOT NULL,
    time_window     text     NOT NULL,
    -- 0 = Monday ... 6 = Sunday, the local weekday of occurred_local_date.
    dow             smallint NOT NULL CHECK (dow BETWEEN 0 AND 6),
    track           text     NOT NULL CHECK (track IN ('violent', 'non_violent')),
    scheme_version  text     NOT NULL REFERENCES reference.severity_scheme (scheme_version),

    window_start    date     NOT NULL,
    window_end      date     NOT NULL,

    -- This cell's incidents and severity-weighted total on this weekday.
    incident_count  integer  NOT NULL,
    weighted_total  double precision NOT NULL,
    -- This weekday's weighted offence here over this cell's average day, per
    -- occurrence of the weekday in the window, blended toward the city's
    -- curve. 1.0 is an ordinary day for this cell.
    day_factor      double precision NOT NULL CHECK (day_factor >= 0),

    refreshed_at    timestamptz NOT NULL DEFAULT now(),

    -- Layer first: the map reads one city, size, window, scheme and weekday.
    PRIMARY KEY (source_id, h3_res, time_window, scheme_version, dow, track, h3_index)
);

-- Route scoring and the cell popup look cells up by index.
CREATE INDEX IF NOT EXISTS cell_dow_factor_cell_idx
    ON gold.cell_dow_factor (h3_index, time_window, scheme_version);

COMMENT ON TABLE gold.cell_dow_factor IS
    'Per-cell weekday factor (res 8, 9; the hourly windows): weighted offence on that weekday over the cell''s average day, blended toward the city''s weekly curve. Multiplied into a rate and ranked at request time.';
