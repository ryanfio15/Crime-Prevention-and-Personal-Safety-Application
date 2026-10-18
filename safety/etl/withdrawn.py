"""Incidents withdrawn upstream (F13).

An incremental pull re-reads each source's revision window, because agencies
revise and reclassify after publication (S8.3). Promotion is an upsert, so a
record the agency has since *withdrawn* used to stay in silver for ever. This
module compares what silver holds for the window with what the re-pull
returned, and either reports the difference or deletes it.

Order of operations, inside `run._ingest`:

1. `assess` -- read-only, BEFORE promotion, so `prior` is what silver held for
   the window: the "previous comparable count" the outage guard divides by.
2. promotion as usual.
3. `apply` -- in its own transaction: delete (mode `delete`, guard passed) with
   every deleted row archived to etl.withdrawn_incident in the same statement,
   then record one `withdrawn_upstream` validation issue.

The domain is exactly what `fetch_incidents(since, until)` re-reads, expressed on
a silver column the adapter names (`reconcile_basis`), minus one day at each
end for UTC/local skew and partial edge days, and restricted to the dataset id
the pull stages rows under (rows from a previous dataset id are never counted).

The outage guard skips the whole deletion when the pull looks partial:
  (a) overall it re-confirmed < 90% of what silver held for the window;
  (b) a stratum (month, plus the adapter's `reconcile_stratum`) holding >= 50
      rows re-confirmed < 90%;
  (c) a stratum holding >= 3 rows vanished entirely (a missing chunk or layer).

Kill switch / mode: WITHDRAWN_RECONCILE = off | report | delete. The code default
is `report` (user decision 2026-10-07); deletion is enabled per instance in its
.env. Archived rows are pruned after WITHDRAWN_RETENTION_DAYS (90).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from safety.etl import validate

log = logging.getLogger(__name__)

MODES = ("off", "report", "delete")
DEFAULT_MODE = "report"
MAX_ABSENT_SHARE = 0.10  # skip if the pull re-confirmed < 90% of what silver held
STRATUM_MIN_ROWS = 50  # strata smaller than this only trip the whole-stratum rule
WHOLE_STRATUM_MIN_ROWS = 3  # a stratum this big that vanished entirely is an outage
SAMPLE = 20
MAX_STRATA_IN_DETAIL = 24
CHECK_NAME = "withdrawn_upstream"

# Silver expression for each reconcile basis. 'reported' is the UTC date of
# reported_at (DC filters its pull on REPORT_DAT).
_BASIS_SQL = {
    "occurred": "i.occurred_local_date",
    "reported": "(i.reported_at AT TIME ZONE 'UTC')::date",
}


@dataclass(frozen=True, slots=True)
class Stratum:
    name: str
    prior: int
    absent: int


@dataclass(slots=True)
class Assessment:
    source_id: str
    dataset: str
    basis: str
    lo: date
    hi: date
    prior: int
    absent_keys: list[str]
    strata: list[Stratum]
    decision: str  # "none" | "delete" | "skip"
    reason: str | None


def resolve_mode(value: str | None) -> str:
    """off | report | delete. Empty means the default; anything else is report."""
    mode = (value or "").strip().lower()
    if not mode:
        return DEFAULT_MODE
    if mode not in MODES:
        log.warning(
            "WITHDRAWN_RECONCILE=%r is not one of %s; treating it as %r",
            value,
            "|".join(MODES),
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    return mode


def reconcile_window(since: datetime, until: datetime, now: datetime) -> tuple[date, date] | None:
    """The dates a pull over [since, until) re-read completely, or None.

    One day of margin at each end: the upstream filter and the silver date can
    differ by a timezone offset, and the edge days may be partial.
    """
    lo = since.date() + timedelta(days=1)
    hi = min(until, now).date() - timedelta(days=1)
    if hi < lo:
        return None
    return lo, hi


def decide(strata: list[Stratum]) -> tuple[str, str | None]:
    """Apply the outage guard. Pure."""
    prior = sum(s.prior for s in strata)
    absent = sum(s.absent for s in strata)
    if prior == 0 or absent == 0:
        return "none", None
    if absent > MAX_ABSENT_SHARE * prior:
        return "skip", (
            f"the pull re-confirmed {(prior - absent) / prior:.0%} of the {prior} rows silver "
            f"held for the window (below {1 - MAX_ABSENT_SHARE:.0%}); it looks partial"
        )
    for s in strata:
        if s.prior >= STRATUM_MIN_ROWS and s.absent > MAX_ABSENT_SHARE * s.prior:
            return "skip", (
                f"stratum {s.name} re-confirmed {(s.prior - s.absent) / s.prior:.0%} of its "
                f"{s.prior} rows (below {1 - MAX_ABSENT_SHARE:.0%}); it looks partial"
            )
    for s in strata:
        if s.prior >= WHOLE_STRATUM_MIN_ROWS and s.absent == s.prior:
            return "skip", (
                f"stratum {s.name} came back empty ({s.prior} rows in silver); "
                "a missing chunk or layer, not withdrawals"
            )
    return "delete", None


def assess(
    conn: psycopg.Connection,
    adapter: Any,
    source_id: str,
    dataset: str,
    seen_ids: set[str],
    since: datetime,
    until: datetime,
    now: datetime | None = None,
) -> Assessment | None:
    """Read-only. Runs BEFORE promotion, so `prior` is what silver held."""
    basis = getattr(adapter, "reconcile_basis", None)
    if basis is None:
        return None
    win = reconcile_window(since, until, now or datetime.now(timezone.utc))
    if win is None:
        return None
    basis_sql = _BASIS_SQL[basis]
    stratum_sql = f"to_char({basis_sql}, 'YYYY-MM')"
    if getattr(adapter, "reconcile_stratum", None) == "raw_source_category":
        stratum_sql += " || ' ' || coalesce(i.raw_source_category, '-')"
    rows = conn.execute(
        f"SELECT i.incident_key, {stratum_sql} AS stratum FROM silver.incident i "
        f"WHERE i.source_id = %s AND i.source_dataset = %s AND {basis_sql} BETWEEN %s AND %s",
        (source_id, dataset, *win),
    ).fetchall()
    # Exactly the key staging builds (transform.stage_records).
    seen = {f"{source_id}:{x}" for x in seen_ids}
    prior: dict[str, int] = {}
    gone: dict[str, int] = {}
    absent_keys: list[str] = []
    for r in rows:
        prior[r["stratum"]] = prior.get(r["stratum"], 0) + 1
        if r["incident_key"] not in seen:
            gone[r["stratum"]] = gone.get(r["stratum"], 0) + 1
            absent_keys.append(r["incident_key"])
    strata = [Stratum(name, n, gone.get(name, 0)) for name, n in sorted(prior.items())]
    decision, reason = decide(strata)
    return Assessment(
        source_id=source_id,
        dataset=dataset,
        basis=basis,
        lo=win[0],
        hi=win[1],
        prior=len(rows),
        absent_keys=sorted(absent_keys),
        strata=strata,
        decision=decision,
        reason=reason,
    )


_DELETE_SQL = """
WITH gone AS (
    DELETE FROM silver.incident i
    WHERE i.source_id = %(source_id)s AND i.incident_key = ANY(%(keys)s)
      AND i.source_dataset = %(dataset)s
      AND {basis} BETWEEN %(lo)s AND %(hi)s
    RETURNING i.*
)
INSERT INTO etl.withdrawn_incident (pull_id, source_id, incident_key, occurred_local_date, row)
SELECT %(pull_id)s, g.source_id, g.incident_key, g.occurred_local_date, to_jsonb(g) - 'geom'
FROM gone g
"""


def apply(conn: psycopg.Connection, pull_id: int, a: Assessment, mode: str) -> dict[str, Any]:
    """Delete (mode 'delete' and the guard passed), then record the issue. Does not commit."""
    if a.decision == "none":
        return {"action": "none", "absent": 0, "prior": a.prior}
    deleted = 0
    if a.decision == "skip":
        action = "skipped"
    elif mode == "delete":
        with conn.cursor() as cur:
            cur.execute(
                _DELETE_SQL.format(basis=_BASIS_SQL[a.basis]),
                {
                    "source_id": a.source_id,
                    "keys": a.absent_keys,
                    "dataset": a.dataset,
                    "lo": a.lo,
                    "hi": a.hi,
                    "pull_id": pull_id,
                },
            )
            deleted = cur.rowcount
        action = "deleted"
        if deleted != len(a.absent_keys):
            # Rows changed between assess and apply (the promotion just ran).
            log.warning(
                "pull %s: %s rows were absent, %s deleted", pull_id, len(a.absent_keys), deleted
            )
    else:
        action = "report"
    absent = len(a.absent_keys)
    detail = {
        "action": action,
        "mode": mode,
        "basis": a.basis,
        "window": [a.lo.isoformat(), a.hi.isoformat()],
        "prior_in_window": a.prior,
        "absent": absent,
        "absent_share": round(absent / a.prior, 4) if a.prior else 0.0,
        "threshold": MAX_ABSENT_SHARE,
        "reason": a.reason,
        "strata": [
            {"stratum": s.name, "prior": s.prior, "absent": s.absent}
            for s in a.strata
            if s.absent > 0
        ][:MAX_STRATA_IN_DETAIL],
        "sample_keys": a.absent_keys[:SAMPLE],
        "deleted": deleted,
        "archived_to": "etl.withdrawn_incident" if deleted else None,
    }
    validate.record_issues(
        conn,
        pull_id,
        a.source_id,
        [validate.ValidationIssue(CHECK_NAME, "warn", absent, detail)],
    )
    log.info(
        "pull %s: %s of %s rows in %s..%s absent upstream; %s",
        pull_id,
        absent,
        a.prior,
        a.lo,
        a.hi,
        action if a.reason is None else f"{action} ({a.reason})",
    )
    return {"action": action, "absent": absent, "deleted": deleted, "prior": a.prior}


def prune(conn: psycopg.Connection, source_id: str, days: int) -> int:
    """Delete archive rows older than `days` for one source. Does not commit."""
    # Served by withdrawn_incident_source_idx (source_id, withdrawn_at DESC).
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM etl.withdrawn_incident WHERE source_id = %s "
            "AND withdrawn_at < now() - make_interval(days => %s)",
            (source_id, max(int(days), 1)),
        )
        return cur.rowcount


# Exact recovery of one pull's deletions (docs/DEPLOY.md quotes this; the DB
# tests run it). geom is rebuilt from latitude/longitude, as promotion builds it.
# Run ensure_partitions for the archived rows' years first, then the gold
# rollups for the city (python -m safety.etl.run gold --city <city>).
RESTORE_SQL = """
INSERT INTO silver.incident
SELECT (jsonb_populate_record(NULL::silver.incident, w.row || jsonb_build_object('geom',
        ST_AsEWKT(ST_SetSRID(ST_MakePoint((w.row->>'longitude')::float8,
                                          (w.row->>'latitude')::float8), 4326))))).*
FROM etl.withdrawn_incident w
WHERE w.pull_id = %(pull_id)s
ON CONFLICT (source_id, occurred_year, incident_key) DO NOTHING
"""
