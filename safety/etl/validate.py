"""Validation before promotion to silver (design doc S8.5).

Checks the specific failure modes these sources are known to have, and records
what it found in etl.validation_issue instead of failing silently. Severity
semantics:

    reject  the record is kept out of silver, but the rejection is counted
    warn    the record IS promoted, and flagged for review
    block   the pull is aborted before anything reaches silver

Note the deliberate asymmetry on offense codes: S8.5 says a code missing from
the current crosswalk version must be *flagged for manual crosswalk review
rather than silently dropped*, so unmapped codes are a `warn` raised in
transform.py after the crosswalk join, never a `reject` here.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from safety.etl.adapters.base import NormalizedIncident, SourceConfig

log = logging.getLogger(__name__)

# Sample this many offending ids per check into the issue detail, so a pull
# with 4,000 bad rows produces one reviewable row rather than 4,000.
_SAMPLE_LIMIT = 10

# Generous padding around the city bounding box: coordinates outside it are
# geocoding failures, not edge-of-jurisdiction incidents.
_BBOX_PAD_DEGREES = 0.25


@dataclass(slots=True)
class ValidationIssue:
    check_name: str
    severity: str
    occurrences: int
    detail: dict[str, Any]
    source_incident_id: str | None = None


@dataclass(slots=True)
class ValidationResult:
    accepted: list[NormalizedIncident] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    rejected: int = 0
    blocked: bool = False
    block_reason: str | None = None


def validate_records(
    records: list[NormalizedIncident],
    *,
    config: SourceConfig,
    bbox: tuple[float, float, float, float] | None,
    chunk_counts: dict[str, int] | None = None,
    historical_median: float | None = None,
) -> ValidationResult:
    """Run every S8.5 check over one pull's normalized records."""
    result = ValidationResult()

    rejected_by_check: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    details: dict[str, dict[str, Any]] = defaultdict(dict)

    now = datetime.now(timezone.utc)
    future_limit = now + timedelta(days=1)
    epoch_floor = datetime(1900, 1, 1, tzinfo=timezone.utc)

    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    reprojected: Counter[str] = Counter()

    for record in records:
        reject_reason: str | None = None

        # -- missing / unusable timestamp -----------------------------------
        if record.occurred_at <= epoch_floor:
            reject_reason = "missing_timestamp"
        # -- future timestamp ------------------------------------------------
        elif record.occurred_at > future_limit:
            reject_reason = "future_timestamp"
            details["future_timestamp"]["limit"] = future_limit.isoformat()
        # -- missing / zeroed coordinates ------------------------------------
        # LA and DC explicitly publish (0,0) for unresolved geocodes; Philadelphia
        # publishes empty point_x/point_y for a small share of records.
        elif (
            record.latitude is None
            or record.longitude is None
            or (record.latitude == 0 and record.longitude == 0)
        ):
            reject_reason = "missing_coordinates"
        # -- coordinates outside the city ------------------------------------
        elif bbox is not None and not _within(record.latitude, record.longitude, bbox):
            reject_reason = "coordinates_out_of_bounds"
            details["coordinates_out_of_bounds"]["bbox"] = list(bbox)

        if reject_reason is not None:
            rejected_by_check[reject_reason] += 1
            if len(samples[reject_reason]) < _SAMPLE_LIMIT:
                samples[reject_reason].append(record.source_incident_id)
            continue

        # -- duplicate incident id within this pull ---------------------------
        if record.source_incident_id in seen_ids:
            duplicate_ids.append(record.source_incident_id)
            continue
        seen_ids.add(record.source_incident_id)

        if record.coordinate_source != "published_wgs84":
            reprojected[record.coordinate_source] += 1

        result.accepted.append(record)

    result.rejected = sum(rejected_by_check.values())

    for check_name, count in rejected_by_check.items():
        result.issues.append(
            ValidationIssue(
                check_name=check_name,
                severity="reject",
                occurrences=count,
                detail={
                    "rejected": count,
                    "of_fetched": len(records),
                    "sample_incident_ids": samples[check_name],
                    **details.get(check_name, {}),
                },
            )
        )

    if reprojected:
        # Not an error -- the records are loaded at correct locations -- but a
        # silent coordinate conversion is exactly the kind of thing that should
        # be visible when someone later audits where a hexagon's count came from.
        result.issues.append(
            ValidationIssue(
                check_name="coordinates_reprojected",
                severity="warn",
                occurrences=sum(reprojected.values()),
                detail={
                    "reason": "source published coordinates in a projected CRS, not WGS84",
                    "by_source_crs": dict(reprojected),
                    "of_fetched": len(records),
                },
            )
        )

    if duplicate_ids:
        # First occurrence wins; the duplicate is dropped from this pull but the
        # incident itself is still loaded, so this is a warn, not a reject.
        result.issues.append(
            ValidationIssue(
                check_name="duplicate_incident_id",
                severity="warn",
                occurrences=len(duplicate_ids),
                detail={
                    "duplicates_dropped": len(duplicate_ids),
                    "distinct_ids": len(set(duplicate_ids)),
                    "sample_incident_ids": sorted(set(duplicate_ids))[:_SAMPLE_LIMIT],
                },
            )
        )

    result.issues.extend(
        _volume_checks(
            fetched=len(records),
            accepted=len(result.accepted),
            chunk_counts=chunk_counts,
            historical_median=historical_median,
            config=config,
        )
    )

    for issue in result.issues:
        if issue.severity == "block":
            result.blocked = True
            result.block_reason = f"{issue.check_name}: {issue.detail}"

    return result


