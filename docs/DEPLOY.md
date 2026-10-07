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
  `POSTGRES_PASSWORD` is required: there is no default in code or compose.

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

**Applied migrations are immutable.** `safety.migrate` records a SHA-256 of each
file it applies (`public.schema_migration.checksum`, CRLF-normalised) and refuses
to run — so the deploy is rejected at step 4, before anything switches — if a
file it already applied has since been edited. Fix a shipped migration with a
new one. If an edit was deliberate and reviewed (a comment, say), re-adopt the
file as it is now with
`UPDATE public.schema_migration SET checksum = NULL WHERE filename = '<file>'`;
the next run records the new checksum. Rows written by an older release (no
checksum) are adopted the same way. Migrate also takes a database-wide advisory
lock for its whole run, data loaders included, so a manual
`python -m safety.migrate` waits (up to `MIGRATE_LOCK_WAIT_SECONDS`, default
600) for a deploy's migrate instead of racing it.

---

## Day to day

**Your working folders.** One clone, two folders, each pinned to the branch
its instance runs (`deploy/setup-worktrees.sh` creates them and installs the
guard hooks; re-run it after changing a hook):

```
~/Crime-Prevention-and-Personal-Safety-Application        testing -> dev    edit, commit, push here
~/Crime-Prevention-and-Personal-Safety-Application-main   main    -> prod   read-only view of prod
```

Commit and push in the testing folder; dev deploys itself. When dev looks
right, `deploy/promote.sh` fast-forwards main to origin/testing (after checking
CI passed on it) and prod deploys itself; merging a testing -> main PR on
GitHub does the same. The hooks refuse commits and merges on main, and any
push to main that is not a fast-forward to exactly origin/testing.


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
`I/.env` (from `.env.example`, with `POSTGRES_PASSWORD` set: it has no default)
and `I/data/` in place:

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

**nginx sites.** `deploy/nginx/safety.conf` (prod) and `safety-dev.conf` (dev)
are byte-for-byte the installed `/etc/nginx/sites-available/{safety,safety-dev}`,
certbot's TLS and redirect lines included (certificate *paths* only; the keys
stay in `/etc/letsencrypt`). Change a site in the repo, then
`sudo deploy/install-deployer.sh --nginx` installs it, runs `nginx -t` and
reloads, putting the old file back if the test fails. If certbot (or a hand
edit) has changed a live site since it was last installed from the repo, the
install refuses and prints the diff: copy the live file into the repo and commit
it first, so a stale repo copy can never revert certbot's edits.

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

**Data after a deploy.** Every successful deploy starts `safety-ops@<instance>`
without waiting for it. It checks each *enabled* city for a missing backfill,
census blocks, a stale or missing gold snapshot, or never-built time-of-day
layers, and runs only what is missing -- so a deploy that changes the pipeline
version rebuilds gold, and one that follows enabling a city loads it. With
nothing missing it exits in seconds. Disabled cities are never loaded; enabling
one stays a manual act (below). Deploys wait while it runs (as they do for any
ETL run), so the first deploy after enabling a city can be held back until its
backfill finishes. Watch it with `journalctl -u safety-ops@prod -f` or
`deploy/logs.sh prod`. `safety-ops@`, `safety-etl@` and `safety-etl-hourly@`
share a per-instance lock (`I/data/.etl.lock`), so they queue instead of
overlapping.

**Fresh data.** Handled by the `safety-etl@` timer every six hours and
`safety-etl-hourly@` weekly. Most six-hourly runs find no city due and exit 0 —
`--due-only` reads each source's cadence from `reference.source_registry`, so
the timer only decides how often to ask.

**Run history.** `deploy/logs.sh <instance>` prints every ETL pull and ops
step from `etl.pull_run` and `etl.ops_run`, oldest first with any error under
its row, then the journal output of the scheduled `safety-etl@` and
`safety-etl-hourly@` runs. The history is the complete record (ops runs and
anything started by hand never reach the journal); the journal has the full
process output. It asks for your sudo password for the history step.

```bash
deploy/logs.sh prod                  # everything
deploy/logs.sh dev --since 7d        # also 12h, 30m, 2026-10-01
deploy/logs.sh prod --city chi --failed
deploy/logs.sh prod -f               # then follow the journal live
```

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
with six cities and loses the record of past pulls, so both databases are
dumped nightly.

