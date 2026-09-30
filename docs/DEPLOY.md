# Deploying to Railway

This puts the whole application on the internet with a working HTTPS address.
You do not need a server, a terminal, or any command-line knowledge. Everything
below happens in a web browser.

Budget about 30 minutes, most of it waiting for builds.

For running the project on your own machine instead, see the quick start in
[`PHASE1.md`](PHASE1.md) — that path is unchanged and needs no Railway account.

---

## What you are setting up

Four pieces, called **services** in Railway:

| Service | What it is | Public? |
|---|---|---|
| `db` | The database (PostgreSQL with PostGIS) | No — internal only |
| `api` | The website and its API | **Yes** — this is the address people visit |
| `etl` | A scheduled job that downloads fresh crime data | No |
| `ops` | A hand-run job for one-off tasks (adding a city, loading population data) | No |

All four live in one Railway **project**. The `ops` service has no schedule and
costs nothing when idle; it exists so that running a one-off command never means
editing the `etl` service and breaking its schedule.

### Why one `etl` service and not one per city

The project covers six cities on five different publication cadences, and §8.2 of
the design document is explicit that a single global nightly refresh is the wrong
shape. The obvious reading — one scheduled service per city — is worse, and
Railway is what settles it: **a volume attaches to exactly one service.** Six ETL
services would mean six separate bronze volumes, six partial copies of the raw
archive, and `reprocess` only able to see whichever city's volume it happens to
be running on. The bronze layer is meant to be one auditable record of what each
city published on each date.

So cadence does not live in Railway's cron field. One schedule fires every few
hours and asks *"is anyone due?"*; `reference.source_registry` answers, from the
`expected_cadence` column it already has. Los Angeles gets asked four times a day
and pulls about twice a month; Chicago pulls daily. That is the `--due-only` flag
in step 3.

---

## Before you start

- A **GitHub account**, with this repository either owned by you or forked to
  your account.
