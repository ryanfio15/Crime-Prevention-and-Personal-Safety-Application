-- 018: per-city history windows (30 days, 3/6/9 months, then 1, 2, 3 ... years
-- back to the oldest stored incident) and the bookkeeping for loading a city's
-- full published history.
--
-- Expand-only. The previous release reads and writes last_30d / last_90d /
-- last_12m / last_24m; the new CHECKs still accept those names, and the new
-- release keeps writing them as copies of last_3m / last_1y / last_2y for one
-- release (safety.etl.gold, settings.gold_legacy_windows), so a rollback still
-- has a map to serve. A later contract migration drops the legacy names.

-- ---------------------------------------------------------------------------
-- Window names: one pattern instead of a fixed list, because the list now
-- depends on how much history each city holds.
--
-- The original CHECKs were declared inline (005, 007, 009), so their names are
-- whatever Postgres generated. Drop every CHECK on these tables that mentions
-- time_window rather than guessing the name, then add one named CHECK each.
-- NOT VALID then VALIDATE, as in 017: a brief ACCESS EXCLUSIVE for the ALTER,
-- and the scan under SHARE UPDATE EXCLUSIVE, which readers do not wait on.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    c record;
BEGIN
    FOR c IN
        SELECT con.conrelid::regclass AS tbl, con.conname
        FROM pg_constraint con
        WHERE con.contype = 'c'
          AND con.conrelid IN (
                'gold.cell_activity'::regclass,
                'gold.cell_safety'::regclass,
                'gold.cell_hour_safety'::regclass)
          AND pg_get_constraintdef(con.oid) LIKE '%time_window%'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
    END LOOP;
END
$$;

ALTER TABLE gold.cell_activity ADD CONSTRAINT cell_activity_time_window_check
    CHECK (time_window ~ '^last_(30d|[369]m|[1-9][0-9]?y|90d|12m|24m)$') NOT VALID;
ALTER TABLE gold.cell_activity VALIDATE CONSTRAINT cell_activity_time_window_check;

ALTER TABLE gold.cell_safety ADD CONSTRAINT cell_safety_time_window_check
    CHECK (time_window ~ '^last_(30d|[369]m|[1-9][0-9]?y|90d|12m|24m)$') NOT VALID;
ALTER TABLE gold.cell_safety VALIDATE CONSTRAINT cell_safety_time_window_check;

ALTER TABLE gold.cell_hour_safety ADD CONSTRAINT cell_hour_safety_time_window_check
    CHECK (time_window ~ '^last_(30d|[369]m|[1-9][0-9]?y|90d|12m|24m)$') NOT VALID;
ALTER TABLE gold.cell_hour_safety VALIDATE CONSTRAINT cell_hour_safety_time_window_check;


-- ---------------------------------------------------------------------------
-- The windows each city is actually built for, written by the gold refresh in
-- the same transaction as the layers, so the API never offers a window whose
-- rows are not there. A city with no rows here has not been rebuilt since this
-- migration and is still served the four legacy windows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.city_window (
    source_id      text        NOT NULL,
    time_window    text        NOT NULL
        CHECK (time_window ~ '^last_(30d|[369]m|[1-9][0-9]?y)$'),
    sort_order     smallint    NOT NULL,
    window_start   date        NOT NULL,
    window_end     date        NOT NULL,
    -- Later than window_start when the city's stored history begins inside the
    -- window (`partial`): "last 3 years" over two years and eleven months of data.
    data_start     date        NOT NULL,
    partial        boolean     NOT NULL,
    -- What is built for this window. The API offers the window either way and
    -- says which of these is missing rather than serving an empty layer.
    safety_built   boolean     NOT NULL,
    hourly_built   boolean     NOT NULL,
    res10_built    boolean     NOT NULL,
    refreshed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, time_window)
);

COMMENT ON TABLE gold.city_window IS
    'Per-city list of built time windows (30d, 3/6/9m, 1..N years), rewritten with each gold refresh.';


-- ---------------------------------------------------------------------------
-- Periods in a city's published history that were recorded or classified
-- differently from today, shown with any window that reaches back into them.
-- Comparing places inside one window stays fair; comparing one period with
-- another across the break is what the caveat warns about.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reference.source_series_caveat (
    source_id    text NOT NULL REFERENCES reference.source_registry (source_id),
    period_from  date NOT NULL,
    period_to    date NOT NULL,             -- exclusive
    kind         text NOT NULL
        CHECK (kind IN ('classification', 'counting', 'coverage')),
    caveat_text  text NOT NULL,
    PRIMARY KEY (source_id, period_from, kind),
    CHECK (period_to > period_from)
);

INSERT INTO reference.source_series_caveat (source_id, period_from, period_to, kind, caveat_text)
SELECT v.source_id, v.period_from, v.period_to, v.kind, v.caveat_text
FROM (VALUES
    ('sea', DATE '2008-01-01', DATE '2019-05-01', 'counting',
     'Seattle records before May 2019 come from SPD''s previous records system and were converted to NIBRS codes afterwards. Each report then listed fewer separate offenses, so counts before mid-2019 run somewhat lower than today''s for the same activity.'),
    ('dc',  DATE '2008-01-01', DATE '2009-01-01', 'classification',
     'Washington DC''s 2008 records do not separate theft from vehicles from other theft; both are counted as other theft for that year.')
) AS v(source_id, period_from, period_to, kind, caveat_text)
WHERE EXISTS (SELECT 1 FROM reference.source_registry r WHERE r.source_id = v.source_id)
ON CONFLICT DO NOTHING;


-- ---------------------------------------------------------------------------
-- How far back a city's history is loaded, separately from the 24-month
-- backfill an incremental falls back to. `history_enabled` is off until the
-- operator turns it on per city and instance (docs/OPERATIONS.md).
-- ---------------------------------------------------------------------------
ALTER TABLE reference.source_registry
    ADD COLUMN IF NOT EXISTS history_start_date date,
    ADD COLUMN IF NOT EXISTS history_enabled    boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN reference.source_registry.history_start_date IS
    'Oldest date the history load reaches; also the floor gold uses for the oldest window, so one misdated record cannot add decades of windows.';

-- Earliest comparable data per city (research 2026-10-08): the current dataset
-- and crosswalk reach back this far with the same codes. Austin and Los Angeles
-- stay at their current floors: Austin's older records have no coordinates, and
-- Los Angeles' are in other datasets with their own codes (a later phase).
UPDATE reference.source_registry r SET history_start_date = v.d
FROM (VALUES
    ('phl', DATE '2006-01-01'),
    ('chi', DATE '2001-01-01'),
    ('dc',  DATE '2008-01-01'),
    ('sea', DATE '2008-01-01')
) AS v(source_id, d)
WHERE r.source_id = v.source_id AND r.history_start_date IS NULL;

UPDATE reference.source_registry
SET history_start_date = backfill_start_date
WHERE source_id IN ('aus', 'lax') AND history_start_date IS NULL;


-- ---------------------------------------------------------------------------
-- History slices are pulls like any other, recorded as mode 'history' so they
-- never count as the backfill or incremental the scheduler keys on.
-- ---------------------------------------------------------------------------
ALTER TABLE etl.pull_run DROP CONSTRAINT IF EXISTS pull_run_mode_check;
ALTER TABLE etl.pull_run ADD  CONSTRAINT pull_run_mode_check
    CHECK (mode IN ('backfill', 'incremental', 'boundary', 'reference', 'history')) NOT VALID;
ALTER TABLE etl.pull_run VALIDATE CONSTRAINT pull_run_mode_check;
