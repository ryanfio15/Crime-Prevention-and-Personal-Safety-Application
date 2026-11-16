"""The ETL on Railway: decide what is due, run it under the ETL lock.

    python deploy/railway/etl_tick.py --loop     # what the `etl` service runs
    python deploy/railway/etl_tick.py            # one tick, then exit
    python deploy/railway/etl_tick.py --dry-run  # print the plan only

With `--loop` the `etl` service is an ordinary always-on worker: one tick as
soon as it starts, then one every hour at minute 20 (UTC), sleeping in between.
It schedules itself rather than using Railway's cron because a Railway
template -- the one-click "Deploy on Railway" button (docs/RAILWAY.md) --
carries a service's start command, variables and volume but not a cron
schedule, which every deployer would otherwise have to set by hand. Starting
with a tick also means a fresh deployment begins its first data load the
moment it is up, with nothing to trigger. A sleeping Python process costs a
few tens of MB; each step runs as its own subprocess, so the memory a census
load needs is returned as soon as it finishes.

The three Kubernetes CronJobs (deploy/k8s/base/etl.yaml) and the bootstrap Job
become this one service, because a volume attaches to one service only. The
commands are exactly the Kubernetes ones; only the scheduling moved in here:

1. Wait for the API's migrate (safety.schemawait). The API applies migrations
   as it starts (deploy/railway/start-api.sh), so the ETL never meets an old
   schema, as with the Kubernetes init container.
2. First tick only: enable BOOTSTRAP_CITIES (`safety.bootstrap --skip-migrate
   --skip-converge`) and leave a marker on the volume, so a city switched off
   later is not switched back on by the next tick.
3. Under the ETL lock (safety/joblock.py), in this order:
   - `safety.ops`: whatever an enabled city is missing (the first tick's
     one-year backfill, census, gold, the time-of-day layers). Hourly.
   - `safety.etl.run incremental --all --due-only --skip-hourly`. Every tick:
     `--due-only` asks each city's cadence, so the hourly schedule pulls a city
     no more often than the six-hourly CronJob did.
   - `safety.etl.run hourly --all`: the time-of-day layers, once per UTC day at
     04:00 or later, remembered on the volume rather than tied to one tick, so
     a day whose 04:20 tick fell inside a long load still gets it.

A tick that runs past the next :20 (the first load takes one to a few hours)
is followed by the next :20 after it ends, never by a backlog. The lock only
matters for a one-off command started by hand at the same time.

A failed step does not stop the next one: one city portal's bad day must not
cost the others their refresh. A single tick exits 0 when every step
succeeded, otherwise with the first failing step's code; the loop logs that and
carries on, and exits 0 on SIGTERM (a redeploy), stopping the running step
cleanly first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# The tick's own two files live at the top of BRONZE_ROOT (/data/bronze, on the
# service's volume): docker-entrypoint.sh makes exactly that directory writable
# for the unprivileged account, and bronze retention only ever walks its
# `source_id=*` subdirectories (safety/etl/retention.py).
STATE_DIR = Path(os.environ.get("BRONZE_ROOT", "/data/bronze"))
BOOTSTRAP_MARKER = STATE_DIR / ".railway-bootstrapped"
STATE_FILE = STATE_DIR / ".railway-etl-state.json"
HOURLY_NOT_BEFORE_UTC = 4
TICK_MINUTE = 20

_child: subprocess.Popen | None = None
_stopping = threading.Event()


def log(message: str) -> None:
    print(f"etl_tick: {message}", flush=True)


def _on_signal(signum: int, _frame: object) -> None:
    # Railway stops a deployment with SIGTERM to PID 1, which is this process.
    # Pass it on so a pull or a gold build stops cleanly, as joblock does, and
    # run no further step.
    _stopping.set()
    if _child is not None and _child.poll() is None:
        _child.send_signal(signum)


def run(*args: str) -> int:
    global _child
    log("running " + " ".join(args))
    _child = subprocess.Popen([sys.executable, "-m", *args], cwd=REPO_ROOT)
    rc = _child.wait()
    _child = None
    log(f"{args[0]} exited {rc}")
    return rc


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    except OSError as exc:  # no volume: the next tick decides from the clock again
        log(f"could not save state to {STATE_FILE}: {exc}")


def hourly_due(state: dict, now: dt.datetime) -> bool:
    return now.hour >= HOURLY_NOT_BEFORE_UTC and state.get("hourly_date") != now.date().isoformat()


def next_tick_after(now: dt.datetime) -> dt.datetime:
    """The next hh:20:00 strictly after `now`."""
    target = now.replace(minute=TICK_MINUTE, second=0, microsecond=0)
    return target if target > now else target + dt.timedelta(hours=1)


def tick(dry_run: bool = False) -> int:
    now = dt.datetime.now(dt.UTC)
    state = load_state()
    bootstrap = not BOOTSTRAP_MARKER.exists()
    hourly = hourly_due(state, now)
    log(
        f"{now:%Y-%m-%d %H:%M} UTC: bootstrap={'yes' if bootstrap else 'no'}, "
        f"ops=yes, incremental=yes (due cities only), hourly={'yes' if hourly else 'no'}"
    )
    if dry_run:
        return 0

    rc = run("safety.schemawait", "--timeout", "1800")
    if rc != 0:
        log("schema is not current; is the api service running? skipping this tick")
        return rc

    if bootstrap and not _stopping.is_set():
        rc = run("safety.bootstrap", "--skip-migrate", "--skip-converge")
        if rc != 0:
            log("enabling the bootstrap cities failed; the next tick tries again")
            return rc
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            BOOTSTRAP_MARKER.write_text(now.isoformat() + "\n")
        except OSError as exc:
            log(f"could not write {BOOTSTRAP_MARKER}: {exc} (is the volume mounted?)")

    from safety.joblock import etl_lock

    first_failure = 0
    try:
        with etl_lock():
            for step in (
                ("safety.ops",),
                ("safety.etl.run", "incremental", "--all", "--due-only", "--skip-hourly"),
            ):
                if _stopping.is_set():
                    return first_failure
                rc = run(*step)
                first_failure = first_failure or rc
            if hourly and not _stopping.is_set():
                # Once a day whatever the outcome, as the daily CronJob ran: a
                # failure waits for tomorrow rather than repeating hourly, and
                # layers that are missing outright are ops' job anyway.
                rc = run("safety.etl.run", "hourly", "--all")
                state["hourly_date"] = now.date().isoformat()
                state["hourly_exit"] = rc
                save_state(state)
                first_failure = first_failure or rc
    except TimeoutError as exc:
        # Another ETL (a one-off started by hand) held the lock throughout;
        # the next tick runs instead.
        log(f"{exc}; nothing run this tick")
        return 0

    log("tick complete" if first_failure == 0 else f"tick finished with failures (exit {first_failure})")
    return first_failure


def loop() -> int:
    log(f"worker started: a tick now, then hourly at :{TICK_MINUTE:02d} UTC")
    while not _stopping.is_set():
        try:
            tick()
        except Exception as exc:  # never let one bad tick end the worker
            log(f"tick raised {type(exc).__name__}: {exc}")
        if _stopping.is_set():
            break
        now = dt.datetime.now(dt.UTC)
        wake = next_tick_after(now)
        log(f"next tick at {wake:%H:%M} UTC")
        _stopping.wait((wake - now).total_seconds())
    log("stopping")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python deploy/railway/etl_tick.py")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="run as the always-on worker")
    mode.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.loop:
        return loop()
    return tick(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
