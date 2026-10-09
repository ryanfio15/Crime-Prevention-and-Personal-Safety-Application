# Operations

How to observe, maintain and repair the two instances on the home server. In this document, `I` is
`/srv/safety/Crime-Prevention-and-Personal-Safety-Application/<prod|dev>`. The OS user `U` is `safety` for prod and
`safety-dev` for dev. Deploying and rolling back are covered in [DEPLOYMENT.md](DEPLOYMENT.md).

Contents: [Schedules](#schedules) · [Logs](#logs) · [Health and run history](#health-and-run-history) ·
[Alerts](#alerts) · [Backups](#backups) · [Limits](#resource-limits-and-scaling) · [Runbook](#runbook) ·
[What normal looks like](#what-normal-looks-like)

---

## Schedules

All schedules are systemd timers. The host clock is UTC. The ETL timers have no explicit `UTC` and rely on that.

| Timer | Schedule | Runs |
|---|---|---|
| `safety-autodeploy@{prod,dev}` | 2 min after boot, then every ~60–70 s | `/usr/local/sbin/safety-autodeploy <i>` as root |
| `safety-etl@prod` | `00/6:00` + up to 15 min random delay | `safety.etl.run incremental --all --due-only --skip-hourly` |
| `safety-etl@dev` | `03/6:00` (drop-in `safety-etl@dev.timer.d/offset.conf`) | same |
| `safety-etl-hourly@prod` / `@dev` | Sun 04:00 / Sun 06:00 | `safety.etl.run hourly --all` (time-of-day layers) |
| `safety-backup` | daily 02:15 UTC + up to 15 min | `backup.sh dump` (both databases) |
| `safety-backup-verify` | 1st of the month, 03:15 UTC | `backup.sh verify` (test restore of prod). **Has not run yet**; first run 2026-11-01 |
| `certbot.timer` (distro) | twice daily | `certbot -q renew` |
| `safety-ops@<i>` | started after every successful deploy; also hourly at :20 UTC once `safety-ops@<i>.timer` is enabled by hand | `safety.ops` (loads history in runs of up to 25 min) |

ETL, hourly and ops share the lock `I/data/.etl.lock` (`flock -w 21600`), so they queue rather than overlap. A deploy
is deferred (exit 75) while any of them runs. Missed runs catch up at boot (`Persistent=true`).

`safety.etl.run --all --due-only` pulls a city only when its last completed pull is at least 0.8 × its cadence old.
Cadence lives in `reference.source_registry.expected_cadence`: daily for phl, chi, sea, dc and aus, biweekly for lax.

---

## Logs

| What | Command |
|---|---|
| API (uvicorn startup and an access line per request) | `journalctl -u safety-api@prod -f` |
| ETL / ops history plus their journal | `deploy/logs.sh prod [--since 7d] [--city chi] [--failed] [-f]` (uses sudo for the history part) |
| One scheduled ETL run | `journalctl -u safety-etl@prod -n 100 --no-pager` (syslog tag is `flock`, so filter by unit, not tag) |
| Deploys | `journalctl -u safety-autodeploy@prod -f`; failures only: `-p err` |
| Backups | `journalctl -u safety-backup` / `-u safety-backup-verify` |
| Alerts | `journalctl -t safety-notify -p err`; `sudo cat /var/lib/safety-notify/alerts.log` |
| nginx | `/var/log/nginx/safety.{access,error}.log`, `/var/log/nginx/safety-dev.{access,error}.log`. 429s are logged at `warn`. Rotated daily, kept 14 days |

Format notes:
- The ETL, ops and migrate CLIs log `"%(asctime)s %(levelname)-7s %(name)s: %(message)s"` at INFO.
- `safety.etl.run` and `safety.ops` print a JSON summary per run. `safety.migrate` prints `Applied N migration(s): …`
  or `Schema already up to date.`
- **The API never configures Python logging.** Only uvicorn's own lines appear; app INFO lines such as
  `connection pool ready` are dropped.
- The journal is persistent and about 1 GB. uvicorn access lines contain full client IPs.
- There is no log shipping and no metrics or tracing. The only counters are the cache stats in `/api/v1/health`.
- `ryan` reads the journal through the `adm` group, without sudo.

---

## Health and run history

```bash
curl -s https://ryanfioserver.ddns.net/api/v1/health | jq           # through nginx
curl -s http://127.0.0.1:8000/api/v1/health | jq .commit             # bypasses nginx (dev: 8001)
curl -s https://ryanfioserver.ddns.net/api/v1/cities | jq '.cities[] | {source_id, enabled, last_refreshed_at}'
curl -s 'https://ryanfioserver.ddns.net/api/v1/quality?city=chi' | jq  # validation issues + recent pulls for one city
```

- `/api/v1/health` returns `status`, `commit`, `pipeline_version`, `data_as_of`, `last_refreshed_at`, `incidents` (a
  JSON **string**) and `cache`.
- It runs one aggregate query, so it proves the DB is reachable. It does **not** check freshness: a stalled ETL still
  reports `"ok"`.
- A pool or statement timeout gives 503. An unmigrated schema gives 500.

From the command line, run each command as the instance user:

```bash
run() { sudo -u safety env -C /srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod/current \
        PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m "$@"; }      # dev: -u safety-dev …/dev/current
run safety.etl.run status                         # JSON: registry, last 10 pulls, issues, snapshots (all cities)
run safety.etl.run log --city chi --since 7d      # pull_run + ops_run history; --failed for failures only
run safety.ops --dry-run                          # what ops would do; each city shows "up to date" when converged
```

`run` is fine for reads and for `enable`. Anything that writes data (`backfill`, `incremental`, `census`, `gold`,
`hourly`, `safety.ops` without `--dry-run`) should take the ETL lock the way the units do. Otherwise it can overlap a
scheduled run, and a deploy will not be deferred for it:

```bash
runlocked() { sudo -u safety env -C /srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod/current \
        PYTHONDONTWRITEBYTECODE=1 flock -w 21600 data/.etl.lock .venv/bin/python -m "$@"; }
```
If the run may overlap a migrating deploy, pause autodeploy first with
`sudo systemctl stop safety-autodeploy@prod.timer`.

---

## Alerts

Every API, ETL, ops, deploy and backup unit has `OnFailure=safety-notify@%N.service`. `deploy/lib/notify.sh` writes
`<unit> failed on <host> at <UTC>; see journalctl -u <unit>` at priority `err` with tag `safety-notify`, and appends the
same line to `/var/lib/safety-notify/alerts.log`.

**Alerts go nowhere else.** That was a decision dated 2026-10-07 (`notify.sh:6-13`), so nobody is paged. To add a
channel, add one best-effort command after `logger` in `deploy/lib/notify.sh`, then run
`sudo deploy/install-deployer.sh`.

These failures produce no alert at all:
- stale data (an ETL that exits 0 with nothing new);
- a city stuck inside the ops retry cooldown;
- the disk filling up between backups;
- certificate expiry or a failed `certbot.service`;
- possibly an API crash loop: `RestartSec=5` against the default start limit of 5 per 10 s may never trip
  `OnFailure` (⚠️ UNVERIFIED).

---

## Backups

| | |
|---|---|
| What | `pg_dump -Fc` of `safety` then `safety_dev`, via `docker exec safety_db` as the superuser over the container socket. Each archive is checked with `pg_restore -l`. A `.meta` sidecar records the migration count |
| Where | `/var/backups/safety/<db>-<UTC timestamp>.dump`, root 0700/0600, **unencrypted**, on the **same disk** as the DB. No off-host copy exists |
| Retention | Prod older than 7 days and dev older than 2 days are deleted, only after a successful dump of that database |
| Disk guard | A dump needs 2× the previous dump + 15 GB free (`SAFETY_BACKUP_MIN_FREE_GB`). Verify needs 6× + 15 GB on `/` |
| Size | About 330 MB per database, about 46 s each (2026-10-08) |
| Verify | Monthly: restores the newest **prod** dump into `safety_restore_check`, compares the migration count, checks that `silver.incident` and `gold.city_snapshot` are non-empty, then drops it. Dev dumps are never verified |
| Not backed up | Bronze files (`I/data`), the `.env` files, `/var/lib/safety-deploy`, `/etc/letsencrypt` |

Run a backup now: `sudo systemctl start safety-backup`. Run the restore check now:
`sudo systemctl start safety-backup-verify`. That is worth doing once, since it has never run.

### Restore a database dump

> ⚠️ UNVERIFIED: no restore has been run on this host yet. Run `sudo systemctl start safety-backup-verify` once to
> prove a dump restores before relying on this.

```bash
sudo ls -t /var/backups/safety/                      # pick the newest safety-<UTC timestamp>.dump
sudo docker exec safety_db createdb -U safety safety_restored
sudo sh -c 'docker exec -i safety_db pg_restore -U safety -d safety_restored --exit-on-error \
    < /var/backups/safety/safety-<timestamp>.dump'     # the directory is root 0700, so redirect inside sudo
```
- Create the instance role (`safety_prod` / `safety_dev`) **before** restoring, so the dump's `OWNER TO` statements
  apply to it.
- A dump taken before `db-isolate.sh` ran is owned by `safety`. Run `deploy/db/transfer-ownership.sql` on it afterwards.
- Then either set `POSTGRES_DB=safety_restored` in `I/.env` and `sudo systemctl restart safety-api@<i>`, or rename the
  restored database into place with the API stopped.

> ⚠️ UNVERIFIED: the "rename into place" steps are not in the repo. They would be: terminate connections, rename,
> re-apply `REVOKE CONNECT … FROM PUBLIC` and `GRANT CONNECT, TEMPORARY … TO <role>` as `db-isolate.sh` does.

### Restore rows withdrawn upstream

When `WITHDRAWN_RECONCILE=delete`, rows that disappear upstream are moved to `etl.withdrawn_incident` and kept for
`WITHDRAWN_RETENTION_DAYS`, 90 by default. The restore procedure (an `ensure_partitions` snippet, then
`withdrawn.RESTORE_SQL` for one `pull_id`, then `safety.etl.run gold --city <city>`) is in
[DEPLOYMENT.md → Operating the data](DEPLOYMENT.md#operating-the-data) ("Restoring deleted rows"). Set the instance
to `report` first, or the next incremental deletes the rows again.

---

## Resource limits and scaling

| Knob | Value | Where |
|---|---|---|
| uvicorn workers | 1 process per instance (no `--workers`) | `deploy/systemd/safety-api@.service:32` |
| API DB pool | min 1, max 8; 10 s wait for a slot → 503; waits up to 30 s for the DB at startup | `safety/api/main.py:47-71`, `config.py:83` |
| API statement timeout | 15,000 ms (`API_STATEMENT_TIMEOUT_MS`) | `main.py:58`, `config.py:82` |
| Response cache | 192 entries / 256 MiB (`CACHE_MAX_ENTRIES` / `CACHE_MAX_BYTES`), per process, emptied on restart | `config.py:71-72` |
| Rate limits | `/api/v1/cells` 2 r/s burst 10; other `/api/v1/*` 10 r/s burst 20; 20 connections per IP (nginx); app limiter capped at 4,096 clients | `deploy/nginx/safety.conf:46-51`, `safety/api/ratelimit.py:65-81` |
| nginx | `proxy_read_timeout 60s`, `client_max_body_size 1m` | `safety.conf:76,111` |
| Postgres | `shared_buffers=256MB`, `work_mem=32MB`, `max_parallel_workers_per_gather=2`; no container memory limit | `docker-compose.yml:45-52` |
| ETL HTTP | 120 s timeout, 4 retries | `config.py:92-93` |
| Unit timeouts | ETL / hourly / ops: **none** (a hung run holds the ETL lock and blocks deploys indefinitely); autodeploy 30 min; backup 3 h; verify 4 h | unit files |
| Memory / CPU limits | No unit sets `MemoryMax` or `CPUQuota` | unit files |

Scaling is vertical only. The cache and rate limiter are per process, and prod and dev share one Postgres cluster.

---

## Runbook

| Task | Command |
|---|---|
| Is it up, and on which commit? | `curl -s https://ryanfioserver.ddns.net/api/v1/health \| jq '{status,commit}'` |
| Force a scheduled ETL run now | `sudo systemctl start --no-block safety-etl@prod`, then `journalctl -u safety-etl@prod -f` |
| Rebuild the time-of-day layers now | `sudo systemctl start --no-block safety-etl-hourly@prod` |
| Load whatever is missing | `sudo systemctl start --no-block safety-ops@prod`, then `journalctl -u safety-ops@prod -f`. These are oneshot units: a start while one is already active does not queue a second pass, so wait for `systemctl is-active` to say `inactive` |
| Retry an ops step held by the cooldown | `runlocked safety.ops --force` (`OPS_FORCE=1` does the same in a unit's environment; `sudo` drops it from yours) |
| Pull one city by hand | `runlocked safety.etl.run incremental --city chi` (also `backfill`, `census`, `gold`, `hourly`) |
| Enable a city | `run safety.etl.run enable --city chi`, then `sudo systemctl start --no-block safety-ops@prod`, then `run safety.etl.run weights --city chi` |
| Disable a city | `run safety.etl.run enable --city chi --off`. This stops its ETL, but `/api/v1/cities` keeps serving its last snapshot |
| Switch the severity scheme | `run safety.migrate --activate nscs_v2_percapita [--city chi]`. This changes what every safety percentile means |
| Annual census refresh | `runlocked safety.etl.run census --all --lodes-year <new year>` for a one-off, or bump `LODES_YEAR` in `safety/etl/census.py` (currently 2023) and deploy to change the default |
| Change a setting | Edit `I/.env` as `U` (`sudo -u safety-dev` for dev), then `sudo systemctl restart safety-api@<i>`. ETL picks it up on its next run |
| Turn off `/docs` | Add `ENABLE_DOCS=false` to `I/.env` and restart the API |
| Rotate an instance DB password | Re-running `sudo deploy/db-isolate.sh <i>` sets a new random password and rewrites `.env` (⚠️ by reading the code; not documented as a rotation procedure). There is **no** procedure for the superuser |
| Emergency SQL | `sudo docker exec -it safety_db psql -U safety -d safety`. Run `SET ROLE safety_prod;` before any DDL |
| Reclaim disk after big deletes | `sudo -u safety env -C /srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod/current .venv/bin/python scripts/storage.py sizes` (read-only). `compact` runs `VACUUM FULL` on gold, needs free disk and holds exclusive locks. ⚠️ Not routine; no schedule exists |
| Deployer, units or nginx changed in git | `sudo deploy/install-deployer.sh [--units] [--nginx]` from an up-to-date checkout. `--nginx` refuses if the live file has drifted |
| Certificates | Automatic. Check with `sudo certbot certificates`. A hostname change means editing both `deploy/nginx/*.conf`, re-running certbot, then committing the live file back |

`run` and `runlocked` are the helpers defined in [Health and run history](#health-and-run-history).

### Loading a city's full history

The backfill keeps 24 months. The history load walks a city back to `reference.source_registry.history_start_date`
(Chicago 2001, Philadelphia 2006, DC and Seattle 2008, Los Angeles 2010), one pull per slice of 6 months
(Chicago 3), newest first. Los Angeles reads three datasets one after another (NIBRS, then LAPD's legacy 2020–2024
and 2010–2019 datasets); `log` shows which dataset each pull read.
Each loaded year adds a window to that city's list on the next gold refresh. It is off until switched on per city and
per instance, and **dev goes first**: prod and dev share one disk, and the full history makes each database several
times larger (step 1 gives the estimate).

1. **Measure before loading** (read-only; dev shown):
   ```bash
   sudo -u safety-dev env -C /srv/safety/Crime-Prevention-and-Personal-Safety-Application/dev/current \
       PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/storage.py history-gate
   ```
   It projects silver, bronze and gold growth per city from today's bytes per row, times 2 instances, against free
   space on `/`, and fails if less than 20 GB would be left.
2. **Load one slice of one city on dev, and measure again** (`runlocked` as defined above, with `-u safety-dev` and
   `…/dev/current`):
   ```bash
   run safety.etl.run enable --city phl --history
   runlocked safety.etl.run history --city phl --max-slices 1
   ```
   Re-run `history-gate`: the bytes per row are now the real ones for older data.
3. **Let ops finish it.** The timer is a new unit file, so install it once from an up-to-date checkout, then enable it
   per instance: `sudo deploy/install-deployer.sh --units`, then `sudo systemctl enable --now safety-ops@dev.timer`.
   Each hourly run loads slices for up to
   `OPS_HISTORY_MINUTES` (25) and stops, so the six-hourly pull and deploys never wait long. Watch it with
   `run safety.etl.run log --city phl --since 1d` or `journalctl -u safety-ops@dev -f`. When a city's history is
   complete, its last run rebuilds gold; until then the six-hourly pull's own refresh picks up each new year.
4. **Check the cost of a refresh.** Every gold refresh logs `gold refresh for <city> took {...}` with per-phase
   seconds (also in the `timings_seconds` field of `gold`'s JSON output). If one city's refresh holds the ETL lock
   for more than about 15 minutes, use the fallback below.
5. **Then the next city**, smallest first: phl, sea, dc, chi. Run `history-gate` between cities.
6. **Prod**, the same way, one city at a time, after dev has run the full set for a few days.

**Fallback if it does not fit.** Set `SAFETY_MAX_WINDOW_YEARS=2` in `I/.env` (as the instance user). Windows longer
than two years then keep their incident counts but are built without the safety ranking, and the map says so. The next
gold refresh applies it (`runlocked safety.etl.run gold --city <id> --skip-hourly` for one city now). To give the space
back as well, run `scripts/storage.py compact` in a quiet window.

**Turning it off.** `run safety.etl.run enable --city <id> --history --off` stops further loading. Rows already
loaded stay. Removing them means deleting that city's older `silver.incident_<id>_<year>` partitions by hand and
rebuilding gold.

**Seattle** before May 2019, **DC** in 2008 and **Los Angeles** before 2025 were recorded differently from today.
Their windows carry that caveat (`reference.source_series_caveat`). Seattle's older rows are mapped through crosswalk
rows marked `approximate`; Los Angeles' through hand-mapped `CRM-<code>` rows for LAPD's own crime codes.

### First steps when the site is down

1. `curl -sS -o /dev/null -w '%{http_code}\n' https://ryanfioserver.ddns.net/api/v1/health`
   - A DNS or TLS error points to DDNS, the router or the certificate (step 5).
   - **502**: the API is down (step 2).
   - **503**: the pool or a query timed out (step 3).
   - **200** but `"incidents": null`, or far below the normal ~1.5 M: no data (step 4).
2. **502**: `systemctl status safety-api@prod --no-pager; journalctl -u safety-api@prod -n 50 --no-pager`. A pool timeout
   at startup means the DB is down or a `POSTGRES_*` value in `.env` is wrong. Test without nginx:
   `curl -s http://127.0.0.1:8000/api/v1/health`.
3. **503 or slow**: `sudo docker ps --filter name=safety_db`;
   `systemctl status safety-etl@prod safety-ops@prod --no-pager`; `journalctl -u safety-autodeploy@prod -n 30`.
4. **Empty or stale map**: `deploy/logs.sh prod --since 2d --failed`, then `sudo systemctl start --no-block safety-ops@prod`.
5. **Certificate or DNS**: `sudo certbot certificates`, `getent hosts ryanfioserver.ddns.net` compared with the
   public IP, and `/var/log/nginx/safety.error.log`.

Always check `journalctl -t safety-notify -p err` as well.

---

## What normal looks like

A snapshot from 2026-10-06 to 2026-10-08. These are observations, not thresholds.

| Signal | Normal |
|---|---|
| API memory | ~46 MB, peak ~51 MB, ~10 tasks |
| ETL tick | 8–11 s CPU; usually 0–1 city due |
| Backup | ~330 MB and ~46 s per database |
| Traffic | prod ~1.7k nginx lines/day, dev ~2.6k |
| Incidents served | ~1.53 M across 6 cities |
| Alerts | none, apart from the 2026-10-07 drill |
| Autodeploy journal | `prod: up to date at <sha>` every minute |

### Maintenance calendar

| Item | Cadence | Automated? |
|---|---|---|
| ETL, time-of-day rebuild, backups, restore check, cert renewal | see [Schedules](#schedules) | yes |
| OS security updates | daily | unattended-upgrades, **no automatic reboot** |
| Read `journalctl -t safety-notify` | whenever you are on the host | manual |
| `LODES_YEAR` bump + `census --all` | yearly | manual |
| Python dependency bumps (`requirements.txt`; no Dependabot) | — | manual, ⚠️ no owner |
| MapLibre upgrade (GHSA-jrc7-96c5-q579) | once | manual, ⚠️ open |
| Off-host backup copy | — | **does not exist** |
