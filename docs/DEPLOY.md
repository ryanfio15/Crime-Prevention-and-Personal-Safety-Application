# Deploying to the home server

The application runs on one Linux server as two independent instances behind
nginx, each deployed automatically from its own branch:

| Instance | Branch    | Directory (`I`)                                                     | API (loopback) | Public address                    |
|----------|-----------|---------------------------------------------------------------------|----------------|-----------------------------------|
| `prod`   | `main`    | `/srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod` | `:8000`        | https://ryanfioserver.ddns.net    |
| `dev`    | `testing` | `/srv/safety/Crime-Prevention-and-Personal-Safety-Application/dev`  | `:8001`        | https://ryanfioserverdev.ddns.net |

A push to `main` or `testing` is deployed to that instance within a few minutes
once CI passes on it, and only if the new build proves it works first.

For running the project on your own machine, see the quick start in
[`PHASE1.md`](PHASE1.md).

---

## What runs where

- **Database.** One PostGIS container, `safety_db`, from `docker-compose.yml`
  in the prod directory, published on `127.0.0.1:55432`. The instances differ by
  database: `safety` for prod, `safety_dev` for dev. Never run `docker compose`
  from the dev directory — prod's `.env` pins `COMPOSE_PROJECT_NAME` so its
  compose commands keep finding the existing volume, and dev's does not.
- **API.** `safety-api@prod` and `safety-api@dev` (`deploy/systemd/`): uvicorn
  as the `safety` user, bound to loopback; nginx (`deploy/nginx/`) is the only
  way in and terminates TLS.
- **Data jobs.** `safety-etl@<instance>.timer` runs an incremental pull every six
  hours (dev three hours after prod), `safety-etl-hourly@<instance>.timer`
  rebuilds the time-of-day layers weekly.
- **Deployer.** `safety-autodeploy@<instance>.timer` checks GitHub every minute.
- **Configuration.** Each instance's `I/.env` names its database, port and data
  path; `safety/config.py` reads it. It is never replaced by a deploy.

### Release layout

```
I/.env                       the instance's settings (safety, 0600)
I/data/                      bronze snapshots written by the ETL (safety)
I/releases/<full sha>/       one commit's tree, read-only to safety
    .venv -> ../../venvs/<hash>
    .env  -> ../../.env
    data  -> ../../data
    DEPLOYED_COMMIT          the full sha, reported by /api/v1/health
I/venvs/<hash>/              one venv per requirements.txt + interpreter
I/current -> releases/<sha>  the only path the systemd units name
```

Releases and venvs are owned by root and readable by the `safety` group, so the
running application cannot modify its own code. Bytecode is compiled at deploy
time and the units set `PYTHONDONTWRITEBYTECODE=1`. A venv is keyed by
`sha256(requirements.txt + python3.12 -VV)`, so a commit that does not touch
requirements reuses the previous one and a deploy takes seconds. The three
newest releases are kept (plus whatever `current` and the previous release are),
and unreferenced venvs are removed, at the end of each successful deploy.

`/api/v1/health` reports the running commit:

```bash
curl -s http://127.0.0.1:8001/api/v1/health | jq .commit
```

---

## How a deploy works

1. **CI.** GitHub Actions runs `.github/workflows/ci.yml` on every push to
   `main` or `testing`: install, byte-compile, import the entry points,
   shellcheck `deploy/`, run `python -m safety.migrate` twice against a fresh
   PostGIS, start the app and run `deploy/lib/smoke.sh` against it. The job is
   called `ci`, and that name is the contract — the deployer looks for a check
   run with exactly that name.
2. **Decide.** `safety-autodeploy@<instance>.timer` runs
   `/usr/local/sbin/safety-autodeploy <instance>` (from `deploy/autodeploy.sh`)
   every minute as root. It asks GitHub for the branch head and, if that is not
   what is deployed, for the `ci` check run on it (unauthenticated, at most once
   per two minutes per instance). Pending CI waits; failed CI, or no CI within
   30 minutes, marks the commit skipped.
