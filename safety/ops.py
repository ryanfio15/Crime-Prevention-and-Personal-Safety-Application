"""Bring the deployed data up to what the registry says it should be.

    python -m safety.ops                # converge every enabled city
    python -m safety.ops --dry-run      # print the plan, change nothing
    python -m safety.ops --city chi     # one city
    python -m safety.ops --force        # ignore the retry cooldown

One fixed command line covers every data-maintenance workflow -- which is the
point. The alternative, documented until this module existed, was to run a
different `safety.etl.run` subcommand for each step of each workflow: five
separate runs to onboard a city, two more to finish a first load, in an order
that had to be remembered because getting it wrong fails quietly rather than
loudly.

It is a convergence loop, not a script of steps. Every run asks the database
what is actually missing for each enabled city and does only that:

    no completed incident pull        -> backfill
    no census blocks                  -> census
    snapshot missing/stale/old version-> gold
    no time-of-day rows at all        -> hourly (folded into the step above
                                         when one is already running)

So a run against a healthy deployment is a handful of EXISTS queries and an
exit; a run against a city enabled ten minutes ago does the full onboarding
sequence in one deploy, in dependency order. Nothing has to be sequenced by
hand, and re-running is always safe, so it can be run on every deploy or on a
schedule without harm.

Two things it deliberately does not do.

**It does not run migrations.** `python -m safety.migrate` belongs to the
deploy step alone (deploy/lib/install.sh). It is idempotent, so a second caller
would be harmless rather than dangerous, but two processes racing to apply the
same DDL is worth not arranging. The consequence is a startup ordering rule,
checked explicitly below: on a brand-new database, migrate has to run once
before `ops` has a schema to read.

**It does not enable cities.** Enabling one is the moment that city's numbers
start being shown to people, and the design document treats that as a
deliberate act rather than something infrastructure decides.
`safety.etl.run enable --city <id>` does it, with the readiness checks
that catch an enabled city with no crosswalk -- which loads perfectly happily
and produces a complete, plausible, wrong map. Convergence operates on cities
that are already enabled.

**Staleness of the time-of-day layers is not its job either.** Those are rebuilt
weekly by the `safety-etl-hourly@` timer, because they are the most expensive thing
the pipeline builds and their windows are a year wide. This module builds them
only when they have *never* been built for a city, which is the gap a new city
leaves between its first load and the next Sunday.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field

import psycopg

from safety import PIPELINE_VERSION
from safety.config import settings
from safety.db import connect, wait_for_db
from safety.etl import gold
from safety.etl.run import (
    _COMPLETED_STATUSES,
    _INCIDENT_MODES,
    build_parser,
    enabled_sources,
)

log = logging.getLogger("safety.ops")


# ---------------------------------------------------------------------------
# What a converge run is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One `safety.etl.run` invocation, with the evidence that it is needed.

    `argv` is the real command line rather than a prebuilt Namespace: it is
    handed to `safety.etl.run.build_parser()`, so every default, choice and
    flag stays defined in exactly one place and a flag added there cannot
    silently go missing here. It also means the plan prints the command an
    operator could run by hand, which is the thing they will want when a step
    fails.
    """

    task: str
    city: str
    reason: str
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return "python -m safety.etl.run " + " ".join(self.argv)


# _INCIDENT_MODES and _COMPLETED_STATUSES are imported from safety.etl.run
# rather than restated here, because both answer the same question this module
# needs -- "does a real incident pull exist" -- and the reasoning behind each is
# subtle enough that two copies would drift. `no_new_data` counts as completed:
# a pull ran and the city had published nothing new, which is the normal outcome
# for a bi-weekly source and emphatically not a reason to backfill again.
# `failed` does not, so a city whose first load died halfway is correctly seen as
# still needing one. Census and boundary pulls write etl.pull_run rows too, which
# is why the mode filter is there at all.

