"""Crime Prevention & Personal Safety Application -- Phase 2 (multi-city).

Layout mirrors the layered architecture in the design doc (S5):

    safety.etl.adapters  per-source connectors (S8.1)
    safety.etl.boundary  coverage polygons from TIGER/Line PLACE (S3.1)
    safety.etl.bronze    raw snapshot store, object-storage stand-in (S9.2)
    safety.etl.location  free-text location bucketing (S7.5)
    safety.etl.validate  pre-promotion data-quality checks (S8.5)
    safety.etl.transform bronze -> silver, H3 computed at ingest (S6)
    safety.etl.gold      silver -> gold cell rollups (S9.3)
    safety.etl.windows   fetch-window arithmetic, shared per source (S8.3)
    safety.api           read-only serving layer over gold (S9.4)
"""

# Stamped onto every etl.pull_run and silver.incident row, so "which pipeline
# produced this" stays answerable (S6). Bumped with Phase 2 because the answer
# genuinely changed: the crosswalk gained a code-only fallback tier, the
# all-hours ranking smooths through a different query plan, and the backfill
# window now honours a per-source floor. Rows written before this carry
# phase1.0.0 and keep it until they are reprocessed, which is the point of
# recording it rather than assuming it.
# phase2.1.0: gold windows are per city (30 days, 3/6/9 months, then years back
# to the oldest stored incident) and the old fixed four are renamed. Bumping it
# is also what makes the post-deploy `safety.ops` run rebuild every city's gold
# under the new names (ops._gold_reason).
PIPELINE_VERSION = "phase2.1.0"
