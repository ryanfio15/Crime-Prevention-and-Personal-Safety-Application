"""Cities split across several databases, and running a command against each.

    python -m safety.shards list
    python -m safety.shards run -- python -m safety.migrate
    python -m safety.shards run -- python -m safety.etl.run incremental --all --due-only

Normally one database holds every city and this module does nothing: `run`
runs its command once, unchanged, and the API opens one pool. It exists for
hardware where no single disk holds all six cities -- FABRIC VMs without the
VM.NoLimit tag stop at 10 GB, and a year of all six is ~6 GB of live data
before WAL and the OS (docs/KUBERNETES.md, "City-sharded databases").

settings.db_shards (DB_SHARDS) then names each database's host and its cities:

    safety-db1-rw=lax,dc;safety-db2-rw=aus,phl;safety-db3-rw=chi,sea

Every database is a complete, ordinary instance -- the full schema, the
reference tables, the registry -- that happens to have only its own cities
enabled. So the ETL needs no changes at all: run against one database it
loads whatever that database has enabled, which is what `run` arranges, one
database at a time. The API routes each request to its city's database
(safety/api/main.py, pool_for).

`run` sets, for each database in turn:
    POSTGRES_HOST      that database
    SHARD_CITIES       its cities (safety.bootstrap disables every other one)
    BOOTSTRAP_CITIES   the same, so the bootstrap enables exactly those
and runs the command. It goes on to the next database when one fails, and
exits with the first non-zero status, so a retried Job redoes all of them --
every command it wraps is idempotent.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass

from safety.config import settings

_CITY = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Shard:
    host: str
    cities: tuple[str, ...]


def parse(spec: str) -> list[Shard]:
    """`host=city,city;host=city` -> shards, in order. Empty -> []."""
    shards: list[Shard] = []
    seen: dict[str, str] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        host, sep, cities_text = part.partition("=")
        host = host.strip()
        if not sep or not host:
            raise ValueError(f"DB_SHARDS entry {part!r} is not host=city,city")
        if any(s.host == host for s in shards):
            raise ValueError(f"DB_SHARDS names host {host!r} twice")
        cities = tuple(c.strip().lower() for c in cities_text.split(",") if c.strip())
        if not cities:
            raise ValueError(f"DB_SHARDS host {host!r} has no cities")
        for city in cities:
            if not _CITY.match(city):
                raise ValueError(f"DB_SHARDS city {city!r} is not a source id")
            if city in seen:
                raise ValueError(f"DB_SHARDS puts {city!r} on both {seen[city]} and {host}")
            seen[city] = host
        shards.append(Shard(host, cities))
    return shards


def configured() -> list[Shard]:
    return parse(settings.db_shards)


def host_of(city: str, shards: list[Shard]) -> str | None:
    for shard in shards:
        if city in shard.cities:
            return shard.host
    return None


def _call(command: list[str], env: dict[str, str] | None) -> int:
    """Run `command`, passing SIGTERM/SIGINT on so a Job's shutdown reaches it."""
    child = subprocess.Popen(command, env=env)

    def forward(signum, _frame):
        child.send_signal(signum)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run(command: list[str], shards: list[Shard] | None = None) -> int:
    """`command` once per database (once, unchanged, with none); first failure's code."""
    shards = configured() if shards is None else shards
    if not shards:
        return _call(command, None)
    first_failure = 0
    for index, shard in enumerate(shards, start=1):
        cities = ",".join(shard.cities)
        print(
            f"==> shard {index}/{len(shards)}: {shard.host} ({cities}): {' '.join(command)}",
            flush=True,
        )
        env = {
            **os.environ,
            "POSTGRES_HOST": shard.host,
            "SHARD_CITIES": cities,
            "BOOTSTRAP_CITIES": cities,
        }
        rc = _call(command, env)
        if rc != 0:
            print(f"==> shard {shard.host} exited {rc}", file=sys.stderr, flush=True)
            first_failure = first_failure or rc
    return first_failure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m safety.shards",
        description="Show the city-to-database split, or run a command on every database.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="each database and its cities")
    run_cmd = sub.add_parser("run", help="run a command once per database")
    run_cmd.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        shards = configured()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.action == "list":
        if not shards:
            print(f"one database ({settings.postgres_host}) holds every city")
        for shard in shards:
            print(f"{shard.host}\t{','.join(shard.cities)}")
        return 0
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("usage: python -m safety.shards run -- command [args...]", file=sys.stderr)
        return 2
    return run(command, shards)


if __name__ == "__main__":
    sys.exit(main())