# Everything the decision below needs, in one round trip per city.
#
# EXISTS rather than count(*) throughout: the question is only ever "is there
# any", and reference.census_block holds hundreds of thousands of rows for a
# large city while gold.cell_hour_safety is the largest table in the database.
# Counting them to compare against zero would make the no-op case -- by far the
# most common -- the expensive one.
_STATE_SQL = """
SELECT
    (
        SELECT max(p.finished_at)
        FROM etl.pull_run p
        WHERE p.source_id = %(source_id)s
          AND p.mode   = ANY(%(modes)s)
          AND p.status = ANY(%(statuses)s)
    ) AS last_incident_pull_at,
    EXISTS (
        SELECT 1 FROM reference.census_block c WHERE c.source_id = %(source_id)s
    ) AS has_census,
    -- Under the window name the current code builds (gold.HOURLY_WINDOWS), so
    -- rows the previous release wrote under its own name do not count: after the
    -- window rename the post-deploy run rebuilds the layer instead of leaving
    -- the time-of-day view empty until the weekly job.
    EXISTS (
        SELECT 1 FROM gold.cell_hour_safety h
        WHERE h.source_id = %(source_id)s AND h.time_window = ANY(%(hourly_windows)s)
    ) AS has_hourly,
    -- Whether the time-of-day layers are *buildable*, which is a separate
    -- question from whether they exist. gold.refresh_hourly_layer omits every
    -- incident with a NULL occurred_local_hour and skips the layers entirely
    -- when none has one (safety/etl/gold.py, _HOUR_COVERAGE_SQL). A city in
    -- that state can never satisfy `has_hourly`, so without this the plan would
    -- plan an `hourly` step, watch it succeed having written nothing, find the
    -- layers still absent, and plan it again on the next deploy forever.
    -- Partition pruning on source_id keeps this to the city's own partition.
    EXISTS (
        SELECT 1 FROM silver.incident i
        WHERE i.source_id = %(source_id)s AND i.occurred_local_hour IS NOT NULL
    ) AS has_clock_hours,
    g.last_refreshed_at AS gold_refreshed_at,
    g.pipeline_version  AS gold_pipeline_version
FROM (SELECT 1) _
LEFT JOIN gold.city_snapshot g ON g.source_id = %(source_id)s
"""


def _city_state(conn: psycopg.Connection, source_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            _STATE_SQL,
            {
                "source_id": source_id,
                "modes": list(_INCIDENT_MODES),
                "statuses": list(_COMPLETED_STATUSES),
                "hourly_windows": list(gold.HOURLY_WINDOWS),
            },
        )
        return cur.fetchone()


def plan_for_city(conn: psycopg.Connection, source_id: str) -> list[Step]:
    """Convenience wrapper: fetch this city's state and plan from it."""
    return plan_from_state(_city_state(conn, source_id), source_id)


