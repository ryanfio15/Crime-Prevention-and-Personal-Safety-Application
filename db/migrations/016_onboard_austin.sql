-- 016: point Austin at the CrimeViewer ArcGIS services (docs/PHASE2.md, Austin).
--
-- APD's open-data datasets locate an incident no finer than a census block
-- group, which is why Austin was seeded and left without an adapter. The
-- services behind APD's public CrimeViewer map publish hundred-block points;
-- safety/etl/adapters/austin.py reads them. base_url is the current
-- FeatureServer. Aggravated assault comes from the older MapServer, whose URL
-- lives in the adapter because the registry has one base_url per source.
--
-- Austin stays disabled. Enabling is the same deliberate one-line UPDATE as for
-- every other city, after its first backfill has been read against PHASE2's
-- checks and the City has confirmed the services may be reused: unlike the
-- open-data portal, they publish no licence.

UPDATE reference.source_registry SET
    api_type               = 'esri_featureserver',
    base_url               = 'https://maps.austintexas.gov/arcgis/rest/services/CrimeViewer_new/APD_Reported_Crimes_new/FeatureServer',
    incident_dataset       = 'APD_Reported_Crimes_new',
    expected_cadence       = 'daily',
    publication_lag_days   = 1,
    revision_lookback_days = 30,
    -- The services begin on 2021-09-23; nothing earlier exists to backfill.
    backfill_start_date    = DATE '2021-10-01',
    attribution_text       = 'Crime incident data: Austin Police Department, via the City of Austin CrimeViewer (maps.austintexas.gov).',
    terms_url              = 'https://maps.austintexas.gov/GIS/CrimeViewer/',
    freshness_note         = 'Refreshed daily. Recent records are preliminary and may be revised or reclassified after initial reporting.',
    occurrence_basis_note  = 'Austin publishes the date and time an offence occurred.',
    denominator_examples_note = 'Downtown and Sixth Street, the airport, and the University of Texas campus all have real reported incidents and comparatively few residents.',
    location_precision_note   = 'Points are published at the hundred block. APD does not publish sex offences on its crime map, so they are not on this one; each incident is shown under its primary offence only.',
    updated_at             = now()
WHERE source_id = 'aus';
