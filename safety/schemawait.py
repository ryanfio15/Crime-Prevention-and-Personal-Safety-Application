"""Block until the database has every migration this release ships.

    python -m safety.schemawait                  # wait up to 30 minutes
    python -m safety.schemawait --timeout 60

The Kubernetes API pods run this as an init container (deploy/k8s/base/
api.yaml). On the home server install.sh migrates before it starts the new
release, so the release never meets an older schema. In a cluster the new
pods and the migrate Job are created together, and nothing else would stop a
pod with migration 024's code from serving against a database still on 023 --
or, on a brand-new cluster, against no schema at all. A pod held here simply
starts later; the old ReplicaSet keeps serving until it is ready.

It reads only public.schema_migration; it never applies anything (that is the
migrate Job's job, and "only the deploy runs migrations" still holds).
Exit 0 once every db/migrations/*.sql file is recorded, 1 on timeout.
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg

from safety.config import MIGRATIONS_DIR
from safety.db import connect, wait_for_db


def missing_migrations() -> list[str]:
    wanted = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    try:
        with connect() as conn:
            rows = conn.execute("SELECT filename FROM public.schema_migration").fetchall()
    except psycopg.errors.UndefinedTable:
        return wanted
    applied = {r["filename"] for r in rows}
    return [name for name in wanted if name not in applied]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m safety.schemawait")
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds (default 1800)")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    wait_for_db(attempts=max(1, int(args.timeout // 2)))
    deadline = time.monotonic() + args.timeout
    announced = None
    while True:
        missing = missing_migrations()
        if not missing:
            print("schemawait: schema is current", flush=True)
            return 0
        if missing != announced:
            print(f"schemawait: waiting for {len(missing)} migration(s): {', '.join(missing)}",
                  flush=True)
            announced = missing
        if time.monotonic() >= deadline:
            print("schemawait: timed out; is the migrate Job running?", file=sys.stderr)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
