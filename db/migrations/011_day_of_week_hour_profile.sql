-- Historical day-of-week + hour profile for Check Risk.
-- The selected calendar date is mapped to its local day of week, then the
-- serving layer reads this precomputed pattern. It does not predict an exact
-- future date and it never aggregates silver at request time.

CREATE TABLE gold.cell_dow_hour_profile (
    source_id      text     NOT NULL,
    h3_index       text     NOT NULL,
    h3_res         smallint NOT NULL,
    time_window    text     NOT NULL,
    day_of_week    smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    hour_block     smallint NOT NULL CHECK (hour_block BETWEEN 0 AND 23),
    category       text     NOT NULL
        CHECK (category IN ('all', 'violent', 'property', 'quality_of_life', 'other')),
    incident_count integer NOT NULL CHECK (incident_count > 0),
    refreshed_at   timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        source_id, h3_index, h3_res, time_window,
        day_of_week, hour_block, category
    )
);

CREATE INDEX cell_dow_hour_profile_lookup_idx
    ON gold.cell_dow_hour_profile
       (h3_index, time_window, day_of_week, hour_block, category);

COMMENT ON TABLE gold.cell_dow_hour_profile IS
    'Historical cell activity by local day-of-week and hour. The calendar date in Check Risk selects the weekday; it is not an exact-date prediction.';
