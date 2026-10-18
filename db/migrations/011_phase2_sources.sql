-- 011: configuration for the five Phase 2 cities (design doc S8.4, S11, S14).
--
-- Phase 1 enabled Philadelphia only. 006 already seeded the other five with
-- their API paradigm, cadence, publication lag, attribution and terms, because
-- S11's claim is that onboarding a city is configuration plus one adapter. This
-- migration fills in the configuration that S11 did not anticipate needing, and
-- that only became visible once a second city was actually being built:
--
--   * which census geographies each city covers, for the exposure denominator;
--   * where each city's coverage polygon comes from, now that it is TIGER/Line
--     PLACE rather than five separate portal integrations;
--   * where each city's backfill window has to stop, because two of them have a
--     hard classification break mid-series (S4, S7.3);
--   * the two pieces of per-city prose the methodology endpoint had hardcoded
--     to Philadelphia.
--
-- Every row stays `enabled = false`. A city flips to true when its adapter,
-- crosswalk and boundary have all landed and its first backfill has been read
-- (see docs/PHASE2.md).


-- ---------------------------------------------------------------------------
-- Coverage polygon provenance
--
-- 010 added state_fips / county_fips for the census denominator. The same
-- TIGER/Line vintage also publishes incorporated-place boundaries, which is
-- where the five new cities' coverage polygons now come from -- one archive
-- format the pipeline already reads (safety/etl/census.py), rather than five
-- portal integrations across three API paradigms.
--
-- place_fips is resolved by name on the first boundary load and written back
-- here, so the match is auditable afterwards rather than re-guessed each run.
-- Philadelphia keeps its police-jurisdiction polygon (the union of
-- phl.carto police_districts) and therefore never gets one.
-- ---------------------------------------------------------------------------
ALTER TABLE reference.source_registry
    ADD COLUMN IF NOT EXISTS place_fips text;

COMMENT ON COLUMN reference.source_registry.place_fips IS
    'TIGER/Line PLACE code, resolved by name on first boundary load. NULL for a city whose boundary comes from its own portal.';


-- ---------------------------------------------------------------------------
-- Per-city methodology prose
--
-- S12 already treats attribution, freshness and location precision as
-- first-class registry columns rather than as application text. These two
-- belong in exactly the same place and were missed, because with one city
-- there was no difference between "what this city does" and "what the product
-- does".
--
-- occurrence_basis_note is the more important of the two. Philadelphia
-- publishes a police *dispatch* time, which is the dominant source of error in
-- the hourly view; Seattle publishes an actual offence start time and DC
-- publishes both a start and a report time. Stating Philadelphia's caveat over
-- Seattle's data would be plainly wrong.
-- ---------------------------------------------------------------------------
ALTER TABLE reference.source_registry
    ADD COLUMN IF NOT EXISTS occurrence_basis_note   text,
    ADD COLUMN IF NOT EXISTS denominator_examples_note text;

COMMENT ON COLUMN reference.source_registry.occurrence_basis_note IS
    'What this source''s timestamps actually measure. Surfaced by /api/v1/methodology, where it is the largest caveat on the hourly view.';
COMMENT ON COLUMN reference.source_registry.denominator_examples_note IS
    'City-specific examples of places with real incident counts and almost no residents, used to explain why the denominator counts jobs as well.';


-- ---------------------------------------------------------------------------
-- Philadelphia: seed the prose the API previously carried inline.
--
-- Only the part that is specific to this source. The consequence -- that a
-- dispatch-driven timestamp clusters toward waking hours, so the hourly view
-- reads closer to when incidents are reported than to when crime happens -- is
-- appended by safety/api/main.py._timestamp_caveat for any source whose records
-- are dispatch- or report-based. Repeating it here would print it twice.
-- ---------------------------------------------------------------------------
UPDATE reference.source_registry
   SET occurrence_basis_note =
           'Philadelphia publishes the time police were dispatched, not the time '
           'an offence occurred.',
       denominator_examples_note =
           'The airport, the Navy Yard, the stadium complex and the central '
           'business district all have real reported incidents and very few '
           'people living in them.',
       -- The timestamp half of this note moved to occurrence_basis_note above,
       -- where the methodology endpoint now reads it. Leaving it in both places
       -- would state it twice in one list.
       location_precision_note =
           'Coordinates are published at block level, not address level, so no '
           'reading below roughly a city block is meaningful.',
       updated_at = now()
 WHERE source_id = 'phl';


-- ---------------------------------------------------------------------------
-- Census geographies per city.
--
-- A county prefilter, not the final answer: the counties below are the cheap
-- way to avoid reading a whole state's blocks, and only Philadelphia is
-- coterminous with its county. safety/etl/census.py trims the loaded set to the
-- blocks that actually intersect the city boundary, which is what keeps
-- "city ambient population" a statement about the city rather than about Cook
-- or Los Angeles County.
-- ---------------------------------------------------------------------------
UPDATE reference.source_registry SET
    state_fips = v.state_fips,
    county_fips = v.county_fips,
    updated_at = now()
FROM (VALUES
    -- Chicago lies entirely within Cook County, O'Hare included.
    ('chi', '17', ARRAY['031']),
    ('sea', '53', ARRAY['033']),                    -- King
    ('lax', '06', ARRAY['037']),                    -- Los Angeles
    -- Austin spans three counties; Travis holds most of it.
    ('aus', '48', ARRAY['453', '491', '209']),      -- Travis, Williamson, Hays
    ('dc',  '11', ARRAY['001'])                     -- District of Columbia
) AS v(source_id, state_fips, county_fips)
WHERE reference.source_registry.source_id = v.source_id;


-- ---------------------------------------------------------------------------
-- Backfill floors, where a source has a hard break behind it (S4, S7.3).
--
-- Seattle migrated to NIBRS with a new RMS in May 2019; records either side of
-- that are not a continuous series, and a crosswalk that mapped both would be
-- claiming otherwise. Los Angeles froze its legacy 2020-present dataset in
-- March 2024 and began publishing NIBRS datasets in October 2024; the adapter
-- reads the NIBRS ones, so the window must not reach back past them.
--
-- These are floors, not windows. A trailing-24-month backfill sits well inside
-- both today; the floors are what stop a future widened window, or a deeper
-- historical load, from silently crossing a classification break.
-- ---------------------------------------------------------------------------
UPDATE reference.source_registry
   SET backfill_start_date = DATE '2019-06-01', updated_at = now()
 WHERE source_id = 'sea';

UPDATE reference.source_registry
   SET backfill_start_date = DATE '2024-10-01', updated_at = now()
 WHERE source_id = 'lax';


-- ---------------------------------------------------------------------------
-- Crosswalk: document the code-only fallback tier.
--
-- reference.offense_crosswalk keys on (code, text) because Philadelphia
-- publishes 30 stable code/text pairs that can be enumerated by hand. Chicago's
-- IUCR list is an order of magnitude larger and its free-text descriptions
-- drift, so an exact-text-only match would send real incidents to 'unmapped'
-- over a wording change upstream.
--
-- A row whose raw_offense_text_key is '*' matches any description under that
-- code, and is only consulted when no exact row matches -- see the two-tier
-- lookup in safety/etl/transform.py. No behaviour changes for a city that has
-- an exact row for every pair it publishes, which Philadelphia does.
-- ---------------------------------------------------------------------------
COMMENT ON COLUMN reference.offense_crosswalk.raw_offense_text_key IS
    'Normalized (upper + trimmed) raw_offense_text used for matching. The sentinel ''*'' matches any description under the same raw_offense_code, and is consulted only when no exact row matches.';
