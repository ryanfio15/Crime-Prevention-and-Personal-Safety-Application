"""Pipeline orchestrator (design doc S8.2, S8.4).

Stands in for the workflow orchestrator S8.4 calls for (the Airflow / Prefect /
Dagster class of tool). The DAG shape is the same one that tool would run --
fetch -> validate -> transform -> refresh affected gold rollups -- expressed as
a CLI so Phase 1 has no scheduler dependency:

    python -m safety.etl.run backfill    --city phl [--months 24]
    python -m safety.etl.run incremental --city phl
    python -m safety.etl.run incremental --all --due-only --skip-hourly
    python -m safety.etl.run reprocess   --city phl --pull-id 3
    python -m safety.etl.run census      --city phl
    python -m safety.etl.run gold        --city phl
    python -m safety.etl.run hourly      --all
    python -m safety.etl.run weights     --city phl
    python -m safety.etl.run status

Schedules themselves are configuration, not code (S8.2): each source's cadence
lives in reference.source_registry, and `incremental` is safe to run more often
than a source actually publishes -- "checked, nothing new" is the normal case
for a bi-weekly source like Los Angeles.

Three flags turn that into something a scheduler can drive, and the deployed
configuration is `incremental --all --due-only --skip-hourly` on a few-hourly
schedule plus `hourly --all` weekly (deploy/systemd/safety-etl*@.timer):

* `--all` runs every enabled source, stalest first, and does not let one city's
  outage stop the other five -- which matters more than it sounds, because six
  independent government portals have six independent bad days.
* `--due-only` skips sources whose cadence says they cannot have new data yet, so
  the schedule only has to decide how often to *ask*. This is what keeps cadence
  in the registry rather than smeared across six cron expressions.
* `--skip-hourly` leaves the time-of-day layers out of the frequent run. They
  are the most expensive thing the pipeline builds and the slowest-moving, since
  both their windows are a year or wider.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import psycopg

from safety import PIPELINE_VERSION
from safety.db import connect, wait_for_db
from safety.etl import boundary as boundary_loader
from safety.etl import census, gold, transform, validate
from safety.etl.adapters import ADAPTERS, SourceConfig, get_adapter
from safety.etl.adapters.base import NormalizedIncident, RawChunk, SourceAdapter
from safety.etl.bronze import LocalBronzeStore, build_manifest
from safety.etl.windows import backfill_window
from safety.config import settings

log = logging.getLogger("safety.etl")


# ---------------------------------------------------------------------------
# Pull bookkeeping
# ---------------------------------------------------------------------------


def _open_pull(
    conn: psycopg.Connection,
    *,
    source_id: str,
    dataset: str,
    mode: str,
    since: datetime | None,
    until: datetime | None,
    crosswalk_version: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO etl.pull_run
                (source_id, dataset, mode, status, since_watermark,
                 window_start, window_end, crosswalk_version, pipeline_version)
            VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s)
            RETURNING pull_id
            """,
            (source_id, dataset, mode, since, since, until, crosswalk_version, PIPELINE_VERSION),
        )
        pull_id = cur.fetchone()["pull_id"]
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE reference.source_registry SET last_attempt_at = now() WHERE source_id = %s",
            (source_id,),
        )
    conn.commit()
    return pull_id


def _finish_pull(
    conn: psycopg.Connection,
    pull_id: int,
    *,
    status: str,
    started: float,
    bronze_uri: str | None = None,
    bronze_bytes: int = 0,
    fetched: int = 0,
    rejected: int = 0,
    valid: int = 0,
    upserted: int = 0,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE etl.pull_run
            SET status = %s, finished_at = now(), duration_seconds = %s,
                bronze_uri = %s, bronze_bytes = %s,
                records_fetched = %s, records_rejected = %s,
                records_valid = %s, records_upserted = %s, error = %s
            WHERE pull_id = %s
            """,
            (
                status,
                round(time.monotonic() - started, 2),
                bronze_uri,
                bronze_bytes,
                fetched,
                rejected,
                valid,
                upserted,
                error,
                pull_id,
            ),
        )
    conn.commit()


def _mark_source_success(conn: psycopg.Connection, source_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reference.source_registry r
            SET last_success_at = now(),
                last_status = 'succeeded',
                last_error = NULL,
                updated_at = now(),
                last_success_watermark = (
                    SELECT max(occurred_at) FROM silver.incident WHERE source_id = r.source_id
                )
            WHERE r.source_id = %s
            """,
            (source_id,),
        )
    conn.commit()


