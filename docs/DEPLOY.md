# Deploying to Railway

This puts the whole application on the internet with a working HTTPS address.
You do not need a server, a terminal, or any command-line knowledge. Everything
below happens in a web browser.

Budget about 30 minutes, most of it waiting for builds.

For running the project on your own machine instead, see the quick start in
[`PHASE1.md`](PHASE1.md) — that path is unchanged and needs no Railway account.

---

## What you are setting up

Three pieces, called **services** in Railway:

| Service | What it is | Public? |
|---|---|---|
| `db` | The database (PostgreSQL with PostGIS) | No — internal only |
| `api` | The website and its API | **Yes** — this is the address people visit |
| `etl` | A scheduled job that downloads fresh crime data daily | No |

All three live in one Railway **project**.

---

## Before you start

- A **GitHub account**, with this repository either owned by you or forked to
  your account.
- A **Railway account** — sign up at [railway.com](https://railway.com) using
  *Sign in with GitHub*. That connection is what lets Railway read the code.
- A payment method. Railway's Hobby plan is about **$5/month** plus usage;
  expect roughly **$10–15/month** total for this project. There is a trial
  credit for new accounts.

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
6. Still in **Settings**, find **Volumes** and click **Add Volume**. Set the
   mount path to:

   ```
   /var/lib/postgresql/data
   ```

   This is where the data actually lives. Without it, everything is erased on
   every restart.

7. Open the **Variables** tab and add these five:

   | Variable | Value |
   |---|---|
   | `POSTGRES_DB` | `safety` |
   | `POSTGRES_USER` | `safety` |
   | `POSTGRES_PASSWORD` | Click the variable menu and choose a generated secret |
   | `TZ` | `UTC` |
   | `PGTZ` | `UTC` |

   Let Railway generate the password. You never need to see or type it — the
   other services reference it by name in step 2.

8. Click **Deploy** and wait for the service to go green.

> Do **not** give this service a public domain. Nothing outside the project
> should be able to reach the database.

---

## Step 2 — Add the website service

1. Click **+ New** → **GitHub Repo**, and pick this repository. Authorise
   Railway to access it if prompted.
2. When the service appears, open **Settings** and rename it to `api`.
3. In **Settings → Deploy**, set these three fields:

   **Custom Start Command**

   ```
   uvicorn safety.api.main:app --host :: --port $PORT --proxy-headers --forwarded-allow-ips='*'
   ```

   Copy it exactly, quotes included.

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

4. Open the **Variables** tab and add:

   | Variable | Value |
   |---|---|
   | `POSTGRES_HOST` | `${{db.RAILWAY_PRIVATE_DOMAIN}}` |
   | `POSTGRES_PORT` | `5432` |
   | `POSTGRES_DB` | `${{db.POSTGRES_DB}}` |
   | `POSTGRES_USER` | `${{db.POSTGRES_USER}}` |
   | `POSTGRES_PASSWORD` | `${{db.POSTGRES_PASSWORD}}` |
   | `ENABLE_DOCS` | `true` |

   Type the `${{db....}}` values exactly as written, braces included. They are
   references — Railway fills in the real values, so no password is ever copied
   by hand. If you named the database service something other than `db`, use
   that name instead.

   Note `5432`, not the `55432` used locally. The local port is unusual only to
   avoid colliding with a developer's own PostgreSQL; inside Railway it is the
   standard port.

5. In **Settings → Networking**, click **Generate Domain**. Railway gives you an
   address like `something.up.railway.app`, with HTTPS already working. This is
   the address to share.
6. Click **Deploy**.

---

## Step 3 — Add the daily data job

1. Click **+ New** → **GitHub Repo** and pick the **same repository** again.
   Yes, twice — same code, different job.
2. Rename the service to `etl` in **Settings**.
3. In **Settings → Deploy**, set:

   **Custom Start Command**

   ```
   python -m safety.etl.run incremental --city phl
   ```

   **Cron Schedule**

   ```
   0 6 * * *
   ```

   That means 6:00 AM UTC daily. Philadelphia publishes daily, and the job is
   safe to run more often than data actually appears — "checked, nothing new" is
   a normal outcome.

   **Restart Policy** — set to **Never**. This job is meant to finish and exit;
   without this Railway may treat a completed run as a crash and start it again.

   Leave **Healthcheck Path** empty. This service does not serve web requests.

4. In **Settings → Volumes**, add a volume mounted at:

   ```
   /data/bronze
   ```

   This keeps a copy of exactly what the city published on each date, so data
   can be rebuilt later without re-downloading.

5. In **Variables**, add the same five database variables from step 2, plus:

   | Variable | Value |
   |---|---|
   | `BRONZE_ROOT` | `/data/bronze` |

6. Click **Deploy**.

---

## Step 4 — Load the data the first time

The scheduled job will not run until 6:00 AM UTC. To load the data now, open the
`etl` service and click **Deploy** (or **Redeploy**) to run it once immediately.

The first run downloads 24 months of Philadelphia crime data — about 320,000
records — and takes roughly **3 minutes**. It does this automatically: the job
notices there is no data yet and performs a full load instead of an incremental
one. There is no separate command to run.

Watch the **Logs** tab. A finished run prints a summary ending with a count of
records loaded.

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

## Keeping it running

**Updating the code.** Push to the `main` branch on GitHub. Railway rebuilds and
redeploys both the `api` and `etl` services automatically. Nothing else to do.

**Daily data.** Handled by the `etl` schedule. No action needed.

**Backups.** Railway does not back up volumes on every plan. If this data
matters, check your plan's backup options for the `db` service volume. The data
can always be rebuilt from the city's public source, but that takes a few
minutes and loses the record of past pulls.

**Cost.** Watch the project's **Usage** tab for the first week. The database
volume and the always-on `api` service are the main costs; the `etl` job runs
for about a minute a day.

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

---

## Notes for developers

`railway.json` at the repository root deliberately contains **build
configuration only**. Both the `api` and `etl` services deploy from this same
repository and therefore read the same file — a `healthcheckPath` there would be
applied to the cron service too, which never serves HTTP and would fail its
health check forever. Deploy settings are per-service in the UI for that reason.

Both services run the same image, built from `Dockerfile`, and differ only in
start command. `safety/config.py` reads all configuration from environment
variables, so no `.env` file exists or is needed in the container.
