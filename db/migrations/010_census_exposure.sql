-- 010: a population denominator for the safety ranking (design doc S3.3, S13).
--
-- gold.cell_safety ranks cells on severity-weighted offence per km2. Area is a
-- poor stand-in for exposure: a cell's weighted total scales with how many
-- people are in it, so the ranking has been reporting where Philadelphia is
-- busy about as much as where it is dangerous. Center City reads as the least
-- safe part of the city in large part because it is the most populated one.
--
-- The denominator this adds is *ambient* population -- residents plus jobs --
-- and not residents alone. Residents alone would be worse than area, not
-- better: the airport, the Navy Yard, Fairmount Park and Penn's Landing all
-- carry real incident counts over almost no resident count, and dividing by
-- that sends them to the top of the least-safe ranking by division rather than
-- by evidence. Jobs are what stop a place being scored as empty when it is
-- merely empty at night.
--
-- Two sources, both keyed on 2020 census blocks so they join with no crosswalk:
--
--   * 2020 Census population, from the TIGER/Line TABBLOCK20 shapefile (the
--     Feb-2022 or later vintage, which carries POP20 / HOUSING20 on the
--     geometry; the original Feb-2021 release has the same filename and stops
--     at INTPTLON20).
--   * Jobs by workplace, from LEHD LODES v8 WAC, which is enumerated on 2020
--     blocks for exactly this kind of join.
--
-- S13 bars joining crime data to demographic layers *for display*. Only total
-- population, housing units and total jobs are loaded here -- no race, income,
-- or any other characteristic. A denominator is not an overlay, and the
-- methodology endpoint says so on the record.

-- ---------------------------------------------------------------------------
-- Which census geographies belong to a city.
--
-- Registry configuration, per S8.4, so onboarding a seventh city stays a row
-- plus an adapter. county_fips is an array because most cities are not
-- coterminous with one county the way Philadelphia is -- Austin spans three.
-- ---------------------------------------------------------------------------
ALTER TABLE reference.source_registry
    ADD COLUMN IF NOT EXISTS state_fips  text,
    ADD COLUMN IF NOT EXISTS county_fips text[];

COMMENT ON COLUMN reference.source_registry.county_fips IS
    'County FIPS codes covering the city, for filtering census blocks. NULL until a city has a census load.';

UPDATE reference.source_registry
   SET state_fips = '42', county_fips = ARRAY['101'], updated_at = now()
 WHERE source_id = 'phl';


-- ---------------------------------------------------------------------------
-- Census blocks
--
-- Maintained reference data, the same standing as the offense crosswalk and
-- the severity weights: an external published dataset the pipeline reads and
-- never invents. Blocks rather than block groups because a block group is too
-- coarse to apportion into a 0.74 km2 hexagon -- Philadelphia has ~18,900
-- blocks against ~1,300 block groups and 551 resolution-8 cells.
-- ---------------------------------------------------------------------------
CREATE TABLE reference.census_block (
    geoid20    text        PRIMARY KEY,
    source_id  text        NOT NULL REFERENCES reference.source_registry (source_id),

    pop20      integer     NOT NULL,
    housing20  integer     NOT NULL,

    -- LODES WAC C000, loaded in a second pass; NULL until it runs.
    jobs       integer,
    jobs_year  smallint,

    aland20    bigint      NOT NULL,
    geom       geometry(MultiPolygon, 4326) NOT NULL,
    loaded_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX census_block_gix        ON reference.census_block USING GIST (geom);
CREATE INDEX census_block_source_idx ON reference.census_block (source_id);

COMMENT ON TABLE reference.census_block IS
    'Published census blocks with 2020 population and LODES workplace jobs. The exposure denominator''s raw input.';
COMMENT ON COLUMN reference.census_block.jobs IS
    'LODES WAC total jobs. Geocoded to the establishment where possible, but some employers land on a headquarters block -- a known source artifact, logged at build time rather than silently absorbed.';


-- ---------------------------------------------------------------------------
-- Ambient population per cell
--
-- Gold rather than reference, because it is keyed on the cell universe and is
-- rebuilt whenever that is. Apportioned by areal weight: a block's counts are
-- split across the cells it overlaps in proportion to the share of its area in
-- each.
--
-- Built for resolutions 8 and 9 only. A resolution-10 cell is ~0.015 km2 --
-- smaller than a typical city block, and Philadelphia has more resolution-10
-- cells than it has census blocks. A population figure there would be this
-- table's apportionment assumption rendered back as though it were data, which
-- is the same objection that kept resolutions 11 and 12 out of 008.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_exposure (
    source_id    text     NOT NULL,
    h3_index     text     NOT NULL,
    h3_res       smallint NOT NULL,

    -- Fractional on purpose. Areal apportionment splits a block across the
    -- cells it straddles, so 41.6 residents is a share of a block, not a claim
    -- about four tenths of a person.
    residents    double precision NOT NULL,
    jobs         double precision NOT NULL,

    -- How many blocks contributed. A cell built from one block is carrying
    -- that block's rounding and disclosure noise more or less undiluted.
    block_count  integer  NOT NULL,

    pop_vintage  smallint NOT NULL,
    jobs_vintage smallint,

    built_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res)
);

