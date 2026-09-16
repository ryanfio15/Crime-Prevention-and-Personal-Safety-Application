-- 007: the safety ranking -- severity-weighted percentiles, violent and
-- non-violent ranked separately (design doc S3.3, S5 layer 3).
--
-- gold.cell_activity already ranks cells on raw incident density, which counts
-- a homicide and a stolen bicycle the same. This layer weights each incident by
-- published offense severity and ranks the weighted totals.
--
-- Why two tracks rather than one combined index: the FBI's own UCR Crime Index
-- was an unweighted tally, and the CJIS Advisory Policy Board discontinued it in
-- June 2004 precisely because it was always driven by whichever offense had the
-- highest count -- normally larceny-theft, which was 59.7% of the 2001 Index
-- against murder's 0.1%. Since then the FBI publishes separate violent and
-- property totals and no combined figure. This follows that.
--
-- Nothing here is derived at request time; the ETL materializes all of it.

-- ---------------------------------------------------------------------------
-- Severity schemes
--
-- A scheme is one complete parameterization of the statistic: the weight table
-- plus the two smoothing constants. Keeping several around lets a candidate be
-- built and diffed against the live one on the same data before it is promoted
-- (safety.etl.run safety-compare).
-- ---------------------------------------------------------------------------
CREATE TABLE reference.severity_scheme (
    scheme_version      text        PRIMARY KEY,
    description         text        NOT NULL,
    source_citation     text        NOT NULL,

    -- Credibility prior, expressed as an area of citywide-average evidence
    -- added to every cell: adj = (W + city_rate * p) / (area_km2 + p).
    --
    -- The prior has to be an exposure, not a count. A cell with no incidents was
    -- still watched for the whole window, so zero is evidence of a low rate, not
    -- an absence of information -- weighting a cell's own figure by n/(n+k)
    -- instead sends every empty cell to the citywide mean and ends up ranking a
    -- cell with six assaults safer than a cell with none.
    --
    -- Because it is an area, one value shrinks the smaller resolution-9 cells
    -- harder than resolution-8 ones, which is correct: they carry less data each.
    eb_prior_km2        double precision NOT NULL CHECK (eb_prior_km2 >= 0),

    -- Share of a cell's smoothed value that comes from the cell itself; the
    -- remainder is the mean of its ring-1 neighbours.
    self_weight         double precision NOT NULL CHECK (self_weight BETWEEN 0 AND 1),

    enabled             boolean     NOT NULL DEFAULT true,
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE reference.severity_scheme IS
    'One tunable parameterization of the safety ranking. Loaded from reference/severity/schemes.csv, never hardcoded.';


-- ---------------------------------------------------------------------------
-- Per-offense severity weights
--
-- Resolved per incident by precedence: the raw source text first (Philadelphia
-- distinguishes armed from unarmed robbery and assault, which the severity
-- literature treats as materially different events), then the NIBRS code, then
-- the coarse UCR bucket.
--
-- `sourced` records whether the weight is a published figure or a derived
-- fallback, so the methodology page can be honest about which is which.
-- ---------------------------------------------------------------------------
CREATE TABLE reference.offense_severity_weight (
    scheme_version   text     NOT NULL REFERENCES reference.severity_scheme (scheme_version),
    track            text     NOT NULL CHECK (track IN ('violent', 'non_violent')),
    key_type         text     NOT NULL
        CHECK (key_type IN ('raw_offense_text_key', 'nibrs_code', 'severity_bucket')),
    key_value        text     NOT NULL,
    weight           double precision NOT NULL CHECK (weight > 0),

    sourced          boolean  NOT NULL DEFAULT true,
    source_item      text,
    notes            text,

    PRIMARY KEY (scheme_version, track, key_type, key_value)
);

CREATE INDEX offense_severity_weight_lookup_idx
    ON reference.offense_severity_weight (scheme_version, key_type, key_value);

COMMENT ON COLUMN reference.offense_severity_weight.source_item IS
    'The published vignette the weight is read from, quoted verbatim so the number is auditable against the source.';
COMMENT ON COLUMN reference.offense_severity_weight.sourced IS
    'false = derived fallback (e.g. the median of sourced weights in that bucket), not a published figure.';


-- ---------------------------------------------------------------------------
-- Cell adjacency
--
-- Materialized once per cell universe so the ring-1 smoothing is a single SQL
-- join rather than a Python round trip. Restricted to cells inside the
-- universe, so an edge cell averages over only the neighbours that exist.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_neighbor (
    source_id   text     NOT NULL,
    h3_res      smallint NOT NULL,
    h3_index    text     NOT NULL,
    neighbor_h3 text     NOT NULL,

    PRIMARY KEY (source_id, h3_res, h3_index, neighbor_h3)
);

COMMENT ON TABLE gold.cell_neighbor IS
    'Ring-1 adjacency within the cell universe, for the spatial smoothing in gold.cell_safety.';


-- ---------------------------------------------------------------------------
-- The safety ranking
--
-- One row per cell / window / track / scheme. `safety_percentile` is oriented
-- so 1.0 is the safest cell in the city on that track -- the *lowest* weighted
-- offense total -- and is a Hazen midrank, so tied blocks sit at the centre of
-- their own range instead of collapsing onto a degenerate 0 or 1.
-- ---------------------------------------------------------------------------
CREATE TABLE gold.cell_safety (
    source_id          text     NOT NULL,
    h3_index           text     NOT NULL,
    h3_res             smallint NOT NULL,
    time_window        text     NOT NULL
        CHECK (time_window IN ('last_30d', 'last_90d', 'last_12m', 'last_24m')),
    track              text     NOT NULL CHECK (track IN ('violent', 'non_violent')),
    scheme_version     text     NOT NULL REFERENCES reference.severity_scheme (scheme_version),

    window_start       date     NOT NULL,
    window_end         date     NOT NULL,

    incident_count     integer  NOT NULL,
    weighted_total     double precision NOT NULL,
    weighted_per_km2   double precision NOT NULL,
    -- The shrunk, neighbour-blended rate the ranking is actually computed on.
    smoothed_per_km2   double precision NOT NULL,

    safety_percentile  double precision NOT NULL CHECK (safety_percentile BETWEEN 0 AND 1),
    safety_rank        integer  NOT NULL,
    city_cell_total    integer  NOT NULL,

    -- 0 is "nothing of this track was reported here", which is a different
    -- statement from "this is among the safest quarter of the city" -- an
    -- absence of reports can be an absence of reporting (S13).
    safety_tier        smallint NOT NULL CHECK (safety_tier BETWEEN 0 AND 4),

    refreshed_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, h3_index, h3_res, time_window, track, scheme_version)
);

