-- 002: reference data -- source registry (S8.4) and offense crosswalk (S7).

-- ---------------------------------------------------------------------------
-- Source registry (design doc S8.4)
--
-- One row per city/dataset. Holds everything that differs between sources:
-- base URL, API paradigm, cadence, publication lag, last-successful watermark,
-- and which crosswalk version currently applies. Adding a seventh city is a
-- row here plus one adapter (S11) -- never a schema change.
-- ---------------------------------------------------------------------------
CREATE TABLE reference.source_registry (
    source_id               text        PRIMARY KEY,
    city_name               text        NOT NULL,
    state_code              text        NOT NULL,
    agency_name             text        NOT NULL,

    -- S8.1: three API paradigms are in play across the six initial cities.
    api_type                text        NOT NULL
        CHECK (api_type IN ('carto_sql', 'socrata', 'esri_featureserver')),
    base_url                text        NOT NULL,
    incident_dataset        text        NOT NULL,
    boundary_dataset        text,

    -- S8.2: cadence is per city, stored as configuration rather than in job code.
    expected_cadence        text        NOT NULL
        CHECK (expected_cadence IN ('daily', 'weekly', 'biweekly', 'annual', 'rolling')),
    publication_lag_days    integer     NOT NULL DEFAULT 0,
    -- How far back an incremental pull re-reads, because agencies revise and
    -- reclassify incidents after first publication (S8.3).
    revision_lookback_days  integer     NOT NULL DEFAULT 14,

    crosswalk_version       text        NOT NULL,
    backfill_start_date     date,

    -- S8.3 watermark + run bookkeeping.
    last_attempt_at         timestamptz,
    last_success_at         timestamptz,
    last_success_watermark  timestamptz,
    last_status             text,
    last_error              text,

    -- S12: attribution and terms are first-class, not a footnote.
    attribution_text        text        NOT NULL,
    terms_url               text,
    freshness_note          text,

    -- Precision/semantics caveats worth surfacing in the methodology page (S13).
    timezone                text        NOT NULL DEFAULT 'UTC',
    location_precision_note text,

    enabled                 boolean     NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE reference.source_registry IS
    'S8.4 source registry: per-city configuration that makes onboarding a new city a config + adapter exercise.';
COMMENT ON COLUMN reference.source_registry.publication_lag_days IS
    'Days the source withholds before publishing (Chicago = 7). Used for honest freshness messaging.';
COMMENT ON COLUMN reference.source_registry.revision_lookback_days IS
    'S8.3: incremental pulls re-read this many days behind the watermark and upsert, since sources revise records.';


-- ---------------------------------------------------------------------------
-- Offense crosswalk (design doc S7)
--
-- Maps each city's raw offense code/text to NIBRS, and secondarily to the
-- coarser UCR Part I/II severity split which is the more forgiving bucket when
-- a precise NIBRS mapping is ambiguous (S7.2). Versioned by effective date so
-- an RMS migration upstream (Seattle 2019, LA 2024) does not silently rewrite
-- history (S7.3).
-- ---------------------------------------------------------------------------
CREATE TABLE reference.offense_crosswalk (
    crosswalk_version    text     NOT NULL,
    source_id            text     NOT NULL REFERENCES reference.source_registry (source_id),

    -- Raw values exactly as the source publishes them (S7.4: never discarded).
    raw_offense_code     text     NOT NULL,
    raw_offense_text     text     NOT NULL,
    -- Normalized (upper + trimmed) form of raw_offense_text used for matching,
    -- so casing drift upstream does not break the join.
    raw_offense_text_key text     NOT NULL,
    raw_source_category  text,

    -- Primary standardization target (S7.1).
    nibrs_code           text,
    nibrs_offense_name   text,
    nibrs_group          text     CHECK (nibrs_group IN ('A', 'B')),
    nibrs_crime_against  text     CHECK (nibrs_crime_against IN ('person', 'property', 'society', 'group_b')),

    -- Coarser fallback split (S6, S7.2).
    ucr_part             text     CHECK (ucr_part IN ('I', 'II')),
    severity_bucket      text     NOT NULL
        CHECK (severity_bucket IN ('part_i_violent', 'part_i_property', 'part_ii', 'unknown')),

    -- Product-level grouping shown in the UI. Derived from the UCR violent /
    -- property split rather than straight from NIBRS crime-against, because
    -- NIBRS files robbery under Crimes Against Property while a personal-safety
    -- product must present it as violent.
    product_category     text     NOT NULL
        CHECK (product_category IN ('violent', 'property', 'quality_of_life', 'other')),

    -- S7.2: honest about how tight each mapping is.
    mapping_confidence   text     NOT NULL
        CHECK (mapping_confidence IN ('exact', 'approximate', 'ambiguous', 'unmapped')),

    effective_from       date     NOT NULL,
    effective_to         date,
    notes                text,

    PRIMARY KEY (crosswalk_version, source_id, raw_offense_code, raw_offense_text_key, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX offense_crosswalk_lookup_idx
    ON reference.offense_crosswalk (source_id, crosswalk_version, raw_offense_code, raw_offense_text_key);

COMMENT ON TABLE reference.offense_crosswalk IS
    'S7 crosswalk: per-city raw offense code/text -> NIBRS + UCR Part I/II, versioned by effective date.';
COMMENT ON COLUMN reference.offense_crosswalk.product_category IS
    'UI-facing grouping. Follows the UCR violent/property split so robbery reads as violent, unlike NIBRS crime-against.';


-- ---------------------------------------------------------------------------
-- City boundary (design doc S3.1)
--
-- Defines the cell universe for relative-activity ranking: a cell with zero
-- reported incidents is meaningfully different from a cell outside the city,
-- and S3.3 compares each cell against other cells "in the same city".
-- ---------------------------------------------------------------------------
CREATE TABLE reference.city_boundary (
    source_id      text        PRIMARY KEY REFERENCES reference.source_registry (source_id),
    boundary_kind  text        NOT NULL
        CHECK (boundary_kind IN ('city_limits', 'police_jurisdiction')),
    geom           geometry(MultiPolygon, 4326) NOT NULL,
    area_km2       double precision NOT NULL,
    source_note    text,
    fetched_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX city_boundary_gix ON reference.city_boundary USING GIST (geom);

COMMENT ON TABLE reference.city_boundary IS
    'S3.1 coverage polygon per source. Used to enumerate the H3 cell universe for relative ranking.';
