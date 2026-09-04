-- 005: gold layer -- precomputed cell rollups (design doc S5 layer 3, S9.3).
--
-- This is the only schema the serving layer reads. Nothing here is derived at
-- request time; the ETL materializes all of it, so rendering a map never scans
-- raw incident rows.

-- ---------------------------------------------------------------------------
-- Cell geometry
--
-- One row per H3 cell in the city's cell universe (the cells tiling
-- reference.city_boundary at a given resolution). Materialized so the API can
-- return map-ready GeoJSON and so PostGIS can answer viewport bbox queries via
-- GiST. It also defines which cells count as "in the city" for the relative
-- ranking in S3.3 -- a cell with zero reported incidents is part of the
-- distribution; a cell outside the boundary is not.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_geometry (
    h3_index   text        PRIMARY KEY,
    source_id  text        NOT NULL REFERENCES reference.source_registry (source_id),
    h3_res     smallint    NOT NULL CHECK (h3_res BETWEEN 0 AND 15),
    area_km2   double precision NOT NULL,
    centroid   geometry(Point, 4326)   NOT NULL,
    boundary   geometry(Polygon, 4326) NOT NULL,
    built_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX cell_geometry_boundary_gix ON gold.cell_geometry USING GIST (boundary);
CREATE INDEX cell_geometry_src_res_idx  ON gold.cell_geometry (source_id, h3_res);

COMMENT ON TABLE gold.cell_geometry IS
    'H3 cell universe per city/resolution, with boundary polygons for map rendering and bbox filtering.';


-- ---------------------------------------------------------------------------
-- Cell activity: the map layer
--
-- Keyed by (city, cell, resolution, time bucket, offense category) exactly as
-- S5/S9.3 describe, holding both the raw count and the relative measure S3.3
-- calls for -- a percentile of reported-incident density against other cells
-- in the same city over the same window, not an absolute risk score.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_activity (
    source_id         text     NOT NULL,
    h3_index          text     NOT NULL,
    h3_res            smallint NOT NULL,
    time_window       text     NOT NULL
        CHECK (time_window IN ('last_30d', 'last_90d', 'last_12m', 'last_24m')),
    category          text     NOT NULL
        CHECK (category IN ('all', 'violent', 'property', 'quality_of_life', 'other')),

    window_start      date     NOT NULL,
    window_end        date     NOT NULL,

    incident_count    integer  NOT NULL,
    incidents_per_km2 double precision NOT NULL,

    -- Relative position within this city / resolution / window / category.
    city_rank         integer  NOT NULL,
    city_cell_total   integer  NOT NULL,
    percentile        double precision NOT NULL CHECK (percentile BETWEEN 0 AND 1),

    -- S2 asks for a simple 5-tier relative scale for the resident/commuter
    -- segment. Tier 0 is a distinct "no reported incidents" state rather than
    -- being folded into the bottom tier, because the two mean different things.
    activity_tier     smallint NOT NULL CHECK (activity_tier BETWEEN 0 AND 5),

    refreshed_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res, time_window, category)
);

CREATE INDEX cell_activity_layer_idx
    ON gold.cell_activity (source_id, h3_res, time_window, category)
    INCLUDE (h3_index, incident_count, activity_tier, percentile);

CREATE INDEX cell_activity_cell_idx
    ON gold.cell_activity (h3_index, time_window);

COMMENT ON COLUMN gold.cell_activity.percentile IS
    'S3.3: fraction of same-city cells with strictly lower reported-incident density in this window.';
COMMENT ON COLUMN gold.cell_activity.activity_tier IS
    '0 = no reported incidents; 1-5 = quintile of the same-city density distribution (S2 five-tier scale).';


-- ---------------------------------------------------------------------------
-- Monthly series per cell (sparse: rows only where count > 0)
--
-- Backs the trend sparkline in the cell detail panel without the read path
-- touching silver (S9.3).
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_monthly (
    source_id      text     NOT NULL,
    h3_index       text     NOT NULL,
    h3_res         smallint NOT NULL,
    month_start    date     NOT NULL,
    category       text     NOT NULL
        CHECK (category IN ('all', 'violent', 'property', 'quality_of_life', 'other')),
    incident_count integer  NOT NULL CHECK (incident_count > 0),
    refreshed_at   timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res, month_start, category)
);

CREATE INDEX cell_monthly_cell_idx ON gold.cell_monthly (h3_index, category, month_start);


-- ---------------------------------------------------------------------------
-- Offense mix per cell: the top raw offense types behind a cell's count.
--
-- Carries the raw source text alongside the mapped NIBRS code so the detail
-- panel can show what the city actually reported (S7.4).
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_offense_mix (
    source_id        text     NOT NULL,
    h3_index         text     NOT NULL,
    h3_res           smallint NOT NULL,
    time_window      text     NOT NULL,
    rank             smallint NOT NULL,
    raw_offense_text text     NOT NULL,
    nibrs_code       text,
    product_category text     NOT NULL,
    incident_count   integer  NOT NULL,
    refreshed_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res, time_window, rank)
);

CREATE INDEX cell_offense_mix_cell_idx ON gold.cell_offense_mix (h3_index, time_window, rank);


-- ---------------------------------------------------------------------------
-- City snapshot
--
-- S12(b): "data as of" must be a first-class, visible UI element, not a
-- footnote -- so it is a first-class column here rather than something the API
-- infers at request time.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.city_snapshot (
    source_id          text        PRIMARY KEY REFERENCES reference.source_registry (source_id),
    city_name          text        NOT NULL,
    agency_name        text        NOT NULL,

    data_as_of         timestamptz NOT NULL,
    last_refreshed_at  timestamptz NOT NULL,
    expected_cadence   text        NOT NULL,
    publication_lag_days integer   NOT NULL,
    freshness_note     text,

    incident_count     bigint      NOT NULL,
    coverage_start     date        NOT NULL,
    coverage_end       date        NOT NULL,
    cell_count_r8      integer     NOT NULL,
    cell_count_r9      integer     NOT NULL,

    center_lat         double precision NOT NULL,
    center_lng         double precision NOT NULL,
    bbox_west          double precision NOT NULL,
    bbox_south         double precision NOT NULL,
    bbox_east          double precision NOT NULL,
    bbox_north         double precision NOT NULL,

    crosswalk_version  text        NOT NULL,
    pipeline_version   text        NOT NULL,
    attribution_text   text        NOT NULL,
    terms_url          text,
    unmapped_offense_count integer NOT NULL DEFAULT 0,
    rejected_record_count  integer NOT NULL DEFAULT 0
);

COMMENT ON TABLE gold.city_snapshot IS
    'Per-city serving metadata, including the S12(b) "data as of" indicator and S8.5 data-quality counts.';