CREATE INDEX cell_safety_layer_idx
    ON gold.cell_safety (source_id, h3_res, time_window, track, scheme_version)
    INCLUDE (h3_index, safety_percentile, safety_tier, weighted_total);

CREATE INDEX cell_safety_cell_idx
    ON gold.cell_safety (h3_index, time_window, scheme_version);

COMMENT ON COLUMN gold.cell_safety.safety_percentile IS
    '0-1, 1.0 = safest. Hazen midrank of the smoothed severity-weighted total against other cells in the same city, same track and window.';
COMMENT ON COLUMN gold.cell_safety.safety_tier IS
    '0 = nothing of this track reported here; 1-4 = quartile of the same-city safety distribution (1 = least safe).';


-- ---------------------------------------------------------------------------
-- Which scheme each city serves, and how well the weights covered its data.
-- Nullable: migrations run before the reference CSVs load, so the pointer is
-- set by safety/migrate.py once a scheme exists.
-- ---------------------------------------------------------------------------
ALTER TABLE reference.source_registry
    ADD COLUMN severity_scheme_version text
        REFERENCES reference.severity_scheme (scheme_version);

ALTER TABLE gold.city_snapshot
    ADD COLUMN severity_scheme_version  text,
    ADD COLUMN severity_weight_coverage double precision;

COMMENT ON COLUMN gold.city_snapshot.severity_weight_coverage IS
    'Share of incidents whose severity weight came from a published figure rather than a derived fallback.';