def _mark_source_failure(conn: psycopg.Connection, source_id: str, status: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reference.source_registry
            SET last_status = %s, last_error = %s, updated_at = now()
            WHERE source_id = %s
            """,
            (status, error[:2000], source_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


def ensure_boundary(
    conn: psycopg.Connection, adapter: SourceAdapter, config: SourceConfig, force: bool = False
) -> None:
    """Fetch and store the city coverage polygon (S3.1) if not already present.

    Two ways a city can get one. An adapter that knows a boundary layer on its
    own portal returns it from `fetch_boundary` -- Philadelphia does, building
    the police-jurisdiction polygon S3.1 explicitly allows. An adapter that
    returns None falls through to the shared TIGER/Line PLACE loader, which is
    where the other five get theirs. See safety/etl/boundary.py for why that is
    one loader rather than five portal integrations.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 AS present FROM reference.city_boundary WHERE source_id = %s",
            (config.source_id,),
        )
        present = cur.fetchone() is not None
    if present and not force:
        return

    started = time.monotonic()
    chunk = adapter.fetch_boundary()
    if chunk is None:
        _ensure_place_boundary(conn, config)
        return

    pull_id = _open_pull(
        conn,
        source_id=config.source_id,
        dataset=config.boundary_dataset or "boundary",
        mode="boundary",
        since=None,
        until=None,
        crosswalk_version=config.crosswalk_version,
    )
    try:
        store = LocalBronzeStore()
        pull = store.open_pull(config.source_id, "boundary", pull_id)
        pull.write_chunk(chunk)
        pull.write_manifest(
            build_manifest(
                source_id=config.source_id,
                dataset=config.boundary_dataset or "boundary",
                pull_id=pull_id,
                mode="boundary",
                since=None,
                until=None,
                chunks=[chunk],
                pipeline_version=PIPELINE_VERSION,
                crosswalk_version=config.crosswalk_version,
                attribution=config.attribution_text,
            )
        )

        feature = json.loads(chunk.payload)["features"][0]
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reference.city_boundary
                    (source_id, boundary_kind, geom, area_km2, source_note, fetched_at)
                VALUES (
                    %s, %s,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                    ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography) / 1e6,
                    %s, now()
                )
                ON CONFLICT (source_id) DO UPDATE SET
                    boundary_kind = EXCLUDED.boundary_kind,
                    geom          = EXCLUDED.geom,
                    area_km2      = EXCLUDED.area_km2,
                    source_note   = EXCLUDED.source_note,
                    fetched_at    = EXCLUDED.fetched_at
                """,
                (
                    config.source_id,
                    feature["properties"].get("boundary_kind", "city_limits"),
                    json.dumps(feature["geometry"]),
                    json.dumps(feature["geometry"]),
                    feature["properties"].get("derived_from"),
                ),
            )
        conn.commit()
        _finish_pull(
            conn,
            pull_id,
            status="succeeded",
            started=started,
            bronze_uri=pull.uri,
            bronze_bytes=pull.total_bytes,
            fetched=1,
            valid=1,
        )
        log.info("stored coverage boundary for %s", config.source_id)
    except Exception as exc:
        _finish_pull(conn, pull_id, status="failed", started=started, error=str(exc))
        raise


def _ensure_place_boundary(conn: psycopg.Connection, config: SourceConfig) -> None:
    """Load the coverage polygon from TIGER/Line PLACE (safety/etl/boundary.py).

    Gets its own etl.pull_run row and its own bronze snapshot, the same standing
    a police department's data gets: "what did the source publish on this date"
    is the same question here, and the polygon is the denominator of every
    percentile in the city.
    """
    started = time.monotonic()
    pull_id = _open_pull(
        conn,
        source_id=config.source_id,
        dataset=boundary_loader.DATASET,
        mode="boundary",
        since=None,
        until=None,
        crosswalk_version=config.crosswalk_version,
    )
    try:
        chunk = boundary_loader.fetch_place_file(config)
        store = LocalBronzeStore()
        pull = store.open_pull(config.source_id, boundary_loader.DATASET, pull_id)
        pull.write_chunk(chunk)
        pull.write_manifest(
            build_manifest(
                source_id=config.source_id,
                dataset=boundary_loader.DATASET,
                pull_id=pull_id,
                mode="boundary",
                since=None,
                until=None,
                chunks=[chunk],
                pipeline_version=PIPELINE_VERSION,
                crosswalk_version=config.crosswalk_version,
                attribution=boundary_loader.ATTRIBUTION,
            )
        )
        chosen = boundary_loader.load_boundary(conn, chunk.payload, config)
        _finish_pull(
            conn,
            pull_id,
            status="succeeded",
            started=started,
            bronze_uri=pull.uri,
            bronze_bytes=pull.total_bytes,
            fetched=1,
            valid=1,
        )
        log.info(
            "%s coverage area %.1f km2 from %s",
            config.source_id,
            chosen["area_km2"],
            chosen["namelsad"],
        )
    except Exception as exc:
        _finish_pull(conn, pull_id, status="failed", started=started, error=str(exc))
        raise


def _city_bbox(
    conn: psycopg.Connection, source_id: str
) -> tuple[float, float, float, float] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ST_XMin(geom::box2d) AS west, ST_YMin(geom::box2d) AS south,
                   ST_XMax(geom::box2d) AS east, ST_YMax(geom::box2d) AS north
            FROM reference.city_boundary WHERE source_id = %s
            """,
            (source_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row["west"], row["south"], row["east"], row["north"]


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _ingest(
    conn: psycopg.Connection,
    config: SourceConfig,
    *,
    mode: str,
    since: datetime,
    until: datetime,
) -> dict[str, int | str]:
    adapter = get_adapter(config)
    ensure_boundary(conn, adapter, config)
    bbox = _city_bbox(conn, config.source_id)

    started = time.monotonic()
    pull_id = _open_pull(
        conn,
        source_id=config.source_id,
        dataset=config.incident_dataset,
        mode=mode,
        since=since,
        until=until,
        crosswalk_version=config.crosswalk_version,
    )
    log.info(
        "pull %s: %s %s from %s to %s",
        pull_id,
        config.source_id,
        mode,
        since.date(),
        until.date(),
    )

    try:
        # --- fetch, writing every chunk to bronze verbatim before parsing ---
        store = LocalBronzeStore()
        pull = store.open_pull(config.source_id, config.incident_dataset, pull_id)
        chunks: list[RawChunk] = []
        chunk_counts: dict[str, int] = {}
        for chunk in adapter.fetch_incidents(since, until):
            pull.write_chunk(chunk)
            chunk_counts[chunk.name] = chunk.record_count or 0
            # Keep the payload only long enough to parse it; the durable copy
            # is already on disk.
            chunks.append(chunk)

        pull.write_manifest(
            build_manifest(
                source_id=config.source_id,
                dataset=config.incident_dataset,
                pull_id=pull_id,
                mode=mode,
                since=since,
                until=until,
                chunks=chunks,
                pipeline_version=PIPELINE_VERSION,
                crosswalk_version=config.crosswalk_version,
                attribution=config.attribution_text,
            )
        )

        # --- parse + normalize (adapter-owned, city-specific) ---------------
        records: list[NormalizedIncident] = []
        for chunk in chunks:
            for raw in adapter.parse_incidents(chunk):
                normalized = adapter.normalize(raw)
                if normalized is not None:
                    records.append(normalized)
        fetched = len(records)
        log.info("pull %s: normalized %s records", pull_id, fetched)

        if fetched == 0:
            _finish_pull(
                conn,
                pull_id,
                status="no_new_data",
                started=started,
                bronze_uri=pull.uri,
                bronze_bytes=pull.total_bytes,
            )
            log.info("pull %s: nothing new (this is normal for a lagging source)", pull_id)
            return {"pull_id": pull_id, "status": "no_new_data", "upserted": 0}

        # --- validate (shared, S8.5) ---------------------------------------
        result = validate.validate_records(
            records,
            config=config,
            bbox=bbox,
            chunk_counts=chunk_counts,
            historical_median=validate.historical_pull_median(conn, config.source_id, mode),
        )
        validate.record_issues(conn, pull_id, config.source_id, result.issues)
        conn.commit()

        if result.blocked:
            _finish_pull(
                conn,
                pull_id,
                status="blocked",
                started=started,
                bronze_uri=pull.uri,
                bronze_bytes=pull.total_bytes,
                fetched=fetched,
                rejected=result.rejected,
                valid=len(result.accepted),
                error=result.block_reason,
            )
            _mark_source_failure(conn, config.source_id, "blocked", result.block_reason or "")
            log.error("pull %s BLOCKED before promotion: %s", pull_id, result.block_reason)
            return {"pull_id": pull_id, "status": "blocked", "upserted": 0}

        # --- transform + promote -------------------------------------------
        transform.stage_records(
            conn,
            pull_id=pull_id,
            source_id=config.source_id,
            dataset=config.incident_dataset,
            records=result.accepted,
        )
        upserted = transform.promote_to_silver(
            conn,
            pull_id=pull_id,
            source_id=config.source_id,
            crosswalk_version=config.crosswalk_version,
            pipeline_version=PIPELINE_VERSION,
        )
        conn.commit()

        transform.flag_unmapped_offenses(
            conn,
            pull_id=pull_id,
            source_id=config.source_id,
            crosswalk_version=config.crosswalk_version,
        )
        transform.clear_staging(conn, pull_id)
        conn.commit()

        _finish_pull(
            conn,
            pull_id,
            status="succeeded",
            started=started,
            bronze_uri=pull.uri,
            bronze_bytes=pull.total_bytes,
            fetched=fetched,
            rejected=result.rejected,
            valid=len(result.accepted),
            upserted=upserted,
        )
        _mark_source_success(conn, config.source_id)
        return {
            "pull_id": pull_id,
            "status": "succeeded",
            "fetched": fetched,
            "rejected": result.rejected,
            "upserted": upserted,
        }

    except Exception as exc:
        conn.rollback()
        _finish_pull(conn, pull_id, status="failed", started=started, error=repr(exc))
        _mark_source_failure(conn, config.source_id, "failed", repr(exc))
        raise


# ---------------------------------------------------------------------------
# Running across several cities
# ---------------------------------------------------------------------------


# How long a source's published cadence says to wait between looks. S8.2 puts
# cadence in the registry rather than in job code precisely so this table is the
# only place it lives -- a scheduler then only has to ask "is anyone due?".
#
# Keys must cover reference.source_registry.expected_cadence's CHECK constraint
# (002_reference.sql). Adding a value there without adding it here is survivable
# -- is_due() pulls an unrecognised cadence rather than skipping it silently, and
# says so -- but it means that source is checked on every tick.
_CADENCE_DAYS = {
    "daily": 1.0,
    "weekly": 7.0,
    "biweekly": 14.0,
    "annual": 365.0,
    # A rolling "last N days" feed republishes continuously, so it is always
    # worth a look. S8.2 wants these checked more often than the others, and with
    # a scheduler firing every few hours that is what a zero interval produces.
    "rolling": 0.0,
}

# Fraction of the interval after which a source counts as due. Below 1.0 on
# purpose: a scheduler fires on its own rhythm, not the source's, so requiring a
# full interval would push a daily source to nearly every other day whenever the
# cron tick landed just short. At 0.8 a daily source becomes due after ~19 hours,
# which any sub-daily schedule then picks up once per day.
_DUE_FRACTION = 0.8

# Only these modes count as "having looked" for cadence purposes. A census or
# boundary pull writes an etl.pull_run row too, and letting one of those suppress
# an incident pull would be wrong.
_INCIDENT_MODES = ("backfill", "incremental")

# And only these outcomes. A pull that reached the source and came back -- with
# rows or with nothing -- is a completed look; anything else is not.
#
# 'failed' and 'blocked' are deliberately absent, and getting this wrong is worse
# than it sounds. A source that 404s, times out, or changes its export format
# overnight would otherwise count as checked and be left alone for a full cadence
# interval, so a transient outage at 03:00 would mean no data until tomorrow --
# with a scheduler running every six hours and precisely the condition a retry
# would fix.
#
# 'running' is absent too, for the opposite reason. A container killed mid-pull
# leaves its row at 'running' forever; counting that as a look would suppress
# every retry indefinitely. The cost is that a genuinely in-flight pull does not
# block a second one, which needs a run to overrun its own interval -- 19 hours
# against a twelve-minute refresh. The benign failure is the right one to pick.
_COMPLETED_STATUSES = ("succeeded", "no_new_data")

_SOURCES_SQL = f"""
SELECT
    r.source_id,
    r.expected_cadence,
    r.last_success_at,
    -- When an incident pull last *completed*, which is a different question from
    -- when one last succeeded. `last_success_at` only moves on a succeeded pull,
    -- and `_ingest` returns early with 'no_new_data' without touching it -- the
    -- normal outcome for a bi-weekly source. Keying "due" on success would
    -- therefore leave Los Angeles permanently overdue and checked every single
    -- tick, which is the exact waste this flag exists to avoid.
    --
    -- Completed, though, not merely started: see _COMPLETED_STATUSES for why a
    -- failed pull must not count as a look.
    checks.last_checked_at,
    EXTRACT(EPOCH FROM (now() - checks.last_checked_at)) / 86400.0 AS days_since_check
FROM reference.source_registry r
CROSS JOIN LATERAL (
    SELECT max(started_at) AS last_checked_at
    FROM etl.pull_run p
    WHERE p.source_id = r.source_id
      AND p.mode   = ANY(%(modes)s)
      AND p.status = ANY(%(statuses)s)
) checks
WHERE r.enabled
ORDER BY checks.last_checked_at ASC NULLS FIRST, r.source_id
"""


def enabled_sources(conn: psycopg.Connection, due_only: bool = False) -> list[str]:
    """Enabled sources, stalest first.

    Ordered by when each was last looked at rather than alphabetically, so a run
    cut short -- a timeout, a container restart, a platform redeploy -- has spent
    its time on the cities that needed it most. NULLS FIRST puts a city that has
    never loaded at the front, which is where it belongs.

    With `due_only`, sources whose published cadence says they cannot have new
    data yet are skipped. That is what lets one scheduled job cover six sources
    on five different cadences: the schedule decides how often to *ask*, and the
    registry decides who actually gets pulled (S8.2).
    """
    with conn.cursor() as cur:
        cur.execute(
            _SOURCES_SQL,
            {
                "modes": list(_INCIDENT_MODES),
                "statuses": list(_COMPLETED_STATUSES),
            },
        )
        rows = cur.fetchall()

    if not due_only:
        return [row["source_id"] for row in rows]

    due: list[str] = []
    for row in rows:
        ok, reason = is_due(row["expected_cadence"], row["days_since_check"])
        log.log(
            logging.WARNING if "unknown cadence" in reason else logging.INFO,
            "%s %s: %s",
            row["source_id"],
            "is due" if ok else "not due",
            reason,
        )
        if ok:
            due.append(row["source_id"])
    return due


def is_due(cadence: str | None, days_since_check: float | None) -> tuple[bool, str]:
    """Whether a source's cadence says it is worth pulling, and why.

    Pure, so the arithmetic can be exercised without a database. Returns the
    decision and a human-readable reason, which the caller logs -- a schedule
    that quietly skips a city is indistinguishable from one that is broken.
    """
    interval = _CADENCE_DAYS.get(cadence or "")
    if interval is None:
        # A cadence the table does not know is a registry problem, not a reason to
        # skip a city. Pull it, and say why it was pulled.
        return True, f"unknown cadence {cadence!r}; pulling rather than skipping"

    if days_since_check is None:
        return True, "never pulled"

    threshold = interval * _DUE_FRACTION
    detail = (
        f"last checked {days_since_check * 24:.1f}h ago, cadence "
        f"{cadence!r} waits {threshold * 24:.1f}h"
    )
    return days_since_check >= threshold, detail


def _fan_out(args: argparse.Namespace, one: Callable[[argparse.Namespace], int]) -> int:
    """Run a single-city command across every enabled source.

    One city's failure must not stop the other five. A source can be down, or
    have changed its export format overnight, and that is a normal Tuesday for
    six independent government portals -- so each is attempted, failures are
    collected, and the summary names them. The exit code is non-zero if any
    city failed, so a scheduler still notices.
    """
    due_only = getattr(args, "due_only", False)
    with connect() as conn:
        sources = enabled_sources(conn, due_only=due_only)

    if not sources:
        if due_only:
            # Not a failure, and importantly not reported as one: with a schedule
            # firing more often than any source publishes, "nobody is due" is the
            # normal outcome of most runs. Exiting non-zero here would light up a
            # platform's failure alerting several times a day.
            print(
                json.dumps({"attempted": [], "skipped": "no source is due"}, indent=2)
            )
            return 0
        print("No enabled sources in reference.source_registry.", file=sys.stderr)
        return 1

    log.info("running across %s source(s): %s", len(sources), ", ".join(sources))
    failures: dict[str, str] = {}
    for source_id in sources:
        per_city = argparse.Namespace(**{**vars(args), "city": source_id, "all": False})
        try:
            if one(per_city) != 0:
                failures[source_id] = "command returned non-zero"
        except Exception as exc:
            # Logged with a traceback, recorded, and moved past. The per-city
            # failure is already durable in etl.pull_run and the registry's
            # last_error by the time it reaches here.
            log.exception("%s failed", source_id)
            failures[source_id] = repr(exc)

    print(
        json.dumps(
            {
                "attempted": sources,
                "succeeded": [s for s in sources if s not in failures],
                "failed": failures,
            },
            indent=2,
            default=str,
        )
    )
    return 1 if failures else 0


def _fannable(one: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    """Wrap a single-city command so `--all` fans it out over every source."""

    def dispatch(args: argparse.Namespace) -> int:
        if getattr(args, "all", False):
            return _fan_out(args, one)
        return one(args)

    return dispatch


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_backfill(args: argparse.Namespace) -> int:
    months = args.months or settings.backfill_months
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        _require_enabled(config)
        since, until = backfill_window(months, config)
        outcome = _ingest(conn, config, mode="backfill", since=since, until=until)
        if outcome["status"] == "succeeded":
            stats = gold.refresh_all(
                conn,
                config.source_id,
                PIPELINE_VERSION,
                include_hourly=not args.skip_hourly,
            )
            outcome.update(stats)
    print(json.dumps(outcome, indent=2, default=str))
    return 0


def cmd_incremental(args: argparse.Namespace) -> int:
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        _require_enabled(config)

        until = datetime.now(timezone.utc) + timedelta(days=1)
        if config.last_success_watermark is None:
            log.info("no watermark for %s; falling back to a full backfill", config.source_id)
            since, until = backfill_window(settings.backfill_months, config)
        else:
            # S8.3: re-read behind the watermark and upsert, because these
            # agencies revise and reclassify after initial publication.
            since = config.last_success_watermark - timedelta(
                days=config.revision_lookback_days
            )

        outcome = _ingest(conn, config, mode="incremental", since=since, until=until)
        if outcome["status"] == "succeeded":
            stats = gold.refresh_all(
                conn,
                config.source_id,
                PIPELINE_VERSION,
                include_hourly=not args.skip_hourly,
            )
            outcome.update(stats)
    print(json.dumps(outcome, indent=2, default=str))
    return 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    """Re-run transform + gold from a stored bronze snapshot, without refetching.

    This is the capability S5 says the bronze layer exists to provide: replay
    exactly what the city published on a given date through updated
    standardization logic.
    """
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        adapter = get_adapter(config)
        with conn.cursor() as cur:
            if args.pull_id is None:
                # Replaying the most recent snapshot is the common case by far,
                # and looking the id up first costs a whole deploy cycle on a
                # platform where commands are service start commands.
                cur.execute(
                    """
                    SELECT * FROM etl.pull_run
                    WHERE source_id = %s AND bronze_uri IS NOT NULL
                      AND status = 'succeeded'
                      -- Incident pulls only. Census and boundary pulls write
                      -- bronze too, and replaying a LODES archive through an
                      -- incident adapter fails on the first byte.
                      AND mode IN ('backfill', 'incremental')
                    ORDER BY pull_id DESC LIMIT 1
                    """,
                    (args.city,),
                )
            else:
                cur.execute(
                    "SELECT * FROM etl.pull_run WHERE pull_id = %s AND source_id = %s",
                    (args.pull_id, args.city),
                )
            original = cur.fetchone()
        if original is None or not original["bronze_uri"]:
            target = args.pull_id if args.pull_id is not None else "any pull"
            print(f"No stored bronze snapshot for {target}", file=sys.stderr)
            return 1
        log.info(
            "replaying pull %s (%s)", original["pull_id"], original["bronze_uri"]
        )

        started = time.monotonic()
        pull_id = _open_pull(
            conn,
            source_id=config.source_id,
            dataset=config.incident_dataset,
            mode="backfill",
            since=original["window_start"],
            until=original["window_end"],
            crosswalk_version=config.crosswalk_version,
        )
        stored = LocalBronzeStore().open_existing(original["bronze_uri"])

        records: list[NormalizedIncident] = []
        for name, payload in stored.iter_chunks():
            chunk = RawChunk(
                name=name,
                content_type="text/csv",
                payload=payload,
                request_url=original["bronze_uri"],
                fetched_at=original["started_at"],
            )
            for raw in adapter.parse_incidents(chunk):
                normalized = adapter.normalize(raw)
                if normalized is not None:
                    records.append(normalized)

        result = validate.validate_records(
            records, config=config, bbox=_city_bbox(conn, config.source_id)
        )
        validate.record_issues(conn, pull_id, config.source_id, result.issues)
        transform.stage_records(
            conn,
            pull_id=pull_id,
            source_id=config.source_id,
            dataset=config.incident_dataset,
            records=result.accepted,
        )
        upserted = transform.promote_to_silver(
            conn,
            pull_id=pull_id,
            source_id=config.source_id,
            crosswalk_version=config.crosswalk_version,
            pipeline_version=PIPELINE_VERSION,
        )
        transform.flag_unmapped_offenses(
            conn,
            pull_id=pull_id,
            source_id=config.source_id,
            crosswalk_version=config.crosswalk_version,
        )
        transform.clear_staging(conn, pull_id)
        conn.commit()
        _finish_pull(
            conn,
            pull_id,
            status="succeeded",
            started=started,
            bronze_uri=original["bronze_uri"],
            fetched=len(records),
            rejected=result.rejected,
            valid=len(result.accepted),
            upserted=upserted,
        )
        stats = gold.refresh_all(conn, config.source_id, PIPELINE_VERSION)

    print(json.dumps({"pull_id": pull_id, "upserted": upserted, **stats}, indent=2, default=str))
    return 0


def cmd_census(args: argparse.Namespace) -> int:
    """Load the population denominator: census blocks, then workplace jobs.

    Two pulls rather than one, because they are two published datasets on
    different release cycles -- the decennial population will not move until
    2030, while LODES is annual. Recording them separately in etl.pull_run
    keeps "where did this number come from" answerable per source, which is the
    same reason a police pull gets its own row.
    """
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        # The boundary has to exist before the blocks land: census.load_blocks
        # trims the county prefilter down to the blocks actually inside the city,
        # and without a boundary it cannot, leaving every citywide population
        # figure describing the counties instead. Cheap and idempotent when the
        # boundary is already stored.
        ensure_boundary(conn, get_adapter(config), config)
        store = LocalBronzeStore()
        loaded: dict[str, int] = {}

        for dataset, fetch, load in (
            (
                census.BLOCKS_DATASET,
                lambda: census.fetch_blocks(config),
                lambda payload: census.load_blocks(conn, payload, config),
            ),
            (
                census.JOBS_DATASET,
                lambda: census.fetch_jobs(config, args.lodes_year),
                lambda payload: census.load_jobs(
                    conn, payload, config.source_id, args.lodes_year
                ),
            ),
        ):
            started = time.monotonic()
            pull_id = _open_pull(
                conn,
                source_id=config.source_id,
                dataset=dataset,
                mode="reference",
                since=None,
                until=None,
                crosswalk_version=config.crosswalk_version,
            )
            try:
                if args.replay:
                    payload, uri, size = _replay_reference(conn, config.source_id, dataset)
                else:
                    chunk = fetch()
                    pull = store.open_pull(config.source_id, dataset, pull_id)
                    pull.write_chunk(chunk)
                    pull.write_manifest(
                        build_manifest(
                            source_id=config.source_id,
                            dataset=dataset,
                            pull_id=pull_id,
                            mode="reference",
                            since=None,
                            until=None,
                            chunks=[chunk],
                            pipeline_version=PIPELINE_VERSION,
                            crosswalk_version=config.crosswalk_version,
                            attribution=_CENSUS_ATTRIBUTION[dataset],
                        )
                    )
                    payload, uri, size = chunk.payload, pull.uri, pull.total_bytes

                rows = load(payload)
                loaded[dataset] = rows
                _finish_pull(
                    conn,
                    pull_id,
                    status="succeeded",
                    started=started,
                    bronze_uri=uri,
                    bronze_bytes=size,
                    fetched=rows,
                    valid=rows,
                    upserted=rows,
                )
            except Exception as exc:
                _finish_pull(
                    conn, pull_id, status="failed", started=started, error=str(exc)
                )
                raise

        # The cell universe has to exist before anything can be apportioned into
        # it. On a first run it will not, and that is a `gold` refresh away --
        # which itself calls back into this, so the ordering resolves either way.
        #
        # rebuild=True because the blocks were just reloaded: a gold refresh only
        # tops up the cells that have no figure yet, which is the right default
        # when the denominator has not moved and the wrong one here.
        cells = gold.refresh_cell_exposure(conn, config.source_id, rebuild=True)

    print(
        json.dumps(
            {
                "city": args.city,
                "blocks": loaded.get(census.BLOCKS_DATASET, 0),
                "job_rows": loaded.get(census.JOBS_DATASET, 0),
                "exposure_cells": cells,
                "next": (
                    f"python -m safety.etl.run safety --city {args.city} "
                    "  # rebuild the ranking on the new denominator"
                ),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_release_geometry(args: argparse.Namespace) -> int:
    """Reclaim the census block polygons once the exposure layer is built.

    A disk-space command, not a pipeline stage: nothing downstream needs it run,
    and running it costs a TIGER re-download before the apportionment can be
    redone. Worth it on a small volume, where six metros of block geometry is
    the largest reference data in the database and the serving layer never reads
    a byte of it.
    """
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        released = census.release_block_geometry(conn, config.source_id)

    print(
        json.dumps(
            {
                "city": args.city,
                "blocks_released": released,
                "restore": f"python -m safety.etl.run census --city {args.city}",
            },
            indent=2,
            default=str,
        )
    )
    return 0


# Attribution per dataset, carried into the bronze manifest. S12 treats naming
# the source as a requirement rather than a courtesy, and that applies to the
# Census Bureau exactly as it does to a police department.
_CENSUS_ATTRIBUTION = {
    "census_blocks": (
        "Population and block geography: U.S. Census Bureau, 2020 Census "
        "Redistricting Data, TIGER/Line Shapefiles (TABBLOCK20)."
    ),
    "lodes_wac": (
        "Workplace jobs: U.S. Census Bureau, Longitudinal Employer-Household "
        "Dynamics, LODES version 8 Workplace Area Characteristics."
    ),
}


def _replay_reference(
    conn: psycopg.Connection, source_id: str, dataset: str
) -> tuple[bytes, str, int]:
    """Re-read the newest stored snapshot instead of re-downloading it.

    The TIGER archive is large and the Census Bureau serves it slowly; iterating
    on the loader should not mean fetching it again each time. Same replay path
    `reprocess` uses for incident data (S5).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bronze_uri, bronze_bytes FROM etl.pull_run
            WHERE source_id = %s AND dataset = %s AND bronze_uri IS NOT NULL
              AND status = 'succeeded'
            ORDER BY pull_id DESC LIMIT 1
            """,
            (source_id, dataset),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(
            f"no stored '{dataset}' snapshot for '{source_id}' to replay; "
            "run without --replay once first"
        )
    pull = LocalBronzeStore().open_existing(row["bronze_uri"])
    chunks = list(pull.iter_chunks())
    if not chunks:
        raise LookupError(f"bronze snapshot at {row['bronze_uri']} is empty")
    return chunks[0][1], row["bronze_uri"], row["bronze_bytes"] or 0


def cmd_gold(args: argparse.Namespace) -> int:
    with connect() as conn:
        stats = gold.refresh_all(
            conn, args.city, PIPELINE_VERSION, include_hourly=not args.skip_hourly
        )
    print(json.dumps(stats, indent=2, default=str))
    return 0


def cmd_safety(args: argparse.Namespace) -> int:
    """Rebuild only the safety ranking, so retuning a scheme is cheap.

    A full `gold` run rebuilds the cell universe and every rollup. Tuning the
    severity weights only invalidates this one layer, and iterating is the whole
    point of keeping the weights in a reference table.
    """
    with connect() as conn:
        anchor = gold.data_anchor(conn, args.city)
        if anchor is None:
            raise LookupError(f"no silver rows for '{args.city}'; nothing to rank")
        windows = gold.resolve_windows(anchor)
        rows, coverage = gold.refresh_safety_layer(conn, args.city, windows, args.scheme)
        gold.refresh_city_snapshot(conn, args.city, PIPELINE_VERSION, coverage)
        conn.commit()

    print(
        json.dumps(
            {
                "city": args.city,
                "scheme": args.scheme or "all enabled",
                "cell_safety_rows": rows,
                "severity_weight_coverage": round(coverage, 4) if coverage else None,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_hourly(args: argparse.Namespace) -> int:
    """Rebuild only the time-of-day layers.

    Separate from `safety` because it is the expensive one -- the same ranking
    recomputed 24 times over -- and because it depends on the all-hours ranking
    already being current. Run `safety` first if the scheme changed.
    """
    with connect() as conn:
        anchor = gold.data_anchor(conn, args.city)
        if anchor is None:
            raise LookupError(f"no silver rows for '{args.city}'; nothing to rank")
        windows = gold.resolve_windows(anchor)
        rows, profile_rows, share = gold.refresh_hourly_layer(
            conn, args.city, windows, args.scheme
        )
        gold.refresh_city_snapshot(
            conn, args.city, PIPELINE_VERSION, hour_coverage_share=share
        )
        conn.commit()

    print(
        json.dumps(
            {
                "city": args.city,
                "scheme": args.scheme or "all enabled",
                "resolutions": list(gold.HOURLY_RESOLUTIONS),
                "windows": list(gold.HOURLY_WINDOWS),
                "cell_hour_safety_rows": rows,
                "cell_hour_profile_rows": profile_rows,
                "hour_known_share": round(share, 4),
            },
            indent=2,
            default=str,
        )
    )
    return 0


_COMPARE_SQL = """
WITH a AS (
    SELECT h3_index, safety_percentile AS pct
    FROM gold.cell_safety
    WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
      AND time_window = %(window)s AND track = %(track)s
      AND scheme_version = %(a)s
),
b AS (
    SELECT h3_index, safety_percentile AS pct
    FROM gold.cell_safety
    WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
      AND time_window = %(window)s AND track = %(track)s
      AND scheme_version = %(b)s
)
SELECT
    count(*)                        AS cells,
    corr(a.pct, b.pct)              AS rank_correlation,
    avg(abs(a.pct - b.pct))         AS mean_abs_shift,
    max(abs(a.pct - b.pct))         AS max_abs_shift
FROM a JOIN b USING (h3_index)
"""

# The percentiles are already ranks, so Pearson on them is Spearman on the
# underlying values -- no separate rank transform needed.
_MOVERS_SQL = """
SELECT a.h3_index,
       a.safety_percentile AS pct_a,
       b.safety_percentile AS pct_b,
       b.safety_percentile - a.safety_percentile AS shift,
       a.incident_count,
       a.weighted_total AS weighted_a,
       b.weighted_total AS weighted_b
FROM gold.cell_safety a
JOIN gold.cell_safety b
  ON  b.source_id = a.source_id AND b.h3_index = a.h3_index
  AND b.h3_res = a.h3_res AND b.time_window = a.time_window
  AND b.track = a.track AND b.scheme_version = %(b)s
WHERE a.source_id = %(source_id)s AND a.h3_res = %(h3_res)s
  AND a.time_window = %(window)s AND a.track = %(track)s
  AND a.scheme_version = %(a)s
ORDER BY abs(b.safety_percentile - a.safety_percentile) DESC
LIMIT 10
"""

# The unweighted density percentile this layer is meant to improve on. If the
# two agree almost perfectly, the severity weighting is not earning its keep --
# a known risk, since reported index crimes are highly correlated.
_VS_ACTIVITY_SQL = """
SELECT
    count(*)           AS cells,
    -- cell_activity.percentile runs low-to-high on density; safety runs the
    -- other way, so a strong relationship shows up as a correlation near -1.
    corr(s.safety_percentile, c.percentile) AS correlation
FROM gold.cell_safety s
JOIN gold.cell_activity c
  ON  c.source_id = s.source_id AND c.h3_index = s.h3_index
  AND c.h3_res = s.h3_res AND c.time_window = s.time_window
  AND c.category = %(category)s
WHERE s.source_id = %(source_id)s AND s.h3_res = %(h3_res)s
  AND s.time_window = %(window)s AND s.track = %(track)s
  AND s.scheme_version = %(scheme)s
"""


def cmd_safety_compare(args: argparse.Namespace) -> int:
    """Diff two severity schemes, and both against the unweighted ranking."""
    params = {
        "source_id": args.city,
        "h3_res": args.res,
        "window": args.window,
        "track": args.track,
        "a": args.a,
        "b": args.b,
    }
    # The non-violent track is spread across three product categories, so the
    # closest single comparison on the unweighted layer is the city-wide one.
    category = "violent" if args.track == "violent" else "all"

    with connect() as conn, conn.cursor() as cur:
        cur.execute(_COMPARE_SQL, params)
        summary = cur.fetchone()
        cur.execute(_MOVERS_SQL, params)
        movers = cur.fetchall()
        cur.execute(
            _VS_ACTIVITY_SQL,
            {**params, "scheme": args.a, "category": category},
        )
        versus = cur.fetchone()

    if not summary or not summary["cells"]:
        # The usual cause is now a deliberate one: a superseded scheme is
        # disabled in schemes.csv so the pipeline stops keeping a second complete
        # copy of the ranking, and `python -m safety.migrate` reclaims the rows it
        # had written. Building one on demand is the supported way back -- `safety
        # --scheme` takes a disabled scheme precisely so a comparison can be
        # re-run -- with the caveat that the next migrate will prune it again.
        print(
            f"No overlapping cells for schemes '{args.a}' and '{args.b}'.\n"
            f"Build both first, e.g.:\n"
            f"  python -m safety.etl.run safety --city {args.city} --scheme {args.a}\n"
            f"  python -m safety.etl.run safety --city {args.city} --scheme {args.b}\n"
            "A scheme disabled in reference/severity/schemes.csv still builds when "
            "named explicitly, but `python -m safety.migrate` prunes its rows again "
            "afterwards; re-enable it there if you want it kept."
        )
        return 1

    print(f"{args.city} res={args.res} window={args.window} track={args.track}")
    print(f"  cells compared       {summary['cells']}")
    print(f"  rank correlation     {summary['rank_correlation']:.4f}")
    print(f"  mean |shift|         {summary['mean_abs_shift']:.4f}")
    print(f"  max  |shift|         {summary['max_abs_shift']:.4f}")
    if versus and versus["correlation"] is not None:
        print(
            f"\n  '{args.a}' vs unweighted cell_activity.percentile "
            f"(category={category}): {versus['correlation']:+.4f}"
        )
        print(
            "  (near -1 means the severity weighting reproduces the raw density "
            "ranking and is adding little)"
        )

    print(f"\n  biggest movers, '{args.a}' -> '{args.b}':")
    print(f"  {'cell':<18} {'n':>5} {'pct A':>8} {'pct B':>8} {'shift':>8}")
    for row in movers:
        print(
            f"  {row['h3_index']:<18} {row['incident_count']:>5} "
            f"{row['pct_a']:>8.4f} {row['pct_b']:>8.4f} {row['shift']:>+8.4f}"
        )
    return 0


def _readiness(conn: psycopg.Connection, config: SourceConfig) -> list[str]:
    """What is still missing before this source can produce a correct map.

    Checked rather than trusted, because the failure mode of enabling too early
    is not a crash. A city with no crosswalk loads perfectly happily and files
    every incident as product_category 'other', severity_bucket 'unknown' --
    a complete, plausible, wrong map. That is the one outcome worth a gate.
    """
    problems: list[str] = []

    if config.source_id not in ADAPTERS:
        problems.append(
            f"no adapter registered for '{config.source_id}'; implemented: "
            f"{sorted(ADAPTERS)}"
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)::int AS n FROM reference.offense_crosswalk
            WHERE source_id = %s AND crosswalk_version = %s
            """,
            (config.source_id, config.crosswalk_version),
        )
        mapped = cur.fetchone()["n"]
    if not mapped:
        problems.append(
            f"no crosswalk rows for '{config.source_id}' at version "
            f"'{config.crosswalk_version}'. Add "
            f"reference/crosswalk/{config.source_id}_*.csv and re-run "
            "`python -m safety.migrate`. Without it every incident is "
            "classified 'other'/'unknown' and the map is quietly wrong"
        )

    if not config.state_fips:
        problems.append(
            f"no state_fips for '{config.source_id}', so neither the coverage "
            "boundary nor the population denominator can be loaded"
        )

    return problems


def cmd_enable(args: argparse.Namespace) -> int:
    """Enable or disable a source, so onboarding never needs database access.

    Exists because the alternative was telling an operator to run SQL, and a
    hosted database often has no query console at all -- the advice was
    unfollowable exactly where it was needed.
    """
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        target = not args.off

        if target and not args.force:
            problems = _readiness(conn, config)
            if problems:
                print(
                    f"'{config.source_id}' ({config.city_name}) is not ready:",
                    file=sys.stderr,
                )
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                print(
                    "\nFix these, or pass --force to enable anyway.",
                    file=sys.stderr,
                )
                return 1

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reference.source_registry
                   SET enabled = %s, updated_at = now()
                 WHERE source_id = %s
                """,
                (target, config.source_id),
            )
        conn.commit()

    verb = "enabled" if target else "disabled"
    print(f"{config.source_id} ({config.city_name}) {verb}.")
    if target:
        print(
            "The scheduled job will pick it up on its next run -- a source that "
            "has never been pulled is always due -- or load it now with:\n"
            "  python -m safety.ops\n"
            "\n"
            "which works out what this city is missing (backfill, then the "
            "population denominator, then the gold rollups) and runs it in "
            "dependency order. Then check it with:\n"
            f"  python -m safety.etl.run weights --city {config.source_id}"
        )
    return 0


def cmd_weights(args: argparse.Namespace) -> int:
    """Report which offenses are riding a derived weight rather than a published one.

    The companion to `flag_unmapped_offenses`: that one surfaces gaps in the
    crosswalk, this one surfaces gaps in the severity scale. Both matter more
    with every city added, because the scale is a fixed 1977 survey of 204
    criminal events and NIBRS has more offense codes than that -- so a new city
    tends to arrive with offenses nothing in the table scores.
    """
    with connect() as conn:
        scheme = args.scheme or gold.active_scheme(conn, args.city)
        if scheme is None:
            raise SystemExit(
                f"no severity scheme active for '{args.city}'; run "
                "python -m safety.migrate first"
            )
        coverage = gold.weight_coverage(conn, args.city, scheme)
        rows = gold.unweighted_offenses(conn, args.city, scheme, args.limit)

    print(f"{args.city} · scheme {scheme}")
    print(f"  published-weight coverage: {coverage * 100:.1f}% of incidents")
    if not rows:
        print("  every offense carries a published weight.")
        return 0

    print(
        f"\n  {len(rows)} offense(s) on a derived fallback, busiest first "
        "(add a weight row keyed on nibrs_code to fix):"
    )
    print(f"  {'n':>8}  {'track':<12} {'nibrs':<6} {'bucket':<16} offense")
    for row in rows:
        print(
            f"  {row['incidents']:>8}  {row['track']:<12} "
            f"{row['nibrs_code'] or '-':<6} {row['severity_bucket']:<16} "
            f"{row['raw_offense_text'] or row['raw_offense_code'] or '?'}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_id, city_name, enabled, expected_cadence,
                   last_status, last_success_at, last_success_watermark, crosswalk_version
            FROM reference.source_registry ORDER BY enabled DESC, source_id
            """
        )
        sources = cur.fetchall()

        cur.execute(
            """
            SELECT pull_id, source_id, mode, status, records_fetched, records_rejected,
                   records_upserted, duration_seconds, started_at
            FROM etl.pull_run ORDER BY pull_id DESC LIMIT 10
            """
        )
        pulls = cur.fetchall()

        cur.execute(
            """
            SELECT source_id, check_name, severity, sum(occurrences) AS total
            FROM etl.validation_issue GROUP BY 1, 2, 3 ORDER BY total DESC
            """
        )
        issues = cur.fetchall()

        cur.execute("SELECT * FROM gold.city_snapshot")
        snapshots = cur.fetchall()

    print(
        json.dumps(
            {
                "sources": sources,
                "recent_pulls": pulls,
                "validation_issues": issues,
                "city_snapshots": snapshots,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _require_enabled(config: SourceConfig) -> None:
    if not config.enabled:
        raise SystemExit(
            f"source '{config.source_id}' is disabled in reference.source_registry. "
            "A city is enabled once its adapter, crosswalk and boundary have all "
            "landed and its first backfill has been read -- see docs/PHASE2.md. "
            f"Enable it with:\n"
            f"  python -m safety.etl.run enable --city {config.source_id}\n"
            "which checks the adapter and crosswalk are actually in place first. "
            "Deliberately not a SQL statement: a hosted database often has no "
            "query console, so that advice would be unfollowable where it is "
            "needed."
        )


def _add_all_flag(parser: argparse.ArgumentParser, due: bool = False) -> None:
    """`--all` fans the command out over every enabled source, stalest first."""
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "run for every enabled source instead of one city, stalest first. "
            "One city's failure does not stop the rest; the exit code is "
            "non-zero if any failed"
        ),
    )
    if due:
        parser.add_argument(
            "--due-only",
            action="store_true",
            help=(
                "with --all, skip sources whose registry cadence says they cannot "
                "have new data yet. Lets one frequent schedule cover six sources "
                "on five cadences: the schedule decides how often to ask, the "
                "registry decides who gets pulled. Exits 0 when nobody is due"
            ),
        )


def _add_skip_hourly_flag(parser: argparse.ArgumentParser) -> None:
    """`--skip-hourly` leaves the expensive, slowest-moving layers alone."""
    parser.add_argument(
        "--skip-hourly",
        action="store_true",
        help=(
            "do not rebuild the time-of-day layers. They are the most expensive "
            "part of a gold refresh and the slowest-moving -- both their windows "
            "are 12 months or wider -- so a frequent incremental is better off "
            "skipping them and letting `hourly` run on its own slower schedule"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="safety.etl.run", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser("backfill", help="full trailing-window load")
    backfill.add_argument("--city", default="phl")
    backfill.add_argument("--months", type=int, default=None)
    _add_all_flag(backfill)
    _add_skip_hourly_flag(backfill)
    backfill.set_defaults(func=_fannable(cmd_backfill))

    incremental = sub.add_parser("incremental", help="pull since the last watermark")
    incremental.add_argument("--city", default="phl")
    _add_all_flag(incremental, due=True)
    _add_skip_hourly_flag(incremental)
    incremental.set_defaults(func=_fannable(cmd_incremental))

    reprocess = sub.add_parser("reprocess", help="replay a stored bronze snapshot")
    reprocess.add_argument("--city", default="phl")
    reprocess.add_argument(
        "--pull-id",
        type=int,
        default=None,
        help="defaults to the newest pull with a stored snapshot",
    )
    reprocess.set_defaults(func=cmd_reprocess)

    census_cmd = sub.add_parser(
        "census", help="load the population denominator (census blocks + LODES jobs)"
    )
    census_cmd.add_argument("--city", default="phl")
    census_cmd.add_argument(
        "--lodes-year",
        type=int,
        default=census.LODES_YEAR,
        help=f"LODES WAC data year (default {census.LODES_YEAR})",
    )
    census_cmd.add_argument(
        "--replay",
        action="store_true",
        help="re-read the stored bronze snapshots instead of re-downloading",
    )
    _add_all_flag(census_cmd)
    census_cmd.set_defaults(func=_fannable(cmd_census))

    release_cmd = sub.add_parser(
        "release-geometry",
        help="drop census block polygons after the exposure layer is built (disk only)",
    )
    release_cmd.add_argument("--city", default="phl")
    _add_all_flag(release_cmd)
    release_cmd.set_defaults(func=_fannable(cmd_release_geometry))

    gold_cmd = sub.add_parser("gold", help="refresh gold rollups only")
    gold_cmd.add_argument("--city", default="phl")
    _add_all_flag(gold_cmd)
    _add_skip_hourly_flag(gold_cmd)
    gold_cmd.set_defaults(func=_fannable(cmd_gold))

    safety_cmd = sub.add_parser("safety", help="rebuild the safety ranking only")
    safety_cmd.add_argument("--city", default="phl")
    safety_cmd.add_argument(
        "--scheme", default=None, help="one severity scheme; default is every enabled one"
    )
    _add_all_flag(safety_cmd)
    safety_cmd.set_defaults(func=_fannable(cmd_safety))

    hourly_cmd = sub.add_parser(
        "hourly", help="rebuild the time-of-day layers only (needs a current `safety`)"
    )
    hourly_cmd.add_argument("--city", default="phl")
    hourly_cmd.add_argument(
        "--scheme", default=None, help="one severity scheme; default is every enabled one"
    )
    _add_all_flag(hourly_cmd)
    hourly_cmd.set_defaults(func=_fannable(cmd_hourly))

    compare_cmd = sub.add_parser(
        "safety-compare", help="diff two severity schemes on the same data"
    )
    compare_cmd.add_argument("--city", default="phl")
    compare_cmd.add_argument("--a", required=True, help="baseline scheme version")
    compare_cmd.add_argument("--b", required=True, help="candidate scheme version")
    compare_cmd.add_argument("--res", type=int, default=8, choices=(8, 9))
    compare_cmd.add_argument("--window", default="last_12m", choices=gold.TIME_WINDOWS)
    compare_cmd.add_argument("--track", default="violent", choices=gold.TRACKS)
    compare_cmd.set_defaults(func=cmd_safety_compare)

    enable_cmd = sub.add_parser(
        "enable", help="enable a city (or --off to disable), checking it is ready first"
    )
    enable_cmd.add_argument("--city", required=True)
    enable_cmd.add_argument(
        "--off", action="store_true", help="disable instead of enabling"
    )
    enable_cmd.add_argument(
        "--force",
        action="store_true",
        help="enable despite a missing crosswalk or adapter; the map will be wrong",
    )
    enable_cmd.set_defaults(func=cmd_enable)

    weights_cmd = sub.add_parser(
        "weights", help="which offenses ride a derived severity weight, not a published one"
    )
    weights_cmd.add_argument("--city", default="phl")
    weights_cmd.add_argument(
        "--scheme", default=None, help="default is the city's active scheme"
    )
    weights_cmd.add_argument("--limit", type=int, default=40)
    _add_all_flag(weights_cmd)
    weights_cmd.set_defaults(func=_fannable(cmd_weights))

    status = sub.add_parser("status", help="registry, recent pulls, data quality")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    wait_for_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
