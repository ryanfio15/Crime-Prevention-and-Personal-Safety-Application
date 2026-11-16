"""First load of a fresh deployment: schema, cities, data -- one command.

    python -m safety.bootstrap                    # migrate, enable, converge
    python -m safety.bootstrap --cities phl,chi   # a subset
    python -m safety.bootstrap --skip-enable      # leave the registry alone

The Kubernetes `safety-bootstrap` Job runs this once after the first deploy
(deploy/k8s/deploy.sh; docs/KUBERNETES.md). It strings together three commands
that already exist, so the order an operator used to have to remember is
written down once:

1. `python -m safety.migrate` -- the schema, crosswalks and severity tables.
   Advisory-locked, so racing the deploy's own migrate Job is harmless.
2. `python -m safety.etl.run enable --city <id>` for each city in
   BOOTSTRAP_CITIES (default all six). That command's readiness checks still
   apply: a city with no crosswalk is reported and left off, not forced on.
   `safety.ops` deliberately never enables a city (its docstring says why);
   this is the deliberate act, taken by whoever chose to run the bootstrap.
3. `python -m safety.ops` under the cluster-wide ETL lock (safety/joblock.py)
   -- backfill (BACKFILL_MONTHS deep; the Kubernetes ConfigMap sets 12),
   census blocks, gold, the time-of-day layers, per city, in dependency order.

With cities split across databases (settings.db_shards), the Job runs this
once per database through `python -m safety.shards run`, which sets
BOOTSTRAP_CITIES and SHARD_CITIES to that database's cities. Step 2 then also
disables every other city there -- migrate leaves Philadelphia enabled on a
fresh database, and two databases loading the same city would both serve it.

Re-running it is always safe: migrate and enable are idempotent and ops does
only what the data says is missing. The Job sets OPS_RETRY_COOLDOWN_HOURS=0 so
a retried pod is not held off for six hours by the `running` ledger row its
killed predecessor left behind; the ETL lock is what keeps two from overlapping.

Exit status: non-zero if migrate failed, or if convergence left any city
unfinished, so the Job's backoff retries it. A city that failed its readiness
check is reported but does not fail the run -- retrying would not fix it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from safety.config import settings
from safety.db import wait_for_db

log = logging.getLogger("safety.bootstrap")


def parse_cities(value: str) -> list[str]:
    seen: list[str] = []
    for part in value.split(","):
        city = part.strip().lower()
        if city and city not in seen:
            seen.append(city)
    return seen


def enabled_cities() -> list[str]:
    """The cities this database's registry has switched on."""
    from safety.db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT source_id FROM reference.source_registry WHERE enabled ORDER BY source_id"
        ).fetchall()
    return [row["source_id"] for row in rows]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m safety.bootstrap",
        description="Migrate, enable the configured cities, then load their data.",
    )
    parser.add_argument(
        "--cities",
        default=None,
        help="comma-separated source ids (default BOOTSTRAP_CITIES: phl,chi,sea,lax,dc,aus)",
    )
    parser.add_argument("--skip-migrate", action="store_true")
    parser.add_argument("--skip-enable", action="store_true")
    parser.add_argument("--skip-converge", action="store_true")
    parser.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=None,
        help="how long to wait for a running ETL job (default JOBLOCK_WAIT_SECONDS)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    cities = parse_cities(args.cities if args.cities is not None else settings.bootstrap_cities)

    # Imported here so `--help` and the unit tests need none of the ETL stack.
    from safety import migrate, ops
    from safety.etl import run
    from safety.joblock import etl_lock

    wait_for_db()

    if not args.skip_migrate:
        log.info("step 1/3: migrate")
        rc = migrate.main([])
        if rc != 0:
            log.error("migrate failed (exit %s); stopping", rc)
            return rc

    not_ready: list[str] = []
    if not args.skip_enable:
        log.info("step 2/3: enable %s", ", ".join(cities) or "(none)")
        for city in cities:
            rc = run.main(["enable", "--city", city])
            if rc != 0:
                not_ready.append(city)
        if not_ready:
            log.warning("left disabled (not ready): %s", ", ".join(not_ready))
        own = parse_cities(settings.shard_cities)
        if own:
            for city in enabled_cities():
                if city not in own:
                    log.info("disabling %s: this database holds %s", city, ", ".join(own))
                    run.main(["enable", "--city", city, "--off"])

    rc = 0
    if not args.skip_converge:
        log.info(
            "step 3/3: converge (backfill %s days, census, gold, time of day)",
            settings.retention_days,
        )
        try:
            with etl_lock(args.lock_wait_seconds):
                rc = ops.main([])
        except TimeoutError as exc:
            log.error("%s; convergence not run", exc)
            return 75

    if rc == 0:
        log.info("bootstrap complete")
    else:
        log.error("convergence left work unfinished (exit %s); the Job will retry", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