def plan_from_state(state: dict, source_id: str) -> list[Step]:
    """The steps this city needs, in dependency order.

    The order is the documented onboarding order, and each dependency is real
    rather than conventional:

    * `backfill` first because it is what fetches the coverage boundary, and
      `census` trims the county-level block prefilter down to the blocks inside
      that boundary. Without it every citywide population figure describes the
      counties instead -- a wrong denominator, not a missing one.
    * `census` before `gold` because the safety ranking divides by ambient
      population. Run the other way round, the exposure layer is skipped with a
      log line and the per-capita scheme cannot be built, which renders as a map
      with counts but no safety ramp. That looks like a working page, which is
      why this used to catch everybody.
    * `hourly` last because it needs the all-hours ranking to already be
      current; it reads percentiles `gold` writes.
    """
    steps: list[Step] = []

    needs_backfill = state["last_incident_pull_at"] is None
    needs_census = not state["has_census"]
    # Only missing if it could be built. A source that publishes no clock hour
    # -- or whose stored timestamps have not had the hour recovered from the
    # bronze snapshots yet -- has no time-of-day layer to build, and asking for
    # one every deploy is a loop, not a convergence. A city about to be
    # backfilled has no silver rows yet, so `has_clock_hours` is necessarily
    # false at planning time and says nothing about whether the source publishes
    # an hour. Plan the layers anyway: gold.refresh_hourly_layer skips them with
    # a log line when no hour arrives, and the next run's state settles it.
    needs_hourly = not state["has_hourly"] and (
        state["has_clock_hours"] or needs_backfill
    )

    if needs_backfill:
        # `cmd_backfill` refreshes gold itself once the ingest succeeds, so
        # after a backfill the only thing that can still leave gold wrong is
        # census landing afterwards and changing the denominator it ranked
        # against. Hence the branch rather than queueing gold unconditionally:
        # a full gold refresh is minutes of work on a large city, and doing it
        # twice in one run is the kind of waste that shows up on the bill or
        # the load graph.
        needs_gold = (
            "the population denominator is loaded after the backfill in this run"
            if needs_census
            else None
        )
    else:
        needs_gold = _gold_reason(state, census_pending=needs_census)

    # Fold the hourly build into whichever heavier step is already running,
    # rather than adding a standalone pass. `backfill` and `gold` both take
    # --skip-hourly and build the time-of-day layers in the same transaction
    # when it is absent, so the choice is literally whether to pass the flag.
    # Standalone `hourly` recomputes the all-hours ranking's inputs again.
    fold_hourly_into = None
    if needs_hourly:
        if needs_gold:
            fold_hourly_into = "gold"
        elif needs_backfill and not needs_census:
            # Only safe when census is already loaded: otherwise the hourly
            # layers get built against a missing denominator and are rebuilt by
            # the gold step a moment later anyway.
            fold_hourly_into = "backfill"

    if needs_backfill:
        argv = ["backfill", "--city", source_id]
        if fold_hourly_into != "backfill":
            argv.append("--skip-hourly")
        steps.append(
            Step(
                task="backfill",
                city=source_id,
                reason="no completed incident pull on record",
                argv=tuple(argv),
            )
        )

    if needs_census:
        steps.append(
            Step(
                task="census",
                city=source_id,
                reason="no census blocks loaded, so the safety ranking has no population denominator",
                argv=("census", "--city", source_id),
            )
        )

    if needs_gold:
        argv = ["gold", "--city", source_id]
        if fold_hourly_into != "gold":
            argv.append("--skip-hourly")
        steps.append(
            Step(task="gold", city=source_id, reason=needs_gold, argv=tuple(argv))
        )

    if needs_hourly and fold_hourly_into is None:
        steps.append(
            Step(
                task="hourly",
                city=source_id,
                reason="time-of-day layers have never been built for this city",
                argv=("hourly", "--city", source_id),
            )
        )

    return steps


def advisories(state: dict) -> list[str]:
    """Things worth saying that no converge step can fix.

    Kept separate from the plan because the distinction matters to whoever
    reads the output: a step is work this run will do, an advisory is a gap it
    cannot close. Printing nothing in these cases would report a city as "up to
    date" while part of the product is missing for it, which is the quiet kind
    of wrong this codebase keeps trying to avoid.
    """
    notes: list[str] = []

    # Only once something has been pulled: before the first backfill there are
    # no silver rows to carry an hour, and the advisory would be false.
    if (
        state["last_incident_pull_at"] is not None
        and not state["has_hourly"]
        and not state["has_clock_hours"]
    ):
        notes.append(
            "no incident carries a clock hour, so the time-of-day view is empty and "
            "cannot be built. Recover the hour from the stored snapshots with: "
            "python -m safety.etl.run reprocess --city <id>"
        )

    return notes


def _gold_reason(state: dict, census_pending: bool) -> str | None:
    """Why gold needs rebuilding, or None if it is current.

    Returned as prose rather than a bool because it is written to
    etl.ops_run.reason, and "what did this deployment think was wrong" is the
    question worth being able to answer six weeks later.
    """
    if census_pending:
        return "the population denominator is being loaded in this run"

    refreshed_at = state["gold_refreshed_at"]
    if refreshed_at is None:
        return "no gold.city_snapshot row, so nothing is being served for this city"

    built_by = state["gold_pipeline_version"]
    if built_by != PIPELINE_VERSION:
        # Not cosmetic. PIPELINE_VERSION is bumped when the meaning of the
        # output changes -- Phase 2 moved the crosswalk's fallback tier, the
        # all-hours smoothing, and the backfill floor -- so a snapshot stamped
        # with an older one is serving numbers computed a different way.
        return f"gold was built by pipeline {built_by}, now {PIPELINE_VERSION}"

    pulled_at = state["last_incident_pull_at"]
    if pulled_at is not None and refreshed_at < pulled_at:
        return f"incidents were pulled at {pulled_at:%Y-%m-%d %H:%M}Z, after the last gold refresh"

    return None


