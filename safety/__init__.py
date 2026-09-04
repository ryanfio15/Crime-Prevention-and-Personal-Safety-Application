"""Crime Prevention & Personal Safety Application -- Phase 1 (Philadelphia).

Layout mirrors the layered architecture in the design doc (S5):

    safety.etl.adapters  per-source connectors (S8.1)
    safety.etl.bronze    raw snapshot store, object-storage stand-in (S9.2)
    safety.etl.validate  pre-promotion data-quality checks (S8.5)
    safety.etl.transform bronze -> silver, H3 computed at ingest (S6)
    safety.etl.gold      silver -> gold cell rollups (S9.3)
    safety.api           read-only serving layer over gold (S9.4)
"""

PIPELINE_VERSION = "phase1.0.0"