def _within(lat: float, lng: float, bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    return (
        south - _BBOX_PAD_DEGREES <= lat <= north + _BBOX_PAD_DEGREES
        and west - _BBOX_PAD_DEGREES <= lng <= east + _BBOX_PAD_DEGREES
    )


def _volume_checks(
    *,
    fetched: int,
    accepted: int,
    chunk_counts: dict[str, int] | None,
    historical_median: float | None,
    config: SourceConfig,
) -> list[ValidationIssue]:
    """Record counts wildly outside the norm signal an upstream format change."""
    issues: list[ValidationIssue] = []

    # A pull that returns rows but promotes none is a transformation failure,
    # not a quiet no-op: stop before it reaches silver.
    if fetched > 0 and accepted == 0:
        issues.append(
            ValidationIssue(
                check_name="volume_anomaly",
                severity="block",
                occurrences=1,
                detail={
                    "reason": "every fetched record was rejected",
                    "fetched": fetched,
                },
            )
        )
        return issues

    # Within-pull comparison: a month far below the pull's own median usually
    # means a truncated export rather than a quiet month.
    if chunk_counts and len(chunk_counts) >= 4:
        counts = sorted(chunk_counts.values())
        median = counts[len(counts) // 2]
        if median > 0:
            suspicious = {
                name: count
                for name, count in chunk_counts.items()
                if count < median * 0.25
                # The newest partial period is legitimately short.
                and name != max(chunk_counts)
            }
            if suspicious:
                issues.append(
                    ValidationIssue(
                        check_name="volume_anomaly",
                        severity="warn",
                        occurrences=len(suspicious),
                        detail={
                            "reason": "chunk record count far below the pull median",
                            "pull_median": median,
                            "chunks": suspicious,
                        },
                    )
                )

    # Cross-pull comparison against this source's own history.
    if historical_median and historical_median > 0:
        ratio = fetched / historical_median
        if ratio < 0.25 or ratio > 4.0:
            issues.append(
                ValidationIssue(
                    check_name="volume_anomaly",
                    severity="warn",
                    occurrences=1,
                    detail={
                        "reason": "pull size far outside this source's historical norm",
                        "fetched": fetched,
                        "historical_median": historical_median,
                        "ratio": round(ratio, 3),
                        "expected_cadence": config.expected_cadence,
                    },
                )
            )

    return issues


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def historical_pull_median(
    conn: psycopg.Connection, source_id: str, mode: str, limit: int = 10
) -> float | None:
    """Median records_fetched over this source's recent successful pulls."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY records_fetched) AS median
            FROM (
                SELECT records_fetched
                FROM etl.pull_run
                WHERE source_id = %s AND mode = %s AND status = 'succeeded'
                ORDER BY started_at DESC
                LIMIT %s
            ) recent
            """,
            (source_id, mode, limit),
        )
        row = cur.fetchone()
    return float(row["median"]) if row and row["median"] is not None else None


def record_issues(
    conn: psycopg.Connection, pull_id: int, source_id: str, issues: list[ValidationIssue]
) -> None:
    if not issues:
        return
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO etl.validation_issue
                (pull_id, source_id, check_name, severity, source_incident_id,
                 occurrences, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    pull_id,
                    source_id,
                    issue.check_name,
                    issue.severity,
                    issue.source_incident_id,
                    issue.occurrences,
                    Jsonb(issue.detail),
                )
                for issue in issues
            ],
        )
    for issue in issues:
        log.log(
            logging.ERROR if issue.severity == "block" else logging.WARNING,
            "validation %s [%s] x%s: %s",
            issue.check_name,
            issue.severity,
            issue.occurrences,
            issue.detail,
        )