# ---------------------------------------------------------------------------
# The ledger (etl.ops_run, migration 014)
#
# Note the division of labour: the plan above is derived entirely from the data,
# so convergence is self-correcting -- truncate etl.ops_run and the next run
# still does exactly the right work. The ledger's only job is retry backoff,
# which the data cannot answer, because "this city has no incidents" looks
# identical whether nobody has tried or somebody has tried and failed four times
# in the last hour.
# ---------------------------------------------------------------------------


_LAST_ATTEMPT_SQL = """
SELECT status, started_at, error,
       EXTRACT(EPOCH FROM (now() - started_at)) / 3600.0 AS hours_ago
FROM etl.ops_run
WHERE task = %s AND source_id IS NOT DISTINCT FROM %s
ORDER BY started_at DESC
LIMIT 1
"""


def _cooldown_block(conn: psycopg.Connection, step: Step, cooldown_hours: float) -> str | None:
    """Why this step should be left alone for now, or None to go ahead."""
    with conn.cursor() as cur:
        cur.execute(_LAST_ATTEMPT_SQL, (step.task, step.city))
        last = cur.fetchone()

    if last is None or last["hours_ago"] >= cooldown_hours:
        # A 'running' row older than the cooldown is an abandoned run, not a
        # live one -- a container that was killed mid-step, which on a platform
        # that redeploys on push is a routine way for a long backfill to end.
        # Treating it as in-flight forever would wedge the city permanently.
        return None

    if last["status"] == "running":
        return (
            f"an attempt started {last['hours_ago']:.1f}h ago is still marked running; "
            "assuming it is in flight"
        )

    if last["status"] == "failed":
        error = (last["error"] or "").strip().splitlines()
        first = error[0] if error else "no error recorded"
        return (
            f"failed {last['hours_ago']:.1f}h ago and the {cooldown_hours:g}h cooldown "
            f"has not elapsed ({first}); --force or OPS_FORCE=1 to retry now"
        )

    # Succeeded inside the cooldown, yet the data still says the step is needed.
    # Worth saying out loud rather than retrying: a step that reports success
    # and does not change the condition it was meant to fix will do exactly the
    # same thing again, and a redeploy loop would run it every few minutes.
    return (
        f"succeeded {last['hours_ago']:.1f}h ago but the condition it was meant to fix "
        "is still true; not retrying automatically"
    )


def _ledger_open(step: Step) -> int:
    """Record the attempt and commit, before the work starts.

    Its own short-lived connection, committed immediately, so the row is durable
    even though the step itself opens and rolls back its own transactions. This
    is the same reason it is written before rather than after: a step killed
    halfway has to leave evidence that it was tried.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO etl.ops_run (task, source_id, status, reason, pipeline_version)
            VALUES (%s, %s, 'running', %s, %s)
            RETURNING ops_run_id
            """,
            (step.task, step.city, step.reason, PIPELINE_VERSION),
        )
        ops_run_id = cur.fetchone()["ops_run_id"]
        conn.commit()
    return ops_run_id


