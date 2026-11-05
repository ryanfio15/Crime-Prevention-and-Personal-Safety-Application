"""One ETL at a time across a cluster: a Postgres advisory lock around a command.

    python -m safety.joblock -- python -m safety.ops
    python -m safety.joblock --wait-seconds 600 -- python -m safety.etl.run hourly --all

On the home server every data job runs under
`flock -w 21600 <instance>/data/.etl.lock` (deploy/systemd/safety-etl@.service
and friends), so the six-hourly pull, the weekly time-of-day rebuild and ops
convergence never overlap. Under Kubernetes those are separate CronJob pods,
quite possibly on separate nodes, and a lock file inside one pod means nothing
to another. The database is the one thing they all share, so the lock lives
there instead: a session-level advisory lock held on a dedicated connection for
as long as the wrapped command runs.

Semantics match the flock it replaces:

- wait up to `--wait-seconds` (settings.joblock_wait_seconds, default 21600,
  the same six hours as `flock -w`) for the holder to finish, then give up with
  exit 75 (EX_TEMPFAIL) without running anything;
- run the command, pass its exit code through, release on exit -- including
  when the pod is killed, because Postgres drops a session lock with the
  session.

While the command runs the lock connection is pinged every minute, so an idle
TCP connection is never reaped mid-backfill. If it is lost anyway the lock is
gone; that is logged loudly, and the command is left to finish, because
killing a half-done load is worse than one overlap.

SIGTERM and SIGINT are forwarded to the command, so `kubectl delete pod` and a
CronJob deadline stop the ETL gracefully rather than orphaning it.
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from safety.config import settings
from safety.db import wait_for_db

log = logging.getLogger("safety.joblock")

# Distinct from migrate's 0x5AFE0016 (safety/migrate.py): a migrate and an ETL
# may run at once, as they always could on the host, where migrate takes no
# flock. It is the ETL jobs that must not overlap each other.
ETL_LOCK_KEY = 0x5AFE0E71

EX_TEMPFAIL = 75
_POLL_SECONDS = 5.0
_PING_SECONDS = 60.0


def _connect() -> psycopg.Connection:
    # Keepalives so a lock held through a multi-hour backfill is not silently
    # dropped by an idle-connection reaper between the pod and the database.
    return psycopg.connect(
        settings.dsn,
        autocommit=True,
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=15,
        keepalives_count=4,
    )


@contextmanager
def etl_lock(
    wait_seconds: float | None = None, key: int = ETL_LOCK_KEY
) -> Iterator[psycopg.Connection]:
    """Hold the cluster-wide ETL lock for the body; TimeoutError if it never frees.

    Yields the lock's own connection, so a long-running caller can ping it.
    """
    wait = settings.joblock_wait_seconds if wait_seconds is None else wait_seconds
    conn = _connect()
    try:
        deadline = time.monotonic() + max(0.0, wait)
        announced = False
        while True:
            got = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
            if got:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"ETL lock still held after {wait:.0f}s")
            if not announced:
                log.info("another ETL job holds the lock; waiting up to %.0fs", wait)
                announced = True
            time.sleep(_POLL_SECONDS)
        log.info("ETL lock acquired")
        try:
            yield conn
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except psycopg.Error:
                pass  # the session is gone, and the lock with it
    finally:
        conn.close()


def run_locked(command: list[str], wait_seconds: float | None = None) -> int:
    """Run `command` under the ETL lock and return its exit code (75 if never locked)."""
    try:
        with etl_lock(wait_seconds) as conn:
            child = subprocess.Popen(command)

            def forward(signum, _frame):
                child.send_signal(signum)

            previous = {
                sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)
            }
            lock_lost = False
            try:
                while True:
                    try:
                        return child.wait(timeout=_PING_SECONDS)
                    except subprocess.TimeoutExpired:
                        pass
                    if lock_lost:
                        continue
                    try:
                        conn.execute("SELECT 1")
                    except psycopg.Error as exc:
                        lock_lost = True
                        log.error(
                            "lost the ETL lock connection (%s); another job may now start "
                            "alongside this one",
                            exc,
                        )
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except TimeoutError as exc:
        log.error("%s; not running %s", exc, " ".join(command))
        return EX_TEMPFAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m safety.joblock",
        description="Run a command under the cluster-wide ETL advisory lock.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=None,
        help="give up after this long (default JOBLOCK_WAIT_SECONDS, 21600)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("joblock: no command given (python -m safety.joblock -- <command>)", file=sys.stderr)
        return 2
    wait_for_db()
    return run_locked(command, args.wait_seconds)


if __name__ == "__main__":
    sys.exit(main())
