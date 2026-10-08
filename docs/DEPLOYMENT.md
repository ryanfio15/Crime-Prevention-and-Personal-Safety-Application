# Deployment

The application runs on one Ubuntu server as two instances behind nginx. Each instance deploys
automatically from its own branch:

| Instance | Branch | Directory (`I`) | API (loopback) | Candidate port | Public URL | Database / role | OS user |
|---|---|---|---|---|---|---|---|
| `prod` | `main` | `/srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod` | `127.0.0.1:8000` | `18000` | https://ryanfioserver.ddns.net | `safety` / `safety_prod` | `safety` |
| `dev` | `testing` | `/srv/safety/Crime-Prevention-and-Personal-Safety-Application/dev` | `127.0.0.1:8001` | `18001` | https://ryanfioserverdev.ddns.net | `safety_dev` / `safety_dev` | `safety-dev` |

Throughout this document, `I` means an instance root (above) and `APP=/srv/safety/Crime-Prevention-and-Personal-Safety-Application`.

Contents: [Routine deploys](#routine-deploys) · [Rollback](#rollback) · [Migrations](#migrations-during-a-deploy) ·
[From-zero setup](#from-zero-setup) · [Load data to match prod](#7-load-data-to-match-prod) ·
[Branch protection](#branch-protection-and-github-settings) · [Reference](#reference) (how a deploy works, release
layout, operating the data, database roles, OS users) · [Container image](#container-image-alternative-target)

This document replaces the earlier `docs/DEPLOY.md`, which now only points here.

---

## Routine deploys

### The normal path: push, CI, autodeploy

1. Commit on `testing` in `~/Crime-Prevention-and-Personal-Safety-Application` and push:
   ```bash
   git push origin testing
   ```
2. GitHub Actions runs the job `ci`, which takes about 1 minute.
3. On the server, root's `safety-autodeploy@dev.timer` checks GitHub every minute. Once the `ci` check run has
   `conclusion == success`, it runs `/usr/local/lib/safety-deploy/install.sh dev <stage> <sha>`.
4. Watch it:
   ```bash
   journalctl -u safety-autodeploy@dev -f
   ```
   A successful deploy logs:
   ```
   dev: deploying <sha> (CI passed)
   0/7 running dev as safety-dev
   1/7 release <sha>      2/7 venv <hash>      3/7 import check      4/7 migrate
   5/7 candidate on :18001   → smoke ok on :18001
   6/7 switch <old> -> <sha>
   7/7 verify on :8001       → smoke ok on :8001
   dev is up on :8001 at <sha>
   started safety-ops@dev to load any missing data
   dev: deployed <sha>
   ```
5. Check it:
   ```bash
   curl -s https://ryanfioserverdev.ddns.net/api/v1/health | jq '{status,commit}'
   ```
   Expect `"status": "ok"` and `commit` equal to `git rev-parse origin/testing`.

### Promote dev to prod

```bash
deploy/promote.sh
```

The script:
1. Fetches.
2. Requires the `ci` check on `origin/testing` to be `success`.
3. Shows `git log main..testing`.
4. Fast-forwards `main` to the head of `origin/testing` (`git push origin <sha>:refs/heads/main`).

That head is normally what dev is running, but not always: a skipped or deferred deploy leaves dev on an older commit.
Check first that `curl -s https://ryanfioserverdev.ddns.net/api/v1/health | jq -r .commit` matches
`git rev-parse origin/testing`.

`safety-autodeploy@prod` then deploys it within a few minutes. Follow it with `journalctl -u safety-autodeploy@prod -f`.
Success is `prod: deployed <sha>`, and `https://ryanfioserver.ddns.net/api/v1/health` then shows that commit.

**A GitHub "Merge pull request" also deploys prod**, but with differences:
- It creates a new merge commit that dev never ran.
- Nothing on GitHub requires CI to pass before merging, because `main` is unprotected. Prod still won't deploy a commit
  whose CI fails.
- It leaves `main` ahead of `testing`. The next `deploy/promote.sh` merges `origin/main` into `testing` and pushes;
  run promote again after CI passes on that push.

### What install.sh does (both instances)

| Step | Action | On failure |
|---|---|---|
| pre | Refuses with exit **75** ("deferred") while `safety-etl@`, `safety-etl-hourly@` or `safety-ops@` for that instance is running | Autodeploy retries on the next tick |
| 1/7 | Builds `I/releases/<sha>`, with symlinks `.venv → ../../venvs/<hash>`, `.env → ../../.env`, `data → ../../data`, and writes `DEPLOYED_COMMIT` | Commit **skipped** |
| 2/7 | Reuses or builds `I/venvs/<hash>` (hash = `sha256(requirements.txt + python3.12 -VV)`) | skipped |
| 3/7 | Import check as the instance user | skipped |
| 4/7 | `PGOPTIONS='-c lock_timeout=30s' python -m safety.migrate` as the instance user | skipped; **migrations already applied stay applied** |
| 5/7 | Starts a candidate on 18000/18001 as the transient unit `safety-candidate-<i>` (logs: `journalctl -u safety-candidate-prod`), then runs `deploy/lib/smoke.sh` | skipped; live site untouched |
| 6/7 | Atomically repoints `I/current`, then `systemctl restart safety-api@<i>` | — |
| 7/7 | Smoke-tests the live port | **automatic rollback** to the previous release, then skipped |
| after | Writes `<i>.deployed`, keeps the 3 newest releases (plus current and previous), starts `safety-ops@<i>` | — |

`smoke.sh` requires all of: `/api/v1/health` returns 200 with the expected `commit`, `GET /` returns 200, and
`/api/v1/cities` returns 200. It retries every second for up to 60 s (`SMOKE_TIMEOUT`).

**Not zero-downtime.** Step 6 restarts the only uvicorn process, so nginx returns 502 for a few seconds and the cache
starts empty. A whole deploy took about 10 s on 2026-10-07.

### Redeploy one instance by hand

```bash
deploy/deploy.sh prod      # or: deploy/deploy.sh dev
```

This fetches `origin/<branch>` with your own GitHub SSH key and runs `sudo install.sh`, so it prompts for your sudo
password. No sudoers rule is installed. It **does not check CI**, and it waits up to 900 s for the deploy lock.
Success ends with `prod is up on :8000 at <sha>`, and autodeploy then reports `up to date`.

### Controls

| Task | Command |
|---|---|
| Pause / resume deploys | `sudo systemctl stop safety-autodeploy@prod.timer` / `start`; use `disable --now` to keep it off across reboots |
| Retry a commit autodeploy gave up on | `sudo rm /var/lib/safety-deploy/prod.skipped` |
| See only failures | `journalctl -u safety-autodeploy@prod -p err` |
| Failure drill | `echo candidate \| sudo tee /var/lib/safety-deploy/dev.inject_fail` (or `live`), push, then `sudo rm` the file |
| Update the deployer itself | `sudo deploy/install-deployer.sh [--units] [--nginx]` from an up-to-date checkout. Deploys never update `/usr/local/sbin/safety-autodeploy`, `/usr/local/lib/safety-deploy/*` or the units |

**When a commit is skipped.** A commit is never retried once it is skipped. That happens when CI fails, when no CI run
appears within 30 minutes, or when install.sh fails, including a migration lock timeout during the nightly backup.
Re-running CI alone does not help either, because a skipped head is never looked at again. To recover:
- push a new commit; or
- if CI itself was the problem, re-run CI until it passes, then `sudo rm /var/lib/safety-deploy/<i>.skipped`.

---

## Rollback

| Situation | What to do |
|---|---|
| Live check fails during a deploy | Nothing; it rolls back automatically. The journal shows `<i>: rolled back <new> -> <prev>` |
| Bad code is live, no rush | `git revert <sha> && git push`. It goes through CI and the same checks. Migrations stay applied |
| Emergency, no time for CI | Repoint `current` to a kept release, as below |

Emergency rollback for prod (use `dev` and port 8001 for dev):
```bash
I=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod   # not readable without sudo, so no cd
sudo ls "$I/releases/"                              # pick <good-sha>; only the last 3 plus previous are kept
sudo ln -sfn releases/<good-sha> "$I/current.new" && sudo mv -T "$I/current.new" "$I/current"
sudo systemctl restart safety-api@prod
/usr/local/lib/safety-deploy/smoke.sh 8000 '?<good-sha>'    # expect "smoke ok on :8000"
# Stop autodeploy from re-deploying the bad head, and record what is actually live:
echo <bad-sha>  | sudo tee /var/lib/safety-deploy/prod.skipped
echo <good-sha> | sudo tee /var/lib/safety-deploy/prod.deployed
```
Then push a `git revert`, which deploys normally. If you write `.deployed` without also writing `.skipped`, autodeploy
redeploys the bad head straight away, because its CI passed.

Other rollbacks:
- Unit templates: `sudo sh -c 'cp -p /var/lib/safety-deploy/units.prev/* /etc/systemd/system/' && sudo systemctl daemon-reload`.
- DB role isolation: `sudo deploy/db-isolate.sh <i> --rollback`.
- OS user isolation: `sudo deploy/os-isolate.sh dev --rollback`.
- Data: see [OPERATIONS.md → Restore a database dump](OPERATIONS.md#restore-a-database-dump).

---

## Migrations during a deploy

- Migrations run at step 4/7, **before** the candidate is tested, and rollback never undoes them. Every migration must
  therefore keep the *previous* release working. CI checks this with an advisory step ("Previous commit on the migrated
  schema"), but a failure there only produces a warning.
- They run as the instance's non-superuser owner role. A migration that needs `CREATE EXTENSION` or
  `ALTER EXTENSION … UPDATE` fails in CI (the "P2 self-test"). Apply such a migration by hand as `safety`:
  ```bash
  sudo docker exec -it safety_db psql -U safety -d safety      # dev: -d safety_dev
  ```
- `lock_timeout=30s`: a migration that needs an exclusive lock while the nightly `pg_dump` runs (02:15 UTC) fails. The
  commit is then **skipped**, not retried. The `safety-backup.timer` comment says otherwise, but
  `deploy/autodeploy.sh:186-190` writes `.skipped` for any exit code other than 0 or 75. Fix it by removing `.skipped`
  or pushing again.
- Applied files are immutable. Editing one makes every later migrate refuse to run, with a checksum error.

---

## From-zero setup

> ⚠️ UNVERIFIED as a whole. This sequence comes from reading the scripts and has never been run end to end. Two
> ordering constraints, both confirmed by reading the code:
> - `migrate-layout.sh cleanup` must not run before the first migrate. `/api/v1/health` returns 500 on an empty schema,
>   so cleanup's smoke check fails.
> - The database must be started from `prod/current`, which only exists after `prepare`.

### 0. Prerequisites

| Need | Version on the current host | Required by |
|---|---|---|
| Ubuntu with systemd | 24.04.5 LTS | units, `systemd-run`, `runuser`, `flock` |
| `/usr/bin/python3.12` + `python3.12-venv` | 3.12.3 | hard-coded in `deploy/lib/release.sh:21` |
| Docker CE + compose plugin (Docker's apt repo, not `docker.io`) | 29.8.2 / 5.6.0 | DB container, backups, db-isolate |
| nginx, certbot, python3-certbot-nginx | 1.24.0, 2.9.0 | TLS proxy |
| git, curl, jq, openssl, util-linux | 2.43, 8.5, 1.7, 3.0.13 | deployer, smoke checks, password generation |
| Hardware | 4 CPUs, 7.6 GiB RAM, 116 GB root disk (67 GB free). A minimum spec has not been defined | Backups need 2× the last dump + 15 GB free; the restore check needs 6× + 15 GB |
| Accounts | GitHub push access to `ryanfio15/Crime-Prevention-and-Personal-Safety-Application` (SSH key); a host login with sudo; the No-IP/ddns.net account for both hostnames; router admin; an email for Let's Encrypt | |
| Network | Router forwards TCP 80 and 443 to the host; both DDNS names resolve to the home IP | |

```bash
sudo apt-get install -y python3.12-venv nginx certbot python3-certbot-nginx jq curl openssl git util-linux
# Docker CE from https://docs.docker.com/engine/install/ubuntu/ , then:
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
timedatectl    # the ETL timers assume the host clock is UTC
```

### 1. Users, directories, `.env` files

```bash
APP=/srv/safety/Crime-Prevention-and-Personal-Safety-Application
sudo install -d -o root -g root -m 0751 /srv/safety
sudo useradd --system --user-group --home-dir /srv/safety --no-create-home --shell /usr/sbin/nologin safety
sudo install -d -o safety -g safety -m 0700 /srv/safety/.cache          # pip cache; safety cannot create it itself
sudo install -d -o root   -g root   -m 0755 "$APP"                      # os-isolate.sh requires exactly root:root 0755
sudo install -d -o safety -g safety -m 0700 "$APP/prod" "$APP/dev"
sudo install -d -o safety -g safety -m 0750 "$APP/prod/data" "$APP/dev/data"
openssl rand -hex 32        # superuser password, used in BOTH files below until isolation
```

Write `prod.env` and `dev.env` with plain `KEY=value` lines: no quotes, no `export`, and comments on their own lines.
`db-isolate.sh` matches whole lines, and systemd and python-dotenv parse anything else differently.

```ini
# prod.env
POSTGRES_DB=safety
POSTGRES_USER=safety
POSTGRES_PASSWORD=<64-hex superuser password>
API_PORT=8000
COMPOSE_PROJECT_NAME=crime-prevention-and-personal-safety-application
```
```ini
# dev.env  (POSTGRES_DB is mandatory: the code default "safety" is prod's database)
POSTGRES_DB=safety_dev
POSTGRES_USER=safety
POSTGRES_PASSWORD=<same superuser password>
API_PORT=8001
```

Then install them:
```bash
sudo install -o safety -g safety -m 0600 prod.env "$APP/prod/.env"
sudo install -o safety -g safety -m 0600 dev.env  "$APP/dev/.env"
shred -u prod.env dev.env
```
`API_PORT` is in `.env.example` (set it per instance); `COMPOSE_PROJECT_NAME` is not, but prod requires it. See
[README → Configuration](../readme.md#6-configuration).

### 2. Clone and git guards

```bash
git clone git@github.com:ryanfio15/Crime-Prevention-and-Personal-Safety-Application.git ~/Crime-Prevention-and-Personal-Safety-Application
cd ~/Crime-Prevention-and-Personal-Safety-Application && git checkout testing
deploy/setup-worktrees.sh      # → "installed hooks: pre-commit pre-merge-commit pre-push"
```
The hooks block commits and merges on `main`, and any push to `main` other than a fast-forward to `origin/testing`.
They only run in this clone; nothing on GitHub enforces them. The script also adds a `main` worktree at
`~/Crime-Prevention-and-Personal-Safety-Application-main`, which `deploy/promote.sh` fast-forwards. That worktree is
optional.

### 3. Root-owned deployer and units

```bash
sudo deploy/install-deployer.sh --units     # → "installed; nothing was restarted or enabled"
for d in safety-etl@dev.timer.d safety-etl-hourly@dev.timer.d; do   # dev's timer offsets; no script installs them
  sudo install -d -m 0755 /etc/systemd/system/$d
  sudo install -m 0644 deploy/systemd/$d/offset.conf /etc/systemd/system/$d/offset.conf
done
sudo systemctl daemon-reload
```

### 4. First releases (no database needed)

```bash
git fetch origin
git rev-parse origin/main    | sudo tee $APP/prod/DEPLOYED_COMMIT
git rev-parse origin/testing | sudo tee $APP/dev/DEPLOYED_COMMIT
sudo deploy/migrate-layout.sh prod prepare  # → "prod: prepared; current -> releases/<sha>, deployed = <sha>"
sudo deploy/migrate-layout.sh dev prepare
```
`migrate-layout.sh prepare` is the only tool that creates `I/current` from nothing, despite its name.

### 5. Database

```bash
sudo sh -c "cd $APP/prod/current && docker compose up -d"
sudo docker inspect -f '{{.State.Health.Status}}' safety_db       # repeat until "healthy"
sudo docker exec safety_db createdb -U safety safety_dev            # nothing else creates the dev database
```
- Always run compose from `prod/current`, and never from `dev` or from a clone on this host. Compose takes the
  password from prod's `.env` (through the `.env` symlink).
- `COMPOSE_PROJECT_NAME` decides the volume name, `<project>_safety_db_data`. With the wrong value, compose creates a
  new, empty volume.

### 6. First deploys (these also run the first migrations)

```bash
deploy/deploy.sh prod     # → "prod is up on :8000 at <sha>" … "started safety-ops@prod to load any missing data"
deploy/deploy.sh dev      # → "dev is up on :8001 at <sha>"
sudo rm $APP/prod/DEPLOYED_COMMIT $APP/dev/DEPLOYED_COMMIT
sudo systemctl enable --now safety-api@prod safety-api@dev \
     safety-etl@prod.timer safety-etl@dev.timer safety-etl-hourly@prod.timer safety-etl-hourly@dev.timer \
     safety-backup.timer safety-backup-verify.timer
```
The first migrate must run while both `.env`s still log in as the superuser `safety`, because migration 001 creates
the `postgis` extension. Do not enable the backup timer before `safety_dev` exists: `backup.sh` dumps both databases
and fails otherwise.

### 7. Load data to match prod

The migrations enable only Philadelphia. On a fresh database, `safety-ops@` (started by each deploy) backfills phl,
loads census data and builds gold. For phl that takes about 3 minutes and ~320k records (`docs/PHASE1.md:28`); there
are no figures for the other cities. To match prod, which serves six cities on `nscs_v2_percapita`, run each command
as the instance user:

```bash
run() { sudo -u safety env -C $APP/prod/current PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m "$@"; }
run safety.etl.run enable --city chi     # repeat for sea, lax, dc; prints "chi (Chicago) enabled."
systemctl is-active safety-ops@prod      # wait for "inactive": the post-deploy run is still loading phl
sudo systemctl start --no-block safety-ops@prod   # backfill → census → gold → hourly for every enabled city
journalctl -u safety-ops@prod -f
run safety.etl.run status                # JSON: each source's last_status and snapshot
```
You don't need `safety.migrate --activate` on a fresh database. Every migrate points cities with no scheme at the
only enabled scheme, `nscs_v2_percapita` (`safety/migrate.py:467-495`). Use `--activate` only to move a city off an
older scheme.
For dev, use `$APP/dev/current` and `-u safety`, or `-u safety-dev` once step 9 has run.

> ⚠️ Austin (`aus`): migration 016 says to keep it disabled until the City of Austin confirms its CrimeViewer services
> may be reused. Prod has it enabled today. Confirm the terms before enabling it on a new host. See
> [README → Known gaps](../readme.md#known-gaps-and-unverified-items).

### 8. nginx and TLS (prod first)

The repo's site files already contain certbot's TLS lines, so installing them on a host with no certificates fails
`nginx -t`. Bootstrap each hostname with a temporary HTTP-only site:

```bash
sudo tee /etc/nginx/sites-available/safety >/dev/null <<'NGX'
server { listen 80; listen [::]:80; server_name ryanfioserver.ddns.net;
         location / { proxy_pass http://127.0.0.1:8000; } }
NGX
sudo ln -s /etc/nginx/sites-available/safety /etc/nginx/sites-enabled/safety
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ryanfioserver.ddns.net --redirect     # needs port 80 forwarded and DNS resolving here
sudo install -o root -g root -m 0644 deploy/nginx/safety.conf /etc/nginx/sites-available/safety
sudo nginx -t && sudo systemctl reload nginx
sudo deploy/install-deployer.sh --nginx     # prod pass: "nginx: installed  safety-dev and reloaded" (copied, not enabled)
                                            # dev pass:  "nginx: sites already match the repo"
```
Repeat for dev with `safety-dev`, `ryanfioserverdev.ddns.net`, port `8001` and `deploy/nginx/safety-dev.conf`. Install
prod first: the dev site relies on the `limit_req_status 429` directives in the prod file.

> ⚠️ UNVERIFIED: that `certbot --nginx` on the temporary site creates `/etc/letsencrypt/options-ssl-nginx.conf` and
> `ssl-dhparams.pem`, both of which the repo files include.

### 9. Isolation (the instances must already be serving)

First wait until `systemctl is-active safety-ops@prod safety-ops@dev` reports `inactive` for both. The first backfill
holds the ETL lock. `db-isolate.sh` gives up after waiting 1 h for that lock (`flock -w 3600`), and `os-isolate.sh`
refuses straight away (exit 75) while any ETL or ops unit is active.

```bash
sudo deploy/db-isolate.sh dev && sudo deploy/db-isolate.sh prod
#   → "db-isolate[<i>]: isolated: safety-api@<i> serves as safety_dev / safety_prod"
sudo deploy/os-isolate.sh dev
#   → "os-isolate[dev]: isolated: dev runs as safety-dev"
```
`db-isolate.sh` creates a non-superuser owner role with a random password, transfers ownership, revokes PUBLIC
CONNECT, rewrites the `.env`, restarts the API and smoke-tests it. It rolls itself back on failure. After this, the next
`docker compose up` from `prod/current` recreates the container, because the interpolated values changed. The volume
and roles are unaffected.

### 10. Continuous deployment

```bash
sudo systemctl enable --now safety-autodeploy@prod.timer safety-autodeploy@dev.timer
journalctl -u safety-autodeploy@prod -n 5 --no-pager      # → "prod: up to date at <sha>"
```

### 11. Verify

```bash
for h in ryanfioserver.ddns.net ryanfioserverdev.ddns.net; do
  curl -s https://$h/api/v1/health | jq '{status,commit}'            # "ok", and the branch head
  curl -s -o /dev/null -w '%{http_code}\n' https://$h/api/v1/cities  # 200
done
curl -sI http://ryanfioserver.ddns.net/ | head -1                    # 301
systemctl list-timers 'safety-*' --no-pager
```

---

## Branch protection and GitHub settings

Every push to `main` deploys prod and every push to `testing` deploys dev, so the
branches are protected with a GitHub ruleset (repo **Settings → Rules → Rulesets →
New branch ruleset**, enforcement *Active*):

| Rule | `main` | `testing` |
|---|---|---|
| Restrict deletions | ✅ | ✅ |
| Block force pushes | ✅ | ✅ |
| Require status checks to pass: `ci` (source *GitHub Actions*) | ✅ | — |
| Require a pull request before merging | — | — |

- `main` requires `ci` because GitHub then refuses any commit on `main` that has not
  passed CI. `deploy/promote.sh` pushes a commit that already passed on `testing`, so
  it keeps working, and so does merging a PR on GitHub. Leave "require branches to be
  up to date" off.
- `testing` cannot require `ci`: CI only runs after the push, so every push would be
  refused.
- No pull-request rule: `promote.sh` pushes straight to `main`
  (`deploy/promote.sh:47`), which that rule would block.
- Leave the bypass list empty. An emergency rollback does not need a push (see
  [Rollback](#rollback)).

Also: two-factor authentication on every account with push access, and only
trusted collaborators (**Settings → Collaborators**), since anyone who can push to
`testing` runs code on the server. `deploy/deploy.sh` asks for your sudo password;
to make it passwordless, allow exactly the installer, for example in
`/etc/sudoers.d/safety-deploy`:

```
<your-user> ALL=(root) NOPASSWD: /usr/local/lib/safety-deploy/install.sh
```

---

## Reference

The internals behind the procedures above. Code comments across `deploy/`,
`safety/` and `db/migrations/` cite these section names.

### How a deploy works

1. **CI.** GitHub Actions runs `.github/workflows/ci.yml` on every push to
   `main`, `testing` or `remediation/**` and on pull requests: install,
   byte-compile, import the entry points, Python and JS unit tests, shellcheck
   `deploy/`, migrate a fresh PostGIS the way the host is shaped (superuser
   first, then transfer ownership and migrate again as the app role, twice for
   idempotency), the database tests, start the app and run `deploy/lib/smoke.sh`
   against it, and an advisory check of the previous commit on the migrated
   schema. The full list is in [README → CI/CD](../readme.md#11-cicd). The job is
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

### Release layout

```
I/.env                       the instance's settings (its OS user U, 0600)
I/data/                      bronze snapshots written by the ETL (U)
I/releases/<full sha>/       one commit's tree, read-only to U
    .venv -> ../../venvs/<hash>
    .env  -> ../../.env
    data  -> ../../data
    DEPLOYED_COMMIT          the full sha, reported by /api/v1/health
I/venvs/<hash>/              one venv per requirements.txt + interpreter
I/current -> releases/<sha>  the only path the systemd units name
```

U is `safety` for prod and `safety-dev` for dev (see "OS users"). Releases and
venvs are owned by root and readable by U's group, so the running application
cannot modify its own code. Bytecode is compiled at deploy
time and the units set `PYTHONDONTWRITEBYTECODE=1`. A venv is keyed by
`sha256(requirements.txt + python3.12 -VV)`, so a commit that does not touch
requirements reuses the previous one and a deploy takes seconds. The three
newest releases are kept (plus whatever `current` and the previous release are),
and unreferenced venvs are removed, at the end of each successful deploy.

`/api/v1/health` reports the running commit:

```bash
curl -s http://127.0.0.1:8001/api/v1/health | jq .commit
```

### What a deploy costs, and what a failed one leaves

- **A good deploy:** one uvicorn restart — a few seconds of 502s from nginx —
  and the serving-layer cache starts empty, so the first requests per city are
  slow again.
- **Rejected at steps 1–5:** the live site is untouched. The commit is marked
  skipped, and the journal says which step failed (the candidate's last log
  lines are included).
- **Failed at step 7:** about a minute of degraded service while the live check
  times out, then the previous release is back.

### Migration checksums and the migrate lock

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

### nginx sites

`deploy/nginx/safety.conf` (prod) and `safety-dev.conf` (dev)
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

### Operating the data

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

**Records withdrawn upstream (F13).** Each scheduled incremental re-reads a
source's revision window (`revision_lookback_days` behind its watermark).
Promotion is an upsert, so a record the agency has since withdrawn would stay in
silver for ever; `safety/etl/withdrawn.py` compares what silver holds for that
window with what the re-pull returned. Setting `WITHDRAWN_RECONCILE` in the
instance's `.env`:

- `report` (the code default, and what prod runs until the user flips it): every
  absent record is counted in one `withdrawn_upstream` / `warn` validation issue
  per pull; nothing is deleted. `/api/v1/quality` lists it.
- `delete`: the same, and the absent rows are deleted from silver -- each one
  copied, in the same statement, into `etl.withdrawn_incident` -- and gold is
  rebuilt in the same run. Deletion is enabled per instance with
  `WITHDRAWN_RECONCILE=delete` in its `.env`; prod stays `report` until the user
  changes that line (dev's is edited as `safety-dev`, see "OS users").
- `off`: no comparison at all. An empty value means `report`; an unknown one
  means `report` with a warning in the log. No restart is needed: each ETL run
  reads the `.env` when it starts.

Only incrementals with a watermark reconcile -- never a backfill, the
incremental's backfill fallback, or a bronze replay (`reprocess`). The domain is
exactly what the pull re-read, minus a day at each end, on the date the
adapter's upstream filter uses (`reconcile_basis`: occurrence date everywhere
except DC, which is filtered on its report date) and only for rows of the
source's current dataset id.

*The outage guard* skips all deletion for the pull (recorded as `skipped`, with
the reason) when the pull looks partial: overall it re-confirmed under 90% of
the rows silver held for the window; or a month (for Austin, a month × layer)
holding 50+ rows re-confirmed under 90%; or one holding 3+ rows came back empty.
A city whose guard keeps tripping -- say a small Austin layer that really was
withdrawn -- gets no deletions until someone looks; the reason names the stratum.

*Reading the numbers.* In report mode the same absent rows are recorded again on
every run, so the `withdrawn_upstream` total in `/quality` (a sum over all
history) keeps growing; read one pull's `detail` instead:
`select pull_id, occurrences, detail from etl.validation_issue where check_name = 'withdrawn_upstream' order by issue_id desc limit 5`
(`action`, `prior_in_window`, `absent_share`, `reason`, `strata`, `sample_keys`).

*Restoring deleted rows.* Archived rows are kept 90 days
(`WITHDRAWN_RETENTION_DAYS`) and then pruned by the ETL itself; after that,
recovery means a bronze replay or a dump. To restore one pull's deletions, as
the instance role, first create the partitions for the rows' years (from the
instance's `current`, as its OS user):

```bash
.venv/bin/python - <<'PY'
from safety.db import connect, ensure_partitions
with connect() as c:
    ensure_partitions(c, "<city>", [<year>, ...]); c.commit()
PY
```

then:

```sql
INSERT INTO silver.incident
SELECT (jsonb_populate_record(NULL::silver.incident, w.row || jsonb_build_object('geom',
        ST_AsEWKT(ST_SetSRID(ST_MakePoint((w.row->>'longitude')::float8,
                                          (w.row->>'latitude')::float8), 4326))))).*
FROM etl.withdrawn_incident w WHERE w.pull_id = <pull_id>
ON CONFLICT (source_id, occurred_year, incident_key) DO NOTHING;
```

and `python -m safety.etl.run gold --city <city>`. (This is
`withdrawn.RESTORE_SQL`, which the CI tests run.) `reprocess --pull-id <older
pull>` is the coarse alternative. Either way, the next incremental deletes the
rows again if they are still absent upstream: set the instance to `report` first.

*Known residual.* An incident whose upstream date is revised from inside the
window to before it drops out of the re-pull and is deleted although it still
exists upstream; a backfill brings it back.

### Backups

What runs, where, retention and restoring are in
[OPERATIONS.md → Backups](OPERATIONS.md#backups). Two points for deploys:

- *Dump window.* `pg_dump` holds ACCESS SHARE on every table for its whole run
  (02:15 UTC, up to 15 min late, about 1.5 min for both databases). A deploy whose
  migration needs ACCESS EXCLUSIVE during that window fails at `lock_timeout=30s`
  (`deploy/lib/install.sh`) before switching: the live site is untouched, but the
  commit is **skipped**, not retried (see [Controls](#controls) to retry it).
  ETL `DELETE`/`INSERT` is unaffected. `Nice=`/`IOSchedulingClass=` on the units
  only lower the priority of the `docker` CLI; the dump runs in the postgres
  backend at normal priority.
- *Restores after role isolation.* Make sure the instance's role
  (`safety_prod`/`safety_dev`) exists before restoring, so the dump's `OWNER TO`
  statements land on it; a dump taken before isolation is owned by `safety`
  throughout, so run `deploy/db/transfer-ownership.sql` on the restored database
  afterwards. `pg_restore --no-owner` instead leaves every object owned by whoever
  ran the restore.

### Database roles

Both instances share one Postgres cluster (`safety_db`).
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
  (see [OPERATIONS.md → Restore a database dump](OPERATIONS.md#restore-a-database-dump)).
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
- *OS users.* The roles stop cross-environment access in the database; the
  separate OS users below (N1) stop dev code from reading prod's `.env` and so
  its credentials.

### OS users

Prod runs as `safety`; dev runs as its own system user, `safety-dev` (N1), so
code deployed to dev -- any push to testing -- cannot read prod's `.env`, data or
`/proc/<pid>/environ`, and prod's user cannot read dev's.

- *How.* `sudo deploy/os-isolate.sh dev` (idempotent) creates `safety-dev`
  (home `/srv/safety-dev`, 0700, which holds its pip cache), installs
  `deploy/systemd/instance-user/dev.conf` as a `10-instance-user.conf` drop-in
  for `safety-{api,etl,etl-hourly,ops}@dev`, writes `safety-dev` to
  `/var/lib/safety-deploy/dev.user`, re-owns the dev tree and restarts
  `safety-api@dev`, then verifies the isolation both ways (it rolls back by
  itself if anything fails). The unit templates are unchanged, so prod's units
  are exactly what they were.
- *The deployer* runs an instance's code (pip, import check, migrate, the
  candidate) as `instance_user` (`deploy/lib/release.sh`): the user named in
  root-owned `/var/lib/safety-deploy/<instance>.user`, `safety` if absent, and
  only from the allow-list `prod:safety`, `dev:safety`, `dev:safety-dev`. Nothing
  in an instance tree can choose it, and prod is always `safety`. The journal
  line `0/7 running dev as safety-dev` shows it.
- *Ownership after isolation.* `dev/` is `root:safety-dev 0750`: root owns it so
  that `safety-dev` cannot replace `releases/`, `venvs/` or `current` with
  symlinks into `prod/` that root would then follow on a deploy (chgrp, build,
  prune). `dev/.env` and `dev/data/` belong to `safety-dev`; releases and venvs
  are sealed `root:safety-dev`, read-only to it. The app directory
  (`/srv/safety/Crime-Prevention-and-Personal-Safety-Application`) and
  `/srv/safety` itself are `root:root` (0755 / 0751), so neither user can rename
  or replace `dev/` or `prod/`; `safety` keeps `/srv/safety/.cache` and its
  dotfiles.
- *No shared inodes.* `chown` re-owns an inode, not a name, so os-isolate
  refuses while any file under `dev/` has another hard link. On this host 189
  bronze files in `dev/data` were hard links of `prod/data`'s (dev was seeded
  from prod), and a first isolation re-owned prod's copies to `safety-dev`
  until it was rolled back. Give dev its own copies first, as dev's current
  user, under its ETL lock (contents and timestamps are kept; prod's files are
  not touched):
  `sudo runuser -u safety -- flock -w 600 dev/data/.etl.lock find dev/data -xdev -type f -links +1 -exec sh -c 'for f; do t=$(mktemp "$f.unlink.XXXXXX") && cp -p "$f" "$t" && mv -f "$t" "$f"; done' sh {} +`
  (paths relative to the app directory).
- *Root never follows a dev-planted path.* Scripts that lock an instance's ETL
  open `data/.etl.lock` read-only and refuse a symlink there; a missing one is
  created by the instance user. Edits to `dev/.env` are made as `safety-dev`
  (`sudo runuser -u safety-dev -- ...`), never by root writing into it.
- *Rollback.* `sudo deploy/os-isolate.sh dev --rollback` removes the drop-ins and
  `dev.user`, puts the tree back to `safety:safety` (`dev/` 0700) and restarts
  dev. The user is kept; `sudo userdel safety-dev && sudo rm -rf /srv/safety-dev`
  removes it afterwards if wanted.
- *Day to day.* Commands that run as dev's user use `safety-dev` (or
  `$(sudo cat /var/lib/safety-deploy/dev.user)`); prod's stay `safety`.
  `deploy/logs.sh` picks the user from the owner of the instance's `.env`.

### Notes for developers

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

---

## Container image (alternative target)

The `Dockerfile` (`python:3.12-slim`) builds one image for both the API and ETL roles. The home server does **not**
use it, and **CI never builds it**.

```bash
docker build -t safety:<tag> .
docker run --rm -p 8000:8000 \
  -e POSTGRES_HOST=<db-host> -e POSTGRES_PORT=5432 -e POSTGRES_DB=safety \
  -e POSTGRES_USER=<role> -e POSTGRES_PASSWORD=<password> \
  -e FORWARDED_ALLOW_IPS=<proxy-ip-or-cidr> \
  safety:<tag>
# Check its paths (comment in .dockerignore):
docker run --rm safety:<tag> python -c "from safety.config import WEB_DIR, MIGRATIONS_DIR, CROSSWALK_DIR; print(WEB_DIR.exists(), MIGRATIONS_DIR.exists(), CROSSWALK_DIR.exists())"
```

The image does **not** provide any of the following; you must supply them yourself:
- an external PostGIS 17/3.5 database;
- a one-off `python -m safety.migrate` before each release;
- `python -m safety.ops` for the first load;
- a scheduler running `python -m safety.etl.run incremental --all --due-only --skip-hourly` (every 6 h) and
  `hourly --all` (weekly);
- a TLS proxy;
- backups;
- a `BRONZE_ROOT` volume (the entrypoint chowns it and then drops to user `safety`).

Leaving `FORWARDED_ALLOW_IPS` at its default of `*` lets clients spoof their IP to the rate limiter. `/api/v1/health` reports
`commit: null`, because the image has no `DEPLOYED_COMMIT` file.

> ⚠️ UNVERIFIED: that the image builds and runs today. It was not built during this documentation work, because there
> was no docker access and building on the production host is not allowed.