COMMENT ON TABLE gold.cell_exposure IS
    'Ambient population (residents + workplace jobs) per H3 cell, areally apportioned from census blocks. The safety ranking''s denominator.';


-- ---------------------------------------------------------------------------
-- The denominator is a property of the scheme, not of the code.
--
-- reference.severity_scheme already holds "one complete parameterization of the
-- statistic", which is what makes safety-compare able to diff a candidate
-- against the live ranking on the same data. The denominator belongs in that
-- same place, so switching to per-capita is a scheme promotion that can be
-- measured before it is made, rather than a deploy that silently changes what
-- every number in the product means.
--
-- eb_prior_persons is a separate column from eb_prior_km2 rather than a reused
-- one because the two are in different units and a scheme uses exactly one of
-- them. It also does more work here than it did before: with the denominator in
-- people, a cell whose ambient population rounds to nothing would otherwise
-- divide by zero, and the prior is what bounds it -- as exposure falls to zero
-- the posterior tends to city_rate + w/prior, not to infinity.
-- ---------------------------------------------------------------------------
ALTER TABLE reference.severity_scheme
    ADD COLUMN IF NOT EXISTS exposure_kind text NOT NULL DEFAULT 'area_km2'
        CHECK (exposure_kind IN ('area_km2', 'ambient_population')),
    -- What one job counts for against one resident. 1.0 treats a workplace and
    -- a home as equal exposure; there is no published figure for this, so it is
    -- a knob to be tuned against the ranking rather than a measurement.
    ADD COLUMN IF NOT EXISTS jobs_weight      double precision NOT NULL DEFAULT 1.0
        CHECK (jobs_weight >= 0),
    ADD COLUMN IF NOT EXISTS eb_prior_persons double precision
        CHECK (eb_prior_persons IS NULL OR eb_prior_persons > 0);

ALTER TABLE reference.severity_scheme
    ADD CONSTRAINT severity_scheme_prior_units CHECK (
        exposure_kind <> 'ambient_population' OR eb_prior_persons IS NOT NULL);

COMMENT ON COLUMN reference.severity_scheme.exposure_kind IS
    'Which denominator the ranking divides by. Kept as a scheme parameter so the area-based ranking stays buildable and diffable against the per-capita one.';
COMMENT ON COLUMN reference.severity_scheme.eb_prior_persons IS
    'Credibility prior in ambient persons, for exposure_kind = ambient_population. Also the floor that keeps a near-empty cell from dividing by zero.';


-- ---------------------------------------------------------------------------
-- Carry the denominator on the ranking rows themselves.
--
-- Nullable because a row built by an area_km2 scheme legitimately has neither,
-- and because that distinction has to survive both schemes being present at
-- once. weighted_per_km2 stays: it is still a true statement about the cell.
-- ---------------------------------------------------------------------------
ALTER TABLE gold.cell_safety
    ADD COLUMN IF NOT EXISTS exposure        double precision,
    ADD COLUMN IF NOT EXISTS weighted_per_1k double precision;

ALTER TABLE gold.cell_hour_safety
    ADD COLUMN IF NOT EXISTS exposure        double precision,
    ADD COLUMN IF NOT EXISTS weighted_per_1k double precision;

COMMENT ON COLUMN gold.cell_safety.exposure IS
    'The denominator this row was ranked on, in the units of its scheme''s exposure_kind. NULL for area-denominated schemes, which use area_km2.';
COMMENT ON COLUMN gold.cell_safety.weighted_per_1k IS
    'Severity-weighted offence per 1,000 ambient residents-and-jobs. NULL for area-denominated schemes.';


-- ---------------------------------------------------------------------------
-- The census pull is a pull like any other, and belongs in etl.pull_run with
-- its bronze snapshot -- "what did the source publish on this date" applies to
-- the Census Bureau exactly as it does to a police department.
-- ---------------------------------------------------------------------------
ALTER TABLE etl.pull_run DROP CONSTRAINT IF EXISTS pull_run_mode_check;
ALTER TABLE etl.pull_run ADD  CONSTRAINT pull_run_mode_check
    CHECK (mode IN ('backfill', 'incremental', 'boundary', 'reference'));


-- ---------------------------------------------------------------------------
-- How much of the city the denominator actually covers, surfaced next to the
-- ranking it underwrites. Same rule as hour_known_share in 009: a gap in the
-- input is published, not absorbed.
-- ---------------------------------------------------------------------------
ALTER TABLE gold.city_snapshot
    ADD COLUMN IF NOT EXISTS ambient_population double precision,
    ADD COLUMN IF NOT EXISTS population_vintage smallint,
    ADD COLUMN IF NOT EXISTS jobs_vintage       smallint;

COMMENT ON COLUMN gold.city_snapshot.ambient_population IS
    'Citywide residents + jobs behind the per-capita ranking. Published so the denominator is inspectable, not implicit.';
