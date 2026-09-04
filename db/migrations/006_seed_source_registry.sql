-- 006: seed the source registry (design doc S8.4).
--
-- Phase 1 (S14) enables Philadelphia only. The remaining five cities are
-- listed here disabled, because the registry is the artifact that documents
-- the cadence and API-paradigm differences the adapter split has to absorb
-- (S8.1, S8.2) -- and because Phase 2 should be a config flip plus one
-- adapter, not a schema change (S11).

INSERT INTO reference.source_registry (
    source_id, city_name, state_code, agency_name,
    api_type, base_url, incident_dataset, boundary_dataset,
    expected_cadence, publication_lag_days, revision_lookback_days,
    crosswalk_version, timezone,
    attribution_text, terms_url, freshness_note, location_precision_note,
    enabled
) VALUES
(
    'phl', 'Philadelphia', 'PA', 'Philadelphia Police Department',
    'carto_sql', 'https://phl.carto.com/api/v2/sql', 'incidents_part1_part2', 'police_districts',
    'daily', 0, 14,
    'phl_v1', 'America/New_York',
    'Crime incident data: City of Philadelphia / Philadelphia Police Department, via OpenDataPhilly (phl.carto.com).',
    'https://opendataphilly.org/datasets/crime-incidents/',
    'Published daily. Recent records are preliminary and may be revised or reclassified after initial reporting.',
    'Coordinates are published at block level, not address level. Timestamps are police dispatch times, not observed occurrence times.',
    true
),
-- Phase 2 targets (S14). Disabled: no adapter yet.
(
    'chi', 'Chicago', 'IL', 'Chicago Police Department',
    'socrata', 'https://data.cityofchicago.org/resource', 'ijzp-q8t2', NULL,
    'daily', 7, 14,
    'chi_v1', 'America/Chicago',
    'Crime incident data: City of Chicago Data Portal.',
    'https://data.cityofchicago.org/Public-Safety/Crimes-2001-to-Present/ijzp-q8t2',
    'Dataset excludes the most recent 7 days.',
    'Addresses and coordinates are reduced to block level for privacy.',
    false
),
(
    'sea', 'Seattle', 'WA', 'Seattle Police Department',
    'socrata', 'https://data.seattle.gov/resource', 'tazs-3rd5', NULL,
    'daily', 0, 14,
    'sea_v1', 'America/Los_Angeles',
    'Crime incident data: City of Seattle Open Data.',
    'https://data.seattle.gov/Public-Safety/SPD-Crime-Data-2008-Present/tazs-3rd5',
    'Classification scheme changes at the May 2019 NIBRS/RMS migration; pre- and post-2019 records are not a continuous series.',
    'Block-level coordinates.',
    false
),
(
    'lax', 'Los Angeles', 'CA', 'Los Angeles Police Department',
    'socrata', 'https://data.lacity.org/resource', 'nibrs-offenses', NULL,
    'biweekly', 0, 30,
    'lax_v1', 'America/Los_Angeles',
    'Crime incident data: City of Los Angeles Open Data.',
    'https://data.lacity.org/',
    'Refreshed roughly bi-weekly. The legacy 2020-present dataset was frozen in March 2024; current data comes from the NIBRS datasets launched October 2024.',
    'Addresses reduced to the nearest hundred block; some records carry unresolved (0,0) coordinates.',
    false
),
(
    'aus', 'Austin', 'TX', 'Austin Police Department',
    'socrata', 'https://data.austintexas.gov/resource', 'fdj4-gpfu', NULL,
    'weekly', 0, 30,
    'aus_v1', 'America/Chicago',
    'Crime incident data: City of Austin Open Data Portal.',
    'https://data.austintexas.gov/',
    'Published as annual datasets. The City notes published figures may differ from official APD crime statistics.',
    'Address-level.',
    false
),
(
    'dc',  'Washington', 'DC', 'Metropolitan Police Department',
    'esri_featureserver', 'https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/MapServer', 'crime_incidents', NULL,
    'rolling', 0, 30,
    'dc_v1', 'America/New_York',
    'Crime incident data: Open Data DC / Metropolitan Police Department.',
    'https://opendata.dc.gov/',
    'Annual datasets plus a rolling last-30-days feed. Recent records are preliminary.',
    'Geocoded to the DC Master Address Repository and snapped to street block; unresolved points show (0,0).',
    false
)
ON CONFLICT (source_id) DO NOTHING;