- *What runs.* `safety-backup.timer` (02:15 UTC, up to 15 min random delay)
  starts `safety-backup.service`, which runs `deploy/lib/backup.sh dump` as root
  from `/usr/local/lib/safety-deploy/`: `pg_dump -Fc` of `safety` (prod) and
  `safety_dev` through `docker exec safety_db` as the bootstrap superuser over
  the container's local socket, `pg_restore -l` on each archive, then a `.meta`
  sidecar recording the dump's `schema_migration` count.
  `safety-backup-verify.timer` (the 1st of each month, 03:15 UTC) restores the
  newest prod dump into a scratch database `safety_restore_check`, compares the
  migration count with the sidecar, checks `silver.incident` and
  `gold.city_snapshot` are non-empty, and drops it. Both are installed by
  `sudo deploy/install-deployer.sh --units` and enabled with
  `sudo systemctl enable --now safety-backup.timer safety-backup-verify.timer`.
- *Where.* `/var/backups/safety/<db>-<UTC timestamp>.dump` and `.dump.meta`,
  root 0700/0600. Same disk as the database: this covers a bad migration, a
  mistaken `DELETE` or corruption, not losing the disk. There is no off-host
  copy yet.
- *Retention and the disk guard.* 7 days of prod dumps, 2 of dev, pruned only
  after a successful dump of that database, so failures never delete the last
  good copy. A dump refuses to start unless twice the previous dump of that
  database plus 15 GB (`SAFETY_BACKUP_MIN_FREE_GB`) is free; the restore check
  wants six times the archive plus 15 GB free on `/`, because it lands inside
  the cluster volume. Both failures are a non-zero exit and an error in
  `journalctl -u safety-backup` / `-u safety-backup-verify`.
- *Restoring.* Restore as the superuser into a fresh database, then point the
  instance at it or rename it into place with the API stopped:
  ```bash
  sudo docker exec safety_db createdb -U safety safety_restored
  sudo docker exec -i safety_db pg_restore -U safety -d safety_restored --exit-on-error \
      < /var/backups/safety/safety-<timestamp>.dump
  ```
  Once the instances use their own roles ("Database roles"), make sure the
  instance's role (`safety_prod`/`safety_dev`) exists before restoring, so the
  dump's `OWNER TO` statements land on it; a dump taken before that change is
  owned by `safety` throughout, so run `deploy/db/transfer-ownership.sql` on the
  restored database afterwards. `pg_restore --no-owner` instead leaves every
  object owned by whoever ran the restore.
- *Dump window.* `pg_dump` holds ACCESS SHARE on every table for its whole run.
  A deploy whose migration needs ACCESS EXCLUSIVE during that window fails at
  `lock_timeout=30s` (`deploy/lib/install.sh`) before switching: the live site
  is untouched and a later autodeploy tick retries it. ETL `DELETE`/`INSERT`
  is unaffected. `Nice=`/`IOSchedulingClass=` on the units only lower the
  priority of the `docker` CLI; the dump runs in the postgres backend at normal
  priority.

**Database roles.** Both instances share one Postgres cluster (`safety_db`).
Originally both connected as `safety`, the image's bootstrap superuser, so a dev
bug or a dev migration could touch prod's database. Each instance now has its
own role that owns its database and every application object in it:

| Instance | Database | Role |
|---|---|---|
| prod | `safety` | `safety_prod` |
| dev | `safety_dev` | `safety_dev` |

Both are `NOSUPERUSER NOCREATEDB NOCREATEROLE`; the API, ETL, ops and migrate
all use the instance's role (a separate read-only API role is possible later).
`CONNECT` and `TEMPORARY` on each database are revoked from `PUBLIC` and granted
to its role only.

- *Switching an instance.* `sudo deploy/db-isolate.sh dev|prod` from a clean
  checkout. It takes the deploy lock and the instance's ETL lock for its whole
  run, saves the old `.env` to
  `/var/lib/safety-deploy/env-backup/<instance>.env.pre-isolation` (root 0600 --
  not beside the instance, because that copy holds the superuser password and
  both instances run as OS user `safety`), creates the role with a random
  password, hands every object over with `deploy/db/transfer-ownership.sql` (one
  `ALTER ... OWNER` per object under `lock_timeout=2s`, retried on lock
  timeouts), rewrites `POSTGRES_USER`/`POSTGRES_PASSWORD` in the `.env`, restarts
  the API and smokes it, and rolls itself back if the smoke fails. Re-running it
  is safe. CI runs the same transfer script against the same PostGIS image on
  every push, then migrates, tests and serves as a non-superuser.
