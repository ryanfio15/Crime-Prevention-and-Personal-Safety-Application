-- 014: a manifest for operator-run convergence steps (safety/ops.py).
--
-- etl.pull_run already records every raw pull, but the `ops` service does work
-- that is not a pull -- loading the population denominator, rebuilding gold,
-- building the time-of-day layers for the first time -- and that work had no
-- record at all. It lived in a Railway deploy log, which is rotated and is not
-- queryable from inside the application.
--
-- The reason this is a table rather than a log line is retry backoff. The `ops`
-- service redeploys on every push to `main` and re-runs whatever it points at,
-- so without a durable record of "this step was attempted and it failed", a
-- city whose portal is down gets a fresh 24-month backfill attempt on every
-- unrelated code change. See the cooldown in safety/ops.py.
--
-- What is *needed* is decided by inspecting the data itself (is there a
-- completed pull, are there census blocks, is the snapshot current) rather than
-- by reading this table. That keeps convergence self-correcting: delete a row
-- here and nothing breaks, truncate the table and the next run still does
-- exactly the right work. This table only answers "did we recently try, and how
-- did it go".

CREATE TABLE etl.ops_run (
    ops_run_id        bigserial   PRIMARY KEY,

    -- The converge step: 'backfill', 'census', 'gold', 'hourly'. Deliberately
    -- not a CHECK constraint -- the step list belongs in the pipeline, where
    -- adding one is a code change rather than a migration (cf. the note on
    -- gold.cell_hour_safety's window CHECK in 009).
    task              text        NOT NULL,

    -- NULL for a step that is not per-city. Nothing uses that yet; it is here so
    -- a global step does not need a sentinel source_id that looks like a city.
    source_id         text        REFERENCES reference.source_registry (source_id),

    status            text        NOT NULL
        CHECK (status IN ('running', 'succeeded', 'failed')),

    -- Why the step was judged necessary, in the words the plan printed. This is
    -- the field worth reading six weeks later: it says what the data looked like
    -- at the time, which the deploy log no longer can.
    reason            text,

    pipeline_version  text        NOT NULL,

    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    duration_seconds  double precision,
    error             text
);

-- Serves the one hot query: the most recent attempt of this task for this city.
CREATE INDEX ops_run_task_source_idx
    ON etl.ops_run (task, source_id, started_at DESC);

COMMENT ON TABLE etl.ops_run IS
    'Manifest of convergence steps run by the ops service (safety/ops.py). Drives retry backoff, not the decision of what work is needed.';
COMMENT ON COLUMN etl.ops_run.reason IS
    'The data condition that made this step necessary, as printed in the plan.';