- A **Railway account** — sign up at [railway.com](https://railway.com) using
  *Sign in with GitHub*. That connection is what lets Railway read the code.
- A payment method. Railway's Hobby plan is about **$5/month** plus usage.
  Expect roughly **$10–15/month** for Philadelphia alone; each additional city
  adds database storage and build time, and the weekly time-of-day rebuild is the
  largest single job. Budget higher as cities are added and watch the **Usage**
  tab. There is a trial credit for new accounts.

---

## Step 1 — Create the project and the database

1. On the Railway dashboard, click **New Project**.
2. Choose **Empty Project**. (Not "Deploy from GitHub repo" — the database goes
   in first, so the other services have something to connect to.)
3. Click **Create** or the **+ New** button, then choose **Docker Image**.
4. Enter exactly:

   ```
   postgis/postgis:17-3.5
   ```

   This is the same database image the project uses locally, so the hosted
   database behaves identically to a developer's. A generic PostgreSQL will
   **not** work — the application needs the PostGIS mapping extension.

5. Once the service appears, click it and open the **Settings** tab. Rename it
   to `db` so the variables in step 2 match this guide.
6. Attach a volume. **This is not in the service's Settings tab** — go back to
   the **project canvas** (the view showing each service as a card), then
   **right-click the `db` card** and choose **Attach Volume**. Pressing
   `Ctrl+K` anywhere in the project and typing `volume` works too.

   Set the mount path to:

   ```
   /var/lib/postgresql/data
   ```

   This is where the data actually lives. Without it, everything is erased on
   every restart.

   **Size it for six cities, not one.** Railway grows a volume but will not
   shrink it, and running out mid-write is a bad failure. The six cities total
   about 3,480 km² against Philadelphia's 347 — the cell universe grows roughly
   **tenfold**, and the gold layer scales with cells rather than with incidents.
   Expect on the order of 1.6 million incident rows and several million gold rows,
   dominated by `gold.cell_hour_safety`. **Start at 20 GB.** These are estimates
   from city areas, not measurements; watch the actual figure after the second
   city lands.

   If no volume option appears at all, check your plan under **Usage** or
   **Billing** — Railway gates persistent volumes above the trial tier. The
   database genuinely needs one; there is no workaround that keeps your data.

7. Open the **Variables** tab, switch to the **Raw Editor**, and paste this
   whole block:

   ```
   POSTGRES_DB=safety
   POSTGRES_USER=safety
   POSTGRES_PASSWORD=${{secret(32)}}
   TZ=UTC
   PGTZ=UTC
   ```

   Then click **Save** / **Update Variables**.

   `${{secret(32)}}` asks Railway to generate a random 32-character password.
   You never see or type it, and the other two services reference it by name
   rather than copying the value. After saving, switch out of the Raw Editor and
   confirm `POSTGRES_PASSWORD` shows a random string rather than the literal
   text `${{secret(32)}}`; if it did not resolve, click that variable and use the
   generate-secret option instead.

   If you ever set this password by hand, keep it to letters and digits. It ends
   up inside a connection URL (`safety/config.py`), and characters like `/`,
   `@`, `:` or `#` break the parse.

8. Click **Deploy**, then **wait for this service to finish before creating the
   others**.

   The very first start is slow — mounting a new volume, running `initdb`, and
   installing the PostGIS extensions took about **11 minutes** on a real
   deployment. Watch the **Logs** tab and wait for:

   ```
   database system is ready to accept connections
   ```

   This is worth being patient about. The website service gives the database
   roughly a minute to answer before it gives up, so creating it while this one
   is still initialising produces a string of `database not ready (attempt N/30)`
   messages that look like a configuration error and are not one.

> Do **not** give this service a public domain. Nothing outside the project
> should be able to reach the database.

---

## Step 2 — Add the website service

1. Click **+ New** → **GitHub Repo**, and pick this repository. Authorise
   Railway to access it if prompted.
2. When the service appears, open **Settings** and rename it to `api`.
3. In **Settings → Deploy**, set two fields and deliberately leave a third alone:

   **Custom Start Command — leave this EMPTY.**

   The image already starts correctly by itself. Do not paste a `uvicorn`
   command here. Railway hands a custom start command to the container as
   arguments rather than running it through a shell, so a `$PORT` inside it is
   never expanded and the service crash-loops on:

   ```
   Error: Invalid value for '--port': '$PORT' is not a valid integer.
   ```

   The `Dockerfile`'s built-in command does go through a shell, expands
   `${PORT:-8000}`, and binds `0.0.0.0` — see the developer notes at the end for
   why that address matters.

   **Pre-Deploy Command**

   ```
   python -m safety.migrate
   ```

   This creates the database tables. It runs before each new version goes live,
   and does nothing when the tables already exist.

   **Healthcheck Path**

   ```
   /api/v1/health
   ```

4. Open the **Variables** tab, switch to the **Raw Editor**, and paste this
   whole block:

   ```
   POSTGRES_HOST=${{db.RAILWAY_PRIVATE_DOMAIN}}
   POSTGRES_PORT=5432
   POSTGRES_DB=${{db.POSTGRES_DB}}
   POSTGRES_USER=${{db.POSTGRES_USER}}
   POSTGRES_PASSWORD=${{db.POSTGRES_PASSWORD}}
   ENABLE_DOCS=true
   CACHE_MAX_BYTES=268435456
   CACHE_MAX_ENTRIES=192
   ```

   Then click **Save** / **Update Variables**.

   The `${{db....}}` entries are references, not literal text — Railway
   substitutes the real values at deploy time, so no password is ever copied by
   hand. **They only work if the database service is named exactly `db`.** If
   you named it something else, replace `db` with that name throughout.

   **`CACHE_MAX_BYTES` must fit the instance.** This is the map-layer cache
   (`safety/api/main.py`), sized for six cities: 256 MB here, and it will really
   use it. Set it to roughly **half** the memory the service actually has, leaving
   the other half for Python, the connection pool, and the layer being built
   before it is cached. On a 512 MB instance use `134217728` (128 MB) instead.
   Getting this wrong does not produce a warning — it produces an out-of-memory
   restart under load, which reads like a crash bug.

   Note `5432`, not the `55432` used locally. The local port is unusual only to
   avoid colliding with a developer's own PostgreSQL; inside Railway it is the
   standard port.

5. In **Settings → Networking**, click **Generate Domain**. Railway gives you an
   address like `something.up.railway.app`, with HTTPS already working. This is
   the address to share.

   **If it asks which port, answer `8080`** — and then confirm it against the
   logs.

   Railway injects its own `PORT` variable, `8080` in practice, and the image
   honours it (`${PORT:-8000}` in the `Dockerfile`). So the port the application
   actually listens on is Railway's choice, not yours. The service log states it
   plainly:

   ```
   Uvicorn running on http://0.0.0.0:8080
   ```

   Whatever number appears there is what the domain has to target. If they
   disagree, the site returns 502 or times out while the service looks
   completely healthy and its health check passes — Railway's internal check
   finds the right port even when the public domain does not. Fix it by editing
   the domain under **Settings → Networking** and setting the target port to
   match the log line.
6. Click **Deploy**.

---

## Step 3 — Add the data job

1. Click **+ New** → **GitHub Repo** and pick the **same repository** again.
   Yes, twice — same code, different job.
2. Rename the service to `etl` in **Settings**.
3. In **Settings → Deploy**, set:

   **Custom Start Command**

   ```
   python -m safety.etl.run incremental --all --due-only --skip-hourly
   ```

   Three flags, each doing a specific job:

   - `--all` covers every enabled city rather than one, stalest first, and does
     not let one city's outage stop the others. Six independent government
     portals have six independent bad days.
   - `--due-only` skips cities whose registry cadence says they cannot have new
     data yet. This is what lets one schedule serve five different cadences. When
     nobody is due — the normal outcome of most runs — it exits **0**, so it does
     not light up failure alerting several times a day.
   - `--skip-hourly` leaves the time-of-day layers out of this run. They are the
     most expensive thing the pipeline builds, and the slowest-moving: both their
     windows are 12 months or wider, so a day of new incidents barely moves them.
     Step 3b rebuilds them weekly instead.

   **Cron Schedule**

   ```
   0 */6 * * *
   ```

   Every six hours. The schedule decides how often to *ask*; the registry decides
   who gets pulled. A daily city ends up pulling once a day, and DC's rolling
   last-30-days feed — which republishes continuously — gets picked up each time.

   **Restart Policy** — set to **Never**. This job is meant to finish and exit;
   without this Railway may treat a completed run as a crash and start it again.

   Leave **Healthcheck Path** empty. This service does not serve web requests.

4. Attach a volume, the same way as in step 1 — from the **project canvas**,
   right-click the `etl` card → **Attach Volume** (not the Settings tab). Mount
   path:

   ```
   /data/bronze
   ```

   This keeps a copy of exactly what the city published on each date, so data
   can be rebuilt later without re-downloading.

   **Size it at 10 GB.** The surprise here is not the crime data — that is a few
   megabytes gzipped per pull — but the census archives. The population
   denominator reads a TIGER/Line block shapefile per state, and California's and
   Texas's are hundreds of megabytes each; six states of those, plus the TIGER
   PLACE files behind the city boundaries, is most of the space.

   **There is no retention policy.** Every scheduled run writes a new snapshot
   and nothing removes old ones, so this volume grows indefinitely — roughly a few
   gigabytes a year across six cities. That is a deliberate trade for
   auditability, but it is a decision to revisit rather than a fact to discover
   when the volume fills.

   Unlike the database, this one is optional. If you cannot attach a volume,
   set `BRONZE_ROOT` to `/tmp/bronze` in step 5 instead and carry on — you lose
   only `reprocess --pull-id`. The website never reads these files.

5. Open the **Variables** tab, switch to the **Raw Editor**, and paste this
   whole block:

   ```
   POSTGRES_HOST=${{db.RAILWAY_PRIVATE_DOMAIN}}
   POSTGRES_PORT=5432
   POSTGRES_DB=${{db.POSTGRES_DB}}
   POSTGRES_USER=${{db.POSTGRES_USER}}
   POSTGRES_PASSWORD=${{db.POSTGRES_PASSWORD}}
   BRONZE_ROOT=/data/bronze
   SOCRATA_APP_TOKEN=
   ```

   Then click **Save** / **Update Variables**.

   Same database block as the website, with `BRONZE_ROOT` added and
   `ENABLE_DOCS` left off — this service serves no web pages. If you skipped the
   volume in step 4, use `BRONZE_ROOT=/tmp/bronze` instead.

   `SOCRATA_APP_TOKEN` is optional and can stay empty. Four of the six cities —
   Chicago, Seattle, Los Angeles, Austin — are on Socrata, which throttles
   anonymous callers by IP. A token is **not a credential**: it identifies the
   caller so requests count against a per-token quota instead of a shared one.
   Nothing needs it to work; a 24-month backfill across four Socrata cities is
   where the anonymous limit starts to bite. Register free at
   [evergreen.data.socrata.com/signup](https://evergreen.data.socrata.com/signup).

6. Click **Deploy**.

---

## Step 3b — Add the weekly rebuild of the time-of-day layers

Skipped by the six-hourly job, so something has to build them.

1. In the **`etl` service**, open **Settings → Deploy**. Railway allows one cron
   schedule per service, so this needs its own service: click **+ New** →
   **GitHub Repo**, same repository, and rename it `etl-hourly`.
2. **Custom Start Command**

   ```
   python -m safety.etl.run hourly --all
   ```

3. **Cron Schedule**

   ```
   0 4 * * 0
   ```

   4:00 AM UTC on Sundays.

4. **Restart Policy** — **Never**. Leave **Healthcheck Path** empty.
5. No volume needed: this service reads and writes the database only.
6. **Variables** — the same block as `etl`, minus `BRONZE_ROOT`:

   ```
   POSTGRES_HOST=${{db.RAILWAY_PRIVATE_DOMAIN}}
   POSTGRES_PORT=5432
   POSTGRES_DB=${{db.POSTGRES_DB}}
   POSTGRES_USER=${{db.POSTGRES_USER}}
   POSTGRES_PASSWORD=${{db.POSTGRES_PASSWORD}}
   ```

7. Click **Deploy**.

**Why weekly is enough, and what it costs.** The hourly layers rank cells within
each of 24 hour-blocks, over 12- and 24-month windows, at two cell sizes, per
severity scheme — the single most expensive thing the pipeline builds, and the
piece most likely to make a scheduled run time out. Because those windows are a
year or wider, a week of new incidents shifts an hourly percentile very little.

The cost is real though, and worth knowing rather than discovering: between runs,
the hourly view reflects last Sunday's build while the all-hours percentile it is
compared against is current. The "change from usual" figure is therefore briefly
derived from two slightly different windows of data. If that matters more to you
than the compute, move this to daily — or drop `--skip-hourly` from step 3 and
delete this service.

---

## Step 3c — Add the `ops` service for one-off tasks

Adding a city, loading its population denominator, and the first backfill are all
one-off commands. Running them by editing the `etl` service's start command works
but leaves that service pointed at the wrong command until you remember to change
it back — and if the cron fires meanwhile, it runs the wrong thing.

1. **+ New** → **GitHub Repo**, same repository. Rename it `ops`.
2. **Settings → Deploy**: set **Restart Policy** to **Never**, leave **Cron
   Schedule** empty, leave **Healthcheck Path** empty.
3. **Custom Start Command** — whatever you need right now, for example:

   ```
   python -m safety.etl.run status
   ```

4. Attach a volume at `/data/bronze` if you will run backfills or census loads
   from here. Note this is a *different* volume from `etl`'s — Railway cannot
   share one between services — so snapshots written here are not visible to
   `etl`'s `reprocess`. For a first backfill that is fine, because the data also
   lands in the database; if you want one coherent archive, run backfills by
   temporarily pointing the `etl` service at them instead.
5. **Variables** — the same block as `etl`.
6. To run a task: edit the start command, click **Deploy**, read the **Logs**.
   The service sits idle and costs nothing between runs.

---

## Step 4 — Load the data the first time

The scheduled job will not run until the next six-hour tick. To load now, open the
`etl` service and click **Deploy** (or **Redeploy**) to run it once immediately.

The first run downloads 24 months of Philadelphia crime data — about 320,000
records — and takes roughly **3 minutes**. It does this automatically: the job
notices there is no data yet and performs a full load instead of an incremental
one. There is no separate command to run, and `--due-only` never skips a city
that has never been pulled.

Watch the **Logs** tab. A finished run prints a summary ending with a count of
records loaded.

Only Philadelphia loads at this point. The other five cities are seeded in the
registry but `enabled = false`, so nothing touches them — see step 6.

Because step 3 passes `--skip-hourly`, the time-of-day view stays empty until
`etl-hourly` runs. To see it immediately, use the `ops` service with:

```
python -m safety.etl.run hourly --all
```

---

## Step 5 — Check that it worked

Open your `something.up.railway.app` address in a browser.

- [ ] The map loads and shows coloured hexagons over Philadelphia
- [ ] The header shows a "data as of" date
- [ ] Clicking a hexagon opens a panel with counts, a monthly trend, and offence
      types
- [ ] **"Use my location"** asks for permission and highlights a cell. This one
      only works over HTTPS, so it confirms the secure address is genuinely
      working
- [ ] Visiting `/api/v1/health` shows `"status": "ok"` and a non-zero
      `"incidents"` count
- [ ] Visiting `/docs` shows the interactive API documentation

If the map is empty but the page loads, the data job has not finished yet.
Check the `etl` service's logs and try again in a few minutes.

---

## Step 6 — Turning on another city

Each city goes live independently, and enabling one is deliberately a manual act:
it is the moment that city's numbers start being shown to people. Nothing in the
infrastructure changes — no new service, no new variable, no schedule edit. The
`etl` job picks up any newly enabled city on its next tick.

A city is ready to enable when all four of these exist: an adapter, a reviewed
crosswalk, a coverage boundary, and a first backfill you have actually read. See
[`PHASE2.md`](PHASE2.md) for the per-city checks and which cities are ready.

**1. Enable it.** Open the `db` service's **Data** tab (or connect any SQL client
to the public proxy URL Railway shows there) and run one statement:

```sql
UPDATE reference.source_registry SET enabled = true WHERE source_id = 'chi';
```

**2. Load it.** On the `ops` service, set the start command and click **Deploy**.
One command per deploy, in this order — the second needs the first, because the
population figures are trimmed to the city boundary the backfill fetches:

```
python -m safety.etl.run backfill --city chi
```

```
python -m safety.etl.run census --city chi
```

```
python -m safety.etl.run gold --city chi
```

Or skip all three and just wait: the six-hourly `etl` job sees a city with no
watermark and does the full backfill itself. It will not load the population
denominator, though, so the safety ranking stays area-based for that city until
`census` runs.

**3. Read the checks before trusting it.**

```
python -m safety.etl.run status
```

```
python -m safety.etl.run weights --city chi
```

`PHASE2.md` lists what the numbers should look like — rejection rate in the low
single percents, zero unmapped offence codes, census retention above 95%.

**4. The website picks it up on its own.** A city selector appears in the filter
row as soon as a second city has data, populated from `/api/v1/cities`. It is
hidden while only one city is loaded, because a dropdown with one entry is a
control that cannot do anything.

> Values are **never comparable between cities**. Every percentile is computed
> against its own city's distribution (design doc §3.3), so a 0.8 in Chicago and a
> 0.8 in Austin are not the same quantity. That is why there is no side-by-side
> comparison view, and why switching cities replaces the map rather than adding to
> it.

**Turning a city back off** is the same `UPDATE` with `false`. Its data stays in
the database and stays visible on the site — `enabled` controls whether the ETL
pulls it, not whether the API serves it. To take it off the site, delete its
`gold.city_snapshot` row.

---

## Keeping it running

**Updating the code.** Push to the `main` branch on GitHub. Railway rebuilds and
redeploys every service that deploys from the repo — `api`, `etl`, `etl-hourly`
and `ops`. Nothing else to do. Note `ops` will re-run whatever start command it
was last left pointing at, so leave it on something harmless like
`python -m safety.etl.run status`.

**Fresh data.** Handled by the `etl` schedule every six hours, and `etl-hourly`
weekly. No action needed.

**Annual maintenance.** One real item: the jobs half of the population
denominator comes from LODES, which publishes yearly. Bump `LODES_YEAR` in
`safety/etl/census.py` and re-run `census` per city from `ops`. The population
half is decennial and will not move until the 2030 Census.

**Backups.** Railway does not back up volumes on every plan. If this data
matters, check your plan's backup options for the `db` service volume. The data
can always be rebuilt from the cities' public sources, but that takes longer with
six than with one and loses the record of past pulls.

**Cost.** Watch the project's **Usage** tab for the first week after each city is
added. The database volume and the always-on `api` service are the steady costs.
Among the jobs, `etl-hourly` is by far the most expensive per run — it is the
reason `--skip-hourly` exists on the frequent one. If the bill is higher than
expected, that weekly job and the `db` volume are the two places to look.

---

## If something goes wrong

**The `api` service keeps restarting.** Open its Logs. A message about the
connection pool timing out after 30 seconds almost always means a database
variable is wrong — most often `POSTGRES_HOST` typed literally instead of as a
`${{db.RAILWAY_PRIVATE_DOMAIN}}` reference, or the database service having a
different name than `db`.

**"relation does not exist" errors.** The Pre-Deploy Command in step 2 did not
run or failed. Check the deployment logs for the `python -m safety.migrate`
step.

**The map is empty and `/api/v1/health` shows `"incidents": 0`.** The data job
has not completed. Run the `etl` service manually as in step 4 and read its
logs.

**A `429 Too Many Requests` response.** Expected behaviour, not a fault. The
application limits how fast a single visitor can request the expensive map
layer, because the API has no login and that request builds the entire city's
data. See `safety/api/ratelimit.py`. Normal use of the site never reaches it.

**The `etl` service shows as failed after a successful run.** Check that
**Restart Policy** is set to **Never** (step 3). A job that finishes and exits
looks like a crash under the default policy.

**The `etl` job finishes instantly and loads nothing.** Expected, most of the
time. With `--due-only` and a six-hourly schedule, most runs find that no city is
due yet and exit 0. The logs say so per city — `chi not due: last checked 2.1h
ago, cadence 'daily' waits 19.2h`. If a city you expect to be pulling never
appears, check that it is `enabled` in the registry.

**The `etl` job exits non-zero but most cities loaded.** Also expected: `--all`
attempts every city, collects failures, and exits non-zero if *any* failed, so one
city's portal being down marks the whole run failed. The JSON summary at the end of
the log names which. That is the intended trade — a silent partial failure across
six sources is worse than a noisy one.

**The time-of-day view is empty, or "change from usual" looks stale.** The
frequent job passes `--skip-hourly`, so those layers come from `etl-hourly`'s last
weekly run. If it has never run, run it once from `ops`. See step 3b for the
trade-off this makes.

**A newly enabled city shows no safety ranking, only counts.** Its population
denominator has not been loaded. Run `census --city <id>` from `ops`, then
`gold --city <id>`. Until then the ranking has no ambient population to divide by.

**The `api` service restarts under load.** Check `CACHE_MAX_BYTES` against the
instance's memory (step 2). The default is sized for six cities and will use what
it is given; on a small instance it needs lowering.

---

## Notes for developers

`railway.json` at the repository root deliberately contains **build
configuration only**. All four repo-backed services read the same file — a
`healthcheckPath` there would be applied to the three cron and one-off services
too, which never serve HTTP and would fail their health check forever. Deploy
settings are per-service in the UI for that reason, and that constraint is also
why the schedules are split across services: Railway allows one cron expression
per service, so `etl` and `etl-hourly` cannot be one service with two schedules.

All four services run the same image, built from `Dockerfile`, and differ only in
start command and variables. `safety/config.py` reads all configuration from
environment variables, so no `.env` file exists or is needed in the container.

**Cadence lives in the database, not in cron.** `--due-only` reads
`reference.source_registry.expected_cadence` and the timestamp of the last
incident-mode row in `etl.pull_run`, so Railway's schedule only decides how often
to ask. Two details in `safety/etl/run.py` worth knowing before changing it:

- It keys on when a pull last *ran*, not when one last *succeeded*.
  `last_success_at` only moves on a succeeded pull, and a pull that finds nothing
  new returns `no_new_data` without touching it — the normal outcome for a
  bi-weekly source. Keying on success would leave Los Angeles permanently overdue
  and re-checked on every single tick, which is the waste the flag exists to
  avoid.
- It only counts `backfill` and `incremental` pulls. Census and boundary pulls
  write `etl.pull_run` rows too, and letting one of those suppress an incident
  pull would be wrong.

**Only one service should run migrations.** `python -m safety.migrate` is the
`api` service's Pre-Deploy Command and belongs nowhere else. It is idempotent, so
a second caller would be harmless rather than dangerous, but two services racing
to apply the same DDL on a shared push is worth not arranging.

**Leave the `api` service's start command empty.** Railway passes a custom start
command as argv, with no shell, so `$PORT` in it stays a literal string and
uvicorn rejects it. The `Dockerfile`'s `CMD` is `sh -c "exec uvicorn ..."`,
which expands `${PORT:-8000}` and keeps uvicorn as PID 1 so stop signals reach
it. The `etl` service does need its start command, but that one contains no
variables, so argv is fine.

**The bind address must be `0.0.0.0`, not `::`.** A `::` bind reads as
dual-stack but is not: Python sets `IPV6_V6ONLY` on the listening socket, so the
process accepts IPv6 connections only. The failure is quiet and misleading —
uvicorn logs a normal startup, no request line ever appears in the logs, and
callers get an empty reply rather than a refusal. Railway's edge proxy arrives
over IPv4. The IPv6-only private network matters for *outbound* connections to
the database, which the listen address has no bearing on. Verified by running
the image both ways against a local PostGIS container.
