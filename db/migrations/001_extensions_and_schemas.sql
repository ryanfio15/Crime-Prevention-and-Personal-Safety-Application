-- 001: extensions and the layered schema namespaces (design doc S5).
--
-- The bronze layer itself lives in object storage (S9.2), so there is no
-- `bronze` schema holding payloads -- only an `etl` manifest that records what
-- was pulled and where the snapshot landed.

CREATE EXTENSION IF NOT EXISTS postgis;

-- Reference data: the source registry (S8.4) and the versioned offense
-- crosswalk (S7). Maintained datasets, not application code.
CREATE SCHEMA IF NOT EXISTS reference;

-- Pipeline bookkeeping: pull runs, validation issues, staging.
CREATE SCHEMA IF NOT EXISTS etl;

-- Layer 2: the standardized incident table (S6).
CREATE SCHEMA IF NOT EXISTS silver;

-- Layer 3: precomputed cell-level rollups, the only thing the API reads (S9.3).
CREATE SCHEMA IF NOT EXISTS gold;

COMMENT ON SCHEMA reference IS
    'Maintained reference data: source registry (S8.4) and versioned offense crosswalks (S7).';
COMMENT ON SCHEMA etl IS
    'Pipeline bookkeeping: pull runs, validation issues, staging tables.';
COMMENT ON SCHEMA silver IS
    'Layer 2 (S6): one canonical incident row per source incident, city-agnostic downstream.';
COMMENT ON SCHEMA gold IS
    'Layer 3 (S9.3): precomputed H3 cell rollups. The serving layer reads only this schema.';
