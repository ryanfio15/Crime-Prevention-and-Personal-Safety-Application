-- 019: Los Angeles history from 2010, out of LAPD's two legacy "Crime Data"
-- datasets (63jg-8b9z, 2010-2019; 2nrs-mtv8, 2020-2024) as well as the NIBRS
-- one. Which dataset covers which years, and why 2024 reads both, is in
-- safety/etl/adapters/los_angeles.py; the LAPD crime codes are mapped onto
-- NIBRS by rows in reference/crosswalk/los_angeles_v1.csv keyed 'CRM-<code>'.
--
-- Expand-only: a floor and a caveat. The previous release never reads either
-- for Los Angeles beyond what 018 already gave it, and history stays off until
-- the operator enables it per instance.

UPDATE reference.source_registry
SET history_start_date = DATE '2010-01-01',
    updated_at         = now()
WHERE source_id = 'lax';

INSERT INTO reference.source_series_caveat (source_id, period_from, period_to, kind, caveat_text)
SELECT 'lax', DATE '2010-01-01', DATE '2025-01-01', 'counting',
       'Los Angeles records before 2025 come wholly or partly from LAPD''s previous records system, '
       'which listed one crime per report under LAPD''s own crime codes, translated here to the '
       'categories used today. A report naming several offences counts once, so counts before 2025 '
       'run somewhat lower than today''s for the same activity, and LAPD moved to its new system '
       'over the course of 2024.'
WHERE EXISTS (SELECT 1 FROM reference.source_registry WHERE source_id = 'lax')
ON CONFLICT DO NOTHING;