3. **Install.** When `ci` has succeeded it fetches the commit into a root-owned
   bare cache (`/var/lib/safety-deploy/repo.git`), extracts it and runs
   `/usr/local/lib/safety-deploy/install.sh` (from `deploy/lib/install.sh`):
   1. build `I/releases/<sha>` and compile its bytecode;
   2. build `I/venvs/<hash>` if this requirements set is new;
   3. import the entry points from the release;
   4. run `safety.migrate` from the release (`lock_timeout` 30 s);
   5. start the release as a **candidate** on a spare loopback port (prod
      18000, dev 18001) and run `smoke.sh` against it: `/api/v1/health` 200 and
      reporting this commit, `GET /` 200, `GET /api/v1/cities` 200;
   6. **switch** `I/current` to the release in one rename, restart
      `safety-api@<instance>`;
   7. **verify** with the same smoke checks on the live port;
   8. if that fails, **roll back**: `I/current` back to the previous release,
      restart, verify it, and fail the deploy.
4. **Record.** Only after step 7 passes is the commit written to
   `/var/lib/safety-deploy/<instance>.deployed`.

Root never executes a file from an instance tree or from a pushed commit. It
moves files, compiles bytecode (which does not run the code) and calls
`systemctl`; pip, the import check, migrate and the candidate all run as
`safety`. Deploys are serialised by `/var/lib/safety-deploy/deploy.lock`, shared
with `deploy/deploy.sh` and `deploy/migrate-layout.sh`, and a deploy is put off
while `safety-etl@<instance>` or `safety-etl-hourly@<instance>` is running.

### What a deploy costs, and what a failed one leaves

- **A good deploy:** one uvicorn restart — a few seconds of 502s from nginx —
  and the serving-layer cache starts empty, so the first requests per city are
  slow again.
- **Rejected at steps 1–5:** the live site is untouched. The commit is marked
  skipped, and the journal says which step failed (the candidate's last log
  lines are included).
- **Failed at step 7:** about a minute of degraded service while the live check
  times out, then the previous release is back.

### Migrations must be backward compatible

Migrations run at step 4, before the candidate check can reject the build, and a
rollback does not undo them. So every migration must leave the schema usable by
the **previous** release: expand first (add a column, table or view the new code
uses), and contract (drop what the old code needed) only in a later commit, once
nothing deployed still reads it. This is reviewed by hand. CI helps on pushes:
an advisory step serves the push's previous commit against the schema the new
commit just migrated and warns if it breaks. It is advisory because that
previous commit may itself be a broken one that was never deployed.

---

## Day to day

Watch it:

```bash
journalctl -u safety-autodeploy@prod -f
journalctl -u safety-autodeploy@prod -p err     # only skips, rejections, rollbacks
```

Each tick logs one decision: `up to date`, `pending` (CI not started or
running, or the API rate limit), `deploying`, `deployed`, `deferred` (ETL
running) or `skipped`, followed during a deploy by install.sh's numbered steps.

**Pause and resume** deploys of one instance:

```bash
sudo systemctl stop safety-autodeploy@prod.timer
sudo systemctl start safety-autodeploy@prod.timer
```

`stop` lasts until the next boot; `disable --now` makes it stick.

**Skipped commits.** A head whose CI failed, whose CI never appeared within 30
minutes, or whose install was rejected or rolled back is written to
`/var/lib/safety-deploy/<instance>.skipped` and not tried again. Pushing a new
commit supersedes it. To retry the same commit instead, re-run CI on GitHub if
that was the problem, then:

```bash
sudo rm /var/lib/safety-deploy/prod.skipped
```

**Rolling back on purpose** is `git revert` and push: the revert goes through
CI and the same checks as any other commit. A migration that was applied stays
applied, which the expand/contract rule makes safe.

**Manual deploys** go through the same `install.sh`, from your own checkout:

```bash
deploy/deploy.sh prod    # origin/main
deploy/deploy.sh dev     # origin/testing
```

It fetches with your GitHub key, so only pushed code can be deployed, and waits
for the deploy lock if the timer is mid-deploy.

**Testing the failure paths.** `install.sh` honours a root-only flag file:

```bash
echo candidate | sudo tee /var/lib/safety-deploy/dev.inject_fail   # reject at step 5
echo live      | sudo tee /var/lib/safety-deploy/dev.inject_fail   # fail step 7, roll back
sudo rm /var/lib/safety-deploy/dev.inject_fail                     # normal deploys again
```

