-- 021: which cities have silver changes their gold layers do not reflect yet.
--
-- An incremental pull re-reads a revision window of a week or more, and most
-- of what comes back is already in silver unchanged. Rebuilding gold after
-- every pull regardless spent half an hour a day recomputing tables that came
-- out the same. Now each write that actually changes silver -- a new or
-- revised incident, or one withdrawn upstream -- records the earliest
-- occurrence date it touched here, in the same transaction; a gold refresh
-- clears the row in its own. The incremental skips gold when there is no row,
-- and rebuilds the daily rollup only from `dirty_from` when there is.
--
-- dirty_from NULL means "rebuild everything": something changed that is not
-- a date range (a census reload), or nothing is known. Every city starts that
-- way, so the first incremental after this migration does one full rebuild --
-- what every incremental did before it -- and gold can never be left behind a
-- pull that committed before this table existed.
--
-- Expand-only: the previous release never reads or writes it.

CREATE TABLE IF NOT EXISTS etl.gold_pending (
    source_id   text        PRIMARY KEY
                REFERENCES reference.source_registry (source_id) ON DELETE CASCADE,
    dirty_from  date,
    marked_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO etl.gold_pending (source_id, dirty_from)
SELECT source_id, NULL FROM reference.source_registry
ON CONFLICT (source_id) DO UPDATE SET dirty_from = NULL, marked_at = now();