def _ledger_close(ops_run_id: int, status: str, seconds: float, error: str | None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE etl.ops_run
               SET status = %s, finished_at = now(), duration_seconds = %s, error = %s
             WHERE ops_run_id = %s
            """,
            (status, seconds, error, ops_run_id),
        )
        conn.commit()


def _schema_ready(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('etl.ops_run') IS NOT NULL AS ready")
        return cur.fetchone()["ready"]


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def run_step(step: Step) -> tuple[bool, str | None]:
    """Execute one step in-process. Returns (ok, error)."""
    log.info("%s/%s: %s", step.city, step.task, step.reason)
    print(f"\n--- {step.command}", flush=True)

    ops_run_id = _ledger_open(step)
    started = time.monotonic()
    try:
        args = build_parser().parse_args(list(step.argv))
        code = args.func(args)
    except (Exception, SystemExit) as exc:
        # SystemExit explicitly, not just Exception. Several commands report a
        # precondition failure by raising SystemExit with a message -- a
        # disabled source in `_require_enabled`, argparse on a rejected flag --
        # and that inherits BaseException, so `except Exception` would let it
        # past. It would then unwind out of the converge loop, abandoning the
        # remaining cities and leaving this step's ledger row stuck on
        # 'running', which the cooldown reads as an attempt still in flight.
        #
        # Otherwise: logged with a traceback and recorded, then handed back so
        # the caller can abandon this city and carry on with the next. By the
        # time a pull failure reaches here it is already durable in
        # etl.pull_run and the registry's last_error; this adds the ops-level
        # view of it.
        log.exception("%s/%s failed", step.city, step.task)
        _ledger_close(ops_run_id, "failed", time.monotonic() - started, repr(exc))
        return False, repr(exc)

    seconds = time.monotonic() - started
    if code != 0:
        _ledger_close(ops_run_id, "failed", seconds, f"exit code {code}")
        return False, f"exit code {code}"

    _ledger_close(ops_run_id, "succeeded", seconds, None)
    return True, None


@dataclass
class CityResult:
    city: str
    done: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    # Steps abandoned because an earlier step for this city failed. Kept apart
    # from `skipped` because the two mean different things to whoever reads the
    # summary: one is "nothing to do", the other is "blocked, look upstream".
    blocked: list[str] = field(default_factory=list)


def converge(
    cities: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    cooldown_hours: float | None = None,
) -> int:
    cooldown = settings.ops_retry_cooldown_hours if cooldown_hours is None else cooldown_hours

    with connect() as conn:
        if not _schema_ready(conn):
            print(
                "etl.ops_run does not exist, so the schema has not been migrated yet.\n"
                "\n"
                "Only the deploy step runs migrations (deploy/lib/install.sh), so on\n"
                "a new database `python -m safety.migrate` has to run once before\n"
                "`ops` has anything to read. Run it (or deploy), then run this again.",
                file=sys.stderr,
            )
            return 1

        # Stalest first, reusing the ETL's own ordering: a converge run cut
        # short -- a timeout, a redeploy, a container restart -- has then spent
        # its time on the cities that needed it most, and a city that has never
        # loaded sorts to the front, which is where it belongs.
        enabled = enabled_sources(conn)

        if cities is None:
            cities = enabled
        else:
            # Checked here rather than left to `_require_enabled` deeper in the
            # pipeline, which reports it by raising SystemExit partway through
            # the first step. A typo'd OPS_CITY is the likeliest cause and
            # deserves to say so before any work starts.
            unknown = [city for city in cities if city not in enabled]
            if unknown:
                print(
                    f"not enabled in reference.source_registry: {', '.join(unknown)}\n"
                    f"enabled: {', '.join(enabled) or '(none)'}\n"
                    "\n"
                    "Enabling a city is deliberately a separate, manual act -- it is the "
                    "moment its numbers start being shown to people:\n"
                    f"  python -m safety.etl.run enable --city {unknown[0]}",
                    file=sys.stderr,
                )
                return 1

        if not cities:
            print(
                json.dumps(
                    {
                        "converged": [],
                        "note": (
                            "no enabled sources in reference.source_registry. Enable one "
                            "with: python -m safety.etl.run enable --city <id>"
                        ),
                    },
                    indent=2,
                )
            )
            return 0

        states = {city: _city_state(conn, city) for city in cities}

    plans = {city: plan_from_state(state, city) for city, state in states.items()}
    notes = {
        city: city_notes
        for city, state in states.items()
        if (city_notes := advisories(state))
    }

    pending = {city: steps for city, steps in plans.items() if steps}

    print(f"\n{len(cities)} enabled cit{'y' if len(cities) == 1 else 'ies'}: {', '.join(cities)}")
    for city in cities:
        if plans[city]:
            for step in plans[city]:
                print(f"  {city:5s} {step.task:9s} <- {step.reason}")
        else:
            print(f"  {city:5s} {'up to date':9s}")
        for note in notes.get(city, []):
            print(f"  {city:5s} {'note':9s} -- {note}")

    if not pending:
        print(
            json.dumps(
                {"converged": [], "up_to_date": cities, "notes": notes}, indent=2
            )
        )
        return 0

    if dry_run:
        print(
            "\n"
            + json.dumps(
                {
                    "dry_run": True,
                    "would_run": {
                        city: [step.command for step in steps]
                        for city, steps in pending.items()
                    },
                },
                indent=2,
            )
        )
        return 0

    results: list[CityResult] = []
    for city, steps in pending.items():
        result = CityResult(city=city)
        results.append(result)

        with connect() as conn:
            blocks = (
                {}
                if force
                else {
                    step.task: reason
                    for step in steps
                    if (reason := _cooldown_block(conn, step, cooldown)) is not None
                }
            )

        for index, step in enumerate(steps):
            if result.failed:
                # One city's steps are a dependency chain, so there is no point
                # running `gold` after `census` failed -- it would succeed and
                # write a snapshot with no exposure layer, which is worse than
                # not running, because it looks done.
                result.blocked.append(step.task)
                continue

            if step.task in blocks:
                result.skipped[step.task] = blocks[step.task]
                log.info("%s/%s skipped: %s", city, step.task, blocks[step.task])
                # A skipped step is a missing dependency for everything after
                # it, same as a failed one.
                result.blocked.extend(later.task for later in steps[index + 1 :])
                break

            ok, error = run_step(step)
            if ok:
                result.done.append(step.task)
            else:
                result.failed[step.task] = error or "unknown error"

    # One city's bad day must not stop the other five: six independent
    # government portals have six independent outages, and a partial converge
    # that loaded four cities is a better outcome than one that stopped at the
    # first failure. The exit code still reports it, so the scheduler marks the
    # run failed and the next run picks up where this one left off.
    failed = {r.city: r.failed for r in results if r.failed}
    print(
        "\n"
        + json.dumps(
            {
                "up_to_date": [city for city in cities if not plans[city]],
                "converged": {r.city: r.done for r in results if r.done},
                "skipped": {r.city: r.skipped for r in results if r.skipped},
                "blocked": {r.city: r.blocked for r in results if r.blocked},
                "failed": failed,
                "notes": notes,
            },
            indent=2,
            default=str,
        )
    )
    return 1 if failed else 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safety.ops",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument(
        "--city",
        default=None,
        help=(
            "converge one city instead of every enabled one. Defaults to OPS_CITY, "
            "or all enabled sources stalest-first"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="print the plan and the commands it would run, then exit. Also OPS_DRY_RUN",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=None,
        help=(
            "run every planned step regardless of what the ledger says about recent "
            "attempts. Also OPS_FORCE"
        ),
    )
    parser.add_argument(
        "--retry-cooldown-hours",
        type=float,
        default=None,
        help=(
            f"how long a failed step is left alone before retrying "
            f"(default {settings.ops_retry_cooldown_hours:g}, or OPS_RETRY_COOLDOWN_HOURS)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    args = build_arg_parser().parse_args(argv)

    # Flags win over environment, and the environment wins over the defaults.
    # A scheduled `python -m safety.ops` sets no arguments at all, so there
    # every one of these arrives as a variable; the flags are for running the
    # same code by hand.
    city = args.city or settings.ops_city or None
    force = settings.ops_force if args.force is None else args.force
    dry_run = settings.ops_dry_run if args.dry_run is None else args.dry_run

    wait_for_db()
    return converge(
        cities=[city] if city else None,
        force=force,
        dry_run=dry_run,
        cooldown_hours=args.retry_cooldown_hours,
    )


if __name__ == "__main__":
    sys.exit(main())