The next deploy of that instance fails at the named stage on purpose. The state
directory is root-only (0700), so nothing running as `safety` can set it.

**Updating the deployer.** Deploys never update the deployer itself — that would
let a push choose what root runs. After changing `deploy/autodeploy.sh`,
`deploy/lib/*.sh` or `deploy/systemd/safety-autodeploy@.*`, merge it and re-run
from an up-to-date checkout:

```bash
sudo deploy/install-deployer.sh
```

Changes to the api/etl unit templates need `--units` as well, then a restart of
the affected services. That flag copies each template it replaces to
`/var/lib/safety-deploy/units.prev/` first. The installer never enables, starts
or restarts anything.

**Running a data command by hand** (status, enabling a city, a one-off rebuild):
as `safety`, from the live release:

```bash
sudo -u safety env -C /srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod/current \
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m safety.etl.run status
```

---

## Setting up an instance

On a fresh server, with nginx, the database container, the `safety` user, and
`I/.env` and `I/data/` in place:

1. `sudo deploy/install-deployer.sh --units` — the deployer, the state
   directory and every unit.
2. Write the full sha of the commit to start from into `I/DEPLOYED_COMMIT`, then
   `sudo deploy/migrate-layout.sh <instance> prepare` builds its release, venv
   and `I/current`, and seeds `<instance>.deployed`.
3. `sudo systemctl enable --now safety-api@<instance> safety-etl@<instance>.timer
   safety-etl-hourly@<instance>.timer`, then
   `sudo deploy/migrate-layout.sh <instance> cleanup` to remove `DEPLOYED_COMMIT`.
4. `deploy/deploy.sh <instance>` (this also runs the migrations), then
   `sudo systemctl enable --now safety-autodeploy@<instance>.timer`.

If `<instance>.deployed` is missing, the first tick seeds it from the running
release's `DEPLOYED_COMMIT`, and refuses to deploy if it cannot.

### Moving an instance to the release layout

A one-time step for an instance that still runs from a flat checkout. The unit
templates are shared by both instances, so the order matters:

1. `sudo deploy/install-deployer.sh` — scripts, state directory and the
   autodeploy units only, not the api/etl templates yet.
2. `sudo deploy/migrate-layout.sh dev prepare` and
   `sudo deploy/migrate-layout.sh prod prepare` — build `I/releases/<sha>` for
   the commit each instance is running now, a fresh venv and `I/current`, and
   seed `<instance>.deployed`. Nothing is stopped or deleted; the flat trees keep
   serving.
3. `sudo deploy/install-deployer.sh --units` — the api/etl templates that name
   `I/current`. The old ones are kept in `/var/lib/safety-deploy/units.prev/`.
4. Restart and verify each, dev first:
   `sudo systemctl restart safety-api@dev` then
   `/usr/local/lib/safety-deploy/smoke.sh 8001 '?<sha>'` (the `?` accepts a
   release from before health reported its commit); the same for prod on 8000.
   If either fails: copy `units.prev/*` back to `/etc/systemd/system/`,
   `daemon-reload`, restart both, and stop there.
5. `sudo deploy/migrate-layout.sh <instance> cleanup` for each. It checks that
   every unit names `I/current`, that the live process runs inside the release
   and passes the smoke checks, and refuses while an ETL run is active; then it
   removes only the flat checkout's own files (the names in that commit's top
   level, `DEPLOYED_COMMIT` and `.venv`).

Both phases are safe to re-run.

---

## Recommended GitHub settings and access

- Branch protection on `main` and `testing`: require the `ci` status check, and
  block force pushes and deletion. Anyone who can push to `main` can change what
  runs in production within a few minutes.
- Two-factor authentication on every account with push access.
- `deploy/deploy.sh` needs passwordless sudo for exactly the installer, for
  example in `/etc/sudoers.d/safety-deploy`:

  ```
  ryan ALL=(root) NOPASSWD: /usr/local/lib/safety-deploy/install.sh
  ```

---

## Operating the data

