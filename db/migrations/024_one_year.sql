-- 024: one rolling year per city, and a record of how far a backfill has got.
--
-- The site now serves exactly one window: the last year (gold.WINDOW,
-- 'last_1y'). These are gone:
-- * the 3/6/9-month and multi-year windows;
-- * custom date ranges;
-- * offense-category filtering;
-- * resolution 10;
-- * the cell detail panel's monthly trend and offense mix.
-- Incidents older than a year are deleted on a schedule
-- (safety/etl/retention.py). This migration drops what the new code never
-- writes or reads. The silver rows themselves are removed by the first
-- `prune` run, which safety.ops plans after the deploy. Silver is partitioned,
-- the cutoff depends on each city's time zone, and a migration is the wrong
-- place for a long DELETE.
--
-- Contract, not expand: a release older than this one reads the dropped
-- tables. The API rolls out right after migrate, so the window in which an
-- old pod can hit one is the rollout itself.

-- Gold rows for windows other than the last year.
DELETE FROM gold.cell_activity    WHERE time_window <> 'last_1y';
DELETE FROM gold.cell_safety      WHERE time_window <> 'last_1y';
DELETE FROM gold.cell_hour_safety WHERE time_window <> 'last_1y';
DELETE FROM gold.cell_hour_profile WHERE time_window <> 'last_1y';
DELETE FROM gold.cell_dow_factor  WHERE time_window <> 'last_1y';
DELETE FROM gold.city_window      WHERE time_window <> 'last_1y';
UPDATE gold.city_window SET res10_built = false WHERE res10_built;

-- Per-category activity (nothing filters by category any more) and resolution 10.
DELETE FROM gold.cell_activity WHERE category <> 'all' OR h3_res = 10;
DELETE FROM gold.cell_neighbor WHERE h3_res = 10;
DELETE FROM gold.cell_geometry WHERE h3_res = 10;
UPDATE gold.city_snapshot SET cell_count_r10 = 0;

-- Layers nothing serves: the daily rollup (custom ranges), the monthly trend
-- and the offense mix (the old detail panel).
DROP TABLE IF EXISTS gold.cell_daily;
DROP TABLE IF EXISTS gold.cell_monthly;
DROP TABLE IF EXISTS gold.cell_offense_mix;
UPDATE gold.city_snapshot SET selectable_start = NULL, daily_scheme_version = NULL;

-- No history loads: one year is all a city keeps.
UPDATE reference.source_registry SET history_enabled = false WHERE history_enabled;

-- How far each city's first load has got, for the progress bar on the live
-- site (GET /api/v1/progress). One row per city, written outside the load's
-- own transactions so the API sees it as it moves:
-- * phase: fetch (the source portal, phase_done of phase_total chunks), then
--   promote (validate and upsert into silver), census, gold, and done.
-- * phase_total is NULL when the adapter cannot say in advance how many
--   chunks a fetch will take.
-- A row with finished_at set is a load that completed. Later incrementals do
-- not touch it.
CREATE TABLE IF NOT EXISTS etl.load_progress (
    source_id        text        PRIMARY KEY
                     REFERENCES reference.source_registry (source_id) ON DELETE CASCADE,
    phase            text        NOT NULL
                     CHECK (phase IN ('fetch', 'promote', 'census', 'gold', 'done', 'failed')),
    phase_done       integer     NOT NULL DEFAULT 0 CHECK (phase_done >= 0),
    phase_total      integer     CHECK (phase_total IS NULL OR phase_total >= 0),
    load_started_at  timestamptz NOT NULL DEFAULT now(),
    phase_started_at timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
