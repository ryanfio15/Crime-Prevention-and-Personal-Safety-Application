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
   ```

   Then click **Save** / **Update Variables**.

   The `${{db....}}` entries are references, not literal text — Railway
   substitutes the real values at deploy time, so no password is ever copied by
   hand. **They only work if the database service is named exactly `db`.** If
   you named it something else, replace `db` with that name throughout.

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

4. Attach a volume, the same way as in step 1 — from the **project canvas**,
   right-click the `etl` card → **Attach Volume** (not the Settings tab). Mount
   path:

   ```
   /data/bronze
   ```

   This keeps a copy of exactly what the city published on each date, so data
   can be rebuilt later without re-downloading.

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
   ```

   Then click **Save** / **Update Variables**.

   Same database block as the website, with `BRONZE_ROOT` added and
   `ENABLE_DOCS` left off — this service serves no web pages. If you skipped the
   volume in step 4, use `BRONZE_ROOT=/tmp/bronze` instead.

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