**Fresh data.** Handled by the `safety-etl@` timer every six hours and
`safety-etl-hourly@` weekly. Most six-hourly runs find no city due and exit 0 —
`--due-only` reads each source's cadence from `reference.source_registry`, so
the timer only decides how often to ask.

**Turning on another city** is deliberately a manual act: it is the moment that
city's numbers start being shown to people. A city is ready when it has an
adapter, a reviewed crosswalk, a coverage boundary and a first backfill you have
actually read — see [`PHASE2.md`](PHASE2.md). Then, run as `safety` from
`I/current` as above:

```bash
.venv/bin/python -m safety.etl.run enable --city chi    # checks it is ready, refuses if not
.venv/bin/python -m safety.ops                          # backfill, census, gold, in order
.venv/bin/python -m safety.etl.run status               # read the checks before trusting it
```

`safety.ops` works out what each enabled city is missing and runs only that;
against an up-to-date database it does nothing. Without it, the next ETL tick
backfills the new city but does not load its population denominator, so its
safety ranking stays area-based until `ops` runs. Values are never comparable
between cities: every percentile is against its own city's distribution.
Turning a city off is `enable --city chi --off`; its data stays visible until
its `gold.city_snapshot` row is removed.

**Annual maintenance.** The jobs half of the population denominator comes from
LODES, which publishes yearly: bump `LODES_YEAR` in `safety/etl/census.py`, then
run `census` per city. The population half is decennial.

**Backups.** The database volume is the only state that cannot be redeployed.
The data can be rebuilt from the cities' public sources, but that takes hours
with six cities and loses the record of past pulls.

---

## If something goes wrong

**A deploy was skipped.** `journalctl -u safety-autodeploy@<instance> -p err`
names the commit and the reason; the lines before it show which install step
failed. Fix forward with a new push, or clear the skip as above.

**`safety-api@` keeps restarting.** `journalctl -u safety-api@<instance>`. A
connection-pool timeout after 30 seconds almost always means the database is
down or a `POSTGRES_*` value in `I/.env` is wrong. If `I/current` is dangling,
point it back at a release in `I/releases/` with `ln -sfn` and restart.

**"relation does not exist" errors.** Migrate did not run against this
database. It runs on every deploy (step 4); run `deploy/deploy.sh <instance>`.

**The map is empty and `/api/v1/health` shows `"incidents": 0`.** The data job
has not completed. `sudo systemctl start safety-etl@<instance>` and read its
journal.

**A `429 Too Many Requests` response.** Expected, not a fault: the API limits how
fast one visitor can request the expensive map layer (`safety/api/ratelimit.py`
and the nginx zones). Normal use never reaches it.

**The ETL run exits non-zero but most cities loaded.** Expected: `--all` attempts
every city and exits non-zero if any failed. The JSON summary names which.

**The time-of-day view is empty, or "change from usual" looks stale.** Those
layers come from the weekly `safety-etl-hourly@` run; start it once by hand.

**A newly enabled city shows no safety ranking, only counts.** Either the
population denominator is missing (`census --city <id>` then `gold --city
<id>`) or no scheme is selected for serving (`safety.migrate --activate
nscs_v2_percapita`).

**`ops` reports a step `skipped`.** The step failed within the retry cooldown;
the message quotes the original error. Set `OPS_FORCE=1` for one run to retry.

---

## Notes for developers

**Cadence lives in the database, not in the timers.** `--due-only` reads
`reference.source_registry.expected_cadence` and the timestamp of the last
incident-mode row in `etl.pull_run`. It keys on when a pull last *ran*, not when
one last succeeded — a pull that finds nothing new returns `no_new_data`, the
normal outcome for a bi-weekly source, and keying on success would re-check it
every tick. It only counts `backfill` and `incremental` pulls.

**Only the deploy runs migrations.** `python -m safety.migrate` is step 4 of
install.sh. It is idempotent, but two processes applying the same DDL at once is
worth not arranging, so `safety.ops` checks for the schema and says so rather
than migrating.

**The `Dockerfile`** still builds a working image of the API and ETL for
container hosts and local experiments; the home server does not use it.

_Continuous deployment last verified end to end (push, CI gate, candidate, switch, rollback drills): 2026-10-07 (F6b live)._
