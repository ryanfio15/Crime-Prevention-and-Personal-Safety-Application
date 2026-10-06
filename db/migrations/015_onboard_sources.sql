-- Registry corrections and methodology prose for the Phase 2 cities whose
-- adapters now exist: Chicago, Seattle, Los Angeles and Washington DC.
--
-- Austin is untouched and stays disabled: neither APD dataset publishes a
-- location finer than a census block group (docs/PHASE2.md).

-- Los Angeles: the seeded 'nibrs-offenses' was a placeholder, not a Socrata id.
UPDATE reference.source_registry SET
    incident_dataset = 'k7nn-b2ep',
    terms_url        = 'https://data.lacity.org/Public-Safety/LAPD-NIBRS-Offenses-Dataset/k7nn-b2ep',
    updated_at       = now()
WHERE source_id = 'lax';

-- DC: the adapter reads the annual layers, not the rolling last-30-days feed,
-- so it is a daily source. 'rolling' would page the whole current-year layer
-- on every six-hourly tick.
UPDATE reference.source_registry SET
    expected_cadence = 'daily',
    freshness_note   = 'Published daily as one dataset per year. Recent records are preliminary.',
    updated_at       = now()
WHERE source_id = 'dc';

UPDATE reference.source_registry AS r SET
    occurrence_basis_note     = v.occurrence_basis_note,
    denominator_examples_note = v.denominator_examples_note,
    location_precision_note   = v.location_precision_note,
    updated_at                = now()
FROM (VALUES
    ('chi',
     'Chicago publishes the recorded time the offence occurred.',
     'O''Hare and Midway, the Loop, and McCormick Place all have real reported incidents and very few people living in them.',
     'Addresses and coordinates are reduced to block level for privacy, so no reading below roughly a city block is meaningful.'),
    ('sea',
     'Seattle publishes the time an offence began, as recorded by SPD.',
     'Downtown, the stadium district and the University District all have real reported incidents and few residents relative to the people present.',
     'Coordinates are block level. SPD withholds the location of roughly one record in six, mostly offences where a location would identify a victim; those records are not on the map.'),
    ('lax',
     'Los Angeles publishes the date and time an offence occurred; times recorded as exactly midnight or noon are somewhat over-represented.',
     'LAX, the Port, Downtown and the Hollywood tourist corridor all have real reported incidents and comparatively few residents.',
     'Coordinates are rounded to the hundred block; records with an unresolved address are not on the map.'),
    ('dc',
     'Washington publishes when an offence began where it is known, and otherwise when it was reported.',
     'The National Mall, the federal core and Union Station all have real reported incidents and almost no residents.',
     'Geocoded to the DC Master Address Repository and snapped to the street block. Only the nine Part I offence types are published, so DC has no simple-assault or vandalism layer.')
) AS v(source_id, occurrence_basis_note, denominator_examples_note, location_precision_note)
WHERE r.source_id = v.source_id;
