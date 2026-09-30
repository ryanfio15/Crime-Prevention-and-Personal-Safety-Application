-- 012: reclaim storage, so six cities fit on a small volume.
--
-- Nothing here changes a number the product shows. Every row and index dropped
-- below is one the serving layer cannot read, or one the pipeline has stopped
-- building -- see docs/PHASE2.md "Fitting six cities on one volume" for the
-- measurements and the order they were applied in.
--
-- DEPLOY.md sizes the database volume at 20 GB on the estimate that the gold
-- layer scales with cells rather than incidents, and it is right about that. The
-- items here are the ones where it was scaling with cells nobody could ask about.

-- ---------------------------------------------------------------------------
-- 1. The unused spatial index on silver.incident
--
-- 004 declared a GiST index on silver.incident.geom alongside the H3 ones. The
-- H3 indexes earn their keep -- every gold rollup groups by h3_r8/r9/r10 -- but
-- nothing has ever queried the point geometry. S9.3's first rule is that reads
-- touch the gold schema only, so no bbox or radius filter runs against silver;
-- the API's two silver queries are aggregates with no spatial predicate, and
-- every ST_* call in safety/etl/ is against reference.census_block.geom or
-- reference.city_boundary.geom.
--
-- At roughly 1.6M incidents across six cities this is the largest index on the
-- table and answers no question. The column stays: it is part of the S6
-- canonical schema, it is cheap next to its index, and a future radius query
-- would want it. Re-creating the index is one statement if that day comes.
--
-- Verify before trusting this on an established database -- idx_scan should be
-- 0 or near it:
--   SELECT indexrelname, idx_scan FROM pg_stat_user_indexes
--    WHERE relname = 'incident' ORDER BY idx_scan;
DROP INDEX IF EXISTS silver.incident_geom_gix;

-- ---------------------------------------------------------------------------
-- 2. Census block polygons become releasable
--
-- reference.census_block holds a MultiPolygon per 2020 tabulation block, and it
-- is the largest reference data in the database: ~18,900 blocks for
-- Philadelphia, ~100,000 for Los Angeles County alone, each with a real polygon
-- and all of them under a GiST index.
--
-- They are a build-time input and only that. Two things read them -- the
-- boundary trim in census._trim_to_boundary and the areal apportionment in
-- census._EXPOSURE_SQL -- and both write their results elsewhere
-- (gold.cell_exposure, gold.city_snapshot). The serving layer never touches the
-- table at all.
--
-- So the column becomes nullable and `safety.etl.run release-geometry` empties
-- it once every cell has an exposure figure, which is checked rather than
-- assumed. The counts (pop20, housing20, jobs, aland20) are untouched, so the
-- retention check, the citywide ambient total and the LODES outlier log all keep
-- working on a released city. Restoring is a TIGER re-download: `census --city
-- <id>`, whose upsert sets geom back.
--
-- NOT NULL is what is being given up here, and with it the guarantee that a
-- loaded block can always be re-apportioned. census.build_cell_exposure raises
-- rather than writing zeros when it needs a polygon that is gone, because a
-- zeroed denominator reads on the map as "nobody lives here" instead of as a
-- missing input.
ALTER TABLE reference.census_block
    ALTER COLUMN geom DROP NOT NULL;

COMMENT ON COLUMN reference.census_block.geom IS
    'Block polygon, for the boundary trim and the areal apportionment only. NULL where released to reclaim disk after gold.cell_exposure was built; restore with a census reload.';

-- ---------------------------------------------------------------------------
-- 3. The resolution-10 safety ranking, which was never served
--
-- gold.cell_safety was built at resolutions 8, 9 and 10 for any scheme dividing
-- by area, because area is exact at any cell size and there was no reason not
-- to. But safety/api/repository.py::SAFETY_RESOLUTIONS is (8, 9) and
-- _require_safety_res refuses a resolution-10 ranking whatever produced it: a
-- res-10 cell is smaller than a census block, so the per-capita denominator
-- there would be interpolation, and the API will not serve two rankings on two
-- different denominators under one name.
--
-- That left ~1.9M rows per city (cells x 4 windows x 2 tracks) that nothing
-- could read. safety.etl.gold.SAFETY_RESOLUTIONS now caps every scheme at (8, 9),
-- so this clears what the old behaviour wrote.
--
-- gold.cell_hour_safety is not in this list and needs no cleanup: HOURLY_RESOLUTIONS
-- has always been (8, 9), so it never held a resolution-10 row.
DELETE FROM gold.cell_safety WHERE h3_res = 10;

-- ---------------------------------------------------------------------------
-- 4. Resolution-10 ring-1 adjacency, which had the same problem
--
-- gold.cell_neighbor has exactly two readers -- _SAFETY_SQL and _HOUR_SAFETY_SQL
-- in safety/etl/gold.py -- and both are the safety ranking, now capped at
-- SAFETY_RESOLUTIONS. So six pairs per resolution-10 cell were being COPYed in on
-- every gold refresh and never joined against: 146,628 of Philadelphia's 170,558
-- rows, and roughly four times that for Los Angeles.
--
-- build_cell_universe now calls _clear_cell_neighbors instead of
-- _build_cell_neighbors outside those resolutions, so this is belt-and-braces for
-- a database that will not see a refresh before it needs the space.
DELETE FROM gold.cell_neighbor WHERE h3_res = 10;

-- ---------------------------------------------------------------------------
-- 5. The resolution-10 activity layer, narrowed
--
-- gold.cell_activity is dense on purpose: every cell in the universe gets a row
-- per window per category, because S3.3 ranks a cell against the whole city and
-- a cell with no reported incidents is part of that distribution. Twenty rows
-- per cell, and at resolution 10 that made it the largest table here -- six
-- cities are on the order of 230,000 res-10 cells, so ~4.6M rows, nearly all of
-- them incident_count = 0 / activity_tier = 0.
--
-- safety.etl.gold.ACTIVITY_WINDOWS / ACTIVITY_CATEGORIES narrow resolution 10 to
-- the two widest windows and category 'all' -- 2 rows per cell instead of 20.
-- The statistical case is the same one that kept the hourly layer off resolution
-- 10: a ~0.015 km2 cell over 30 days is a field of ties, and a percentile
-- computed against it was ranking cells that all held zero. The map's default
-- view (last_12m, all) is unaffected at every resolution, and the API now
-- answers an out-of-scope request with the reason instead of an empty layer.
--
-- Written as the complement of the scope rather than as an explicit list, so
-- re-widening the scope in Python needs no migration to undo this -- the next
-- refresh simply writes the rows back. refresh_cell_activity deletes per
-- (resolution, window) across every window before it skips the ones out of
-- scope, which is what makes that true.
DELETE FROM gold.cell_activity
 WHERE h3_res = 10
   AND (time_window NOT IN ('last_12m', 'last_24m') OR category <> 'all');