- *No `REASSIGN OWNED`.* On the bootstrap superuser it errors, and it would
  also move objects in the other instance's database. The transfer script
  moves only this database's schemas, relations (each partition on its own),
  routines and types, and leaves extensions, their schemas (`topology`, `tiger`,
  `tiger_data`) and their members with `safety`.
- *Break-glass.* `safety` is still the superuser, reachable over the container's
  local socket: `sudo docker exec -it safety_db psql -U safety -d <db>`. **Any
  DDL done that way must start with `SET ROLE safety_prod;` (or `safety_dev`)**,
  otherwise the new objects are owned by `safety` and the app cannot use them.
  A migration that needs `CREATE EXTENSION` or `ALTER EXTENSION ... UPDATE` has
  to be applied by a human as `safety` (CI fails it first). Never set
  `statement_timeout` or other settings on these roles with `ALTER ROLE ... SET`:
  the ETL and migrate share the role and legitimately run for minutes.
- *Backups and restores.* Unaffected: they use the container socket as `safety`
  (see "Backups" for restoring after the switch).
- *Rollback.* `sudo deploy/db-isolate.sh dev|prod --rollback` puts the saved
  `.env` back, restarts and smokes the API, and re-grants `CONNECT, TEMPORARY`
  to `PUBLIC`. Ownership stays with the role -- the superuser can use every
  object whatever its owner -- which is why the old credentials work at once.
  To reverse the ownership as well:
  `sudo docker exec -i safety_db psql -U safety -d <db> -X -q -At -v from_role=<role> -v to_role=safety < deploy/db/transfer-ownership.sql`
  then `ALTER DATABASE <db> OWNER TO safety`. Once the superuser's password has
  been rotated (`/var/lib/safety-deploy/env-backup/superuser-rotated` exists) the
  saved `.env` holds a dead password and `--rollback` refuses: copy it back by
  hand with `POSTGRES_PASSWORD` set to the new password from
  `/root/safety-db-superuser.pw`.
- *Compose recreates the database container.* `docker-compose.yml` interpolates
  `POSTGRES_USER`/`POSTGRES_PASSWORD` from the `.env` in the directory compose
  runs from -- `prod/current`, whose `.env` also pins `COMPOSE_PROJECT_NAME`.
  After prod is switched, those values differ from the ones the container was
  created with, so the next `docker compose up` from `prod/current` *recreates*
  `safety_db`: a restart for both instances. The data volume and the roles are
  untouched (the image ignores `POSTGRES_*` on an initialised volume), but do it
  at a quiet time. Never run `docker compose` from `dev`.
- *Residual risk.* Both instances still run as the same OS user `safety`, so
  code running on dev can read prod's `.env` and with it prod's credentials. The
  roles stop accidental cross-environment access, not deliberately malicious dev
  code; a separate OS user for dev is planned (finding N1).

---

## If something goes wrong

**Alerts.** Every ETL, ops, deploy, API and backup unit carries
`OnFailure=safety-notify@%N.service`. When one fails, `safety-notify@` logs one
err-priority line tagged `safety-notify` naming the unit, the host and the
time, and appends the same line to `/var/lib/safety-notify/alerts.log`, which
outlives journal rotation. Alerts go to the journal only:

```bash
journalctl -t safety-notify -p err           # every alert, newest last
sudo cat /var/lib/safety-notify/alerts.log
```

Check it whenever you look at the host. `safety-api@` restarts itself on
failure, so it alerts only once systemd gives up restarting it. Adding an
external channel later (a webhook, mail) is one command after `logger` in
`deploy/lib/notify.sh`, followed by `sudo deploy/install-deployer.sh`; keep it
best-effort, since the hook must never fail.

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

_Continuous deployment last verified end to end (push, CI gate, candidate, switch, rollback drills): 2026-10-07 (dev and prod)._
