"""Pipeline orchestrator (design doc S8.2, S8.4).

Stands in for the workflow orchestrator S8.4 calls for (the Airflow / Prefect /
Dagster class of tool). The DAG shape is the same one that tool would run --
fetch -> validate -> transform -> refresh affected gold rollups -- expressed as
a CLI so Phase 1 has no scheduler dependency:

    python -m safety.etl.run backfill    --city phl [--months 24]
    python -m safety.etl.run incremental --city phl
    python -m safety.etl.run reprocess   --city phl --pull-id 3
    python -m safety.etl.run gold        --city phl
    python -m safety.etl.run status

Schedules themselves are configuration, not code (S8.2): each source's cadence
lives in reference.source_registry, and `incremental` is safe to run more often
than a source actually publishes -- "checked, nothing new" is the normal case
for a bi-weekly source like Los Angeles.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg

from safety import PIPELINE_VERSION
from safety.db import connect, wait_for_db
from safety.etl import gold, transform, validate
from safety.etl.adapters import SourceConfig, get_adapter
from safety.etl.adapters.base import NormalizedIncident, RawChunk, SourceAdapter
from safety.etl.adapters.philadelphia import default_backfill_window
from safety.etl.bronze import LocalBronzeStore, build_manifest
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
    """Fetch and store the city coverage polygon (S3.1) if not already present."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 AS present FROM reference.city_boundary WHERE source_id = %s",
            (config.source_id,),
        )
        present = cur.fetchone() is not None
    if present and not force:
        return

    started = time.monotonic()
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
        chunk = adapter.fetch_boundary()
        if chunk is None:
            _finish_pull(conn, pull_id, status="no_new_data", started=started)
            return

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
# Commands
# ---------------------------------------------------------------------------


def cmd_backfill(args: argparse.Namespace) -> int:
    months = args.months or settings.backfill_months
    with connect() as conn:
        config = SourceConfig.load(conn, args.city)
        _require_enabled(config)
        since, until = default_backfill_window(months)
        outcome = _ingest(conn, config, mode="backfill", since=since, until=until)
        if outcome["status"] == "succeeded":
            stats = gold.refresh_all(conn, config.source_id, PIPELINE_VERSION)
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
            since, until = default_backfill_window(settings.backfill_months)
        else:
            # S8.3: re-read behind the watermark and upsert, because these
            # agencies revise and reclassify after initial publication.
            since = config.last_success_watermark - timedelta(
                days=config.revision_lookback_days
            )

        outcome = _ingest(conn, config, mode="incremental", since=since, until=until)
        if outcome["status"] == "succeeded":
            stats = gold.refresh_all(conn, config.source_id, PIPELINE_VERSION)
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
            cur.execute(
                "SELECT * FROM etl.pull_run WHERE pull_id = %s AND source_id = %s",
                (args.pull_id, args.city),
            )
            original = cur.fetchone()
        if original is None or not original["bronze_uri"]:
            print(f"No stored bronze snapshot for pull {args.pull_id}", file=sys.stderr)
            return 1

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


def cmd_gold(args: argparse.Namespace) -> int:
    with connect() as conn:
        stats = gold.refresh_all(conn, args.city, PIPELINE_VERSION)
    print(json.dumps(stats, indent=2, default=str))
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
            "Phase 1 covers Philadelphia only (design doc S14)."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="safety.etl.run", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser("backfill", help="full trailing-window load")
    backfill.add_argument("--city", default="phl")
    backfill.add_argument("--months", type=int, default=None)
    backfill.set_defaults(func=cmd_backfill)

    incremental = sub.add_parser("incremental", help="pull since the last watermark")
    incremental.add_argument("--city", default="phl")
    incremental.set_defaults(func=cmd_incremental)

    reprocess = sub.add_parser("reprocess", help="replay a stored bronze snapshot")
    reprocess.add_argument("--city", default="phl")
    reprocess.add_argument("--pull-id", type=int, required=True)
    reprocess.set_defaults(func=cmd_reprocess)

    gold_cmd = sub.add_parser("gold", help="refresh gold rollups only")
    gold_cmd.add_argument("--city", default="phl")
    gold_cmd.set_defaults(func=cmd_gold)

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
