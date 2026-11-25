# Deploying on a bare Linux server

Three containers: `db` (PostGIS), `api` (FastAPI + the static frontend), `valhalla`
(street routing). nginx runs on the host and terminates TLS.

Every command below was executed end to end on 2026-11-24 against a fresh clone
and a fresh database. Figures marked *(measured)* came from that run.

## 1. Requirements

Ubuntu 24.04 LTS (or any distro with Docker Engine 25+). Needs `sudo`.

| | |
|---|---|
| CPU / RAM | 2 vCPU, 4 GB minimum. Valhalla is capped at 1.5 GB and the database asks for 256 MB of shared buffers. |
| Disk | 25 GB. Image 322 MB *(measured)*; routing tiles 1.1 GB download, ~1.2 GB installed *(measured)*; per city roughly 230 MB of raw snapshots and 160 MB of database for a rolling year — Philadelphia at 38,076 incidents was 234 MB and 157 MB *(measured)*. Chicago and Los Angeles are several times that. |
| Network | Outbound HTTPS. The server pulls from six city open-data portals, `api.transitous.org`, `photon.komoot.io`, and GitHub for the tiles. |
| Ports | 80 and 443 inbound. Nothing else: the database, API and Valhalla publish on loopback only. |

Host packages — this exact sequence was run on a clean `ubuntu:24.04` *(measured)*:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git jq
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

`git` clones the repo, `curl` and `jq` are what the smoke check uses, `certbot`
issues the certificate. Everything else the application needs is inside the
image — no Python, no compiler, no PostgreSQL on the host.

## 2. Clone and configure

```bash
sudo mkdir -p /srv && cd /srv
sudo git clone https://github.com/ryanfio15/Crime-Prevention-and-Personal-Safety-Application.git safety
cd safety
sudo cp .env.example .env
sudo chmod 600 .env
```

Edit `.env`. One value is mandatory — the stack refuses to start without it:

```
POSTGRES_PASSWORD=<a long random string>
```

Generate one with `openssl rand -base64 36`. Worth setting at the same time:

- `SOCRATA_APP_TOKEN` — free, from https://evergreen.data.socrata.com/signup.
  Optional, but the first backfill of four cities is where the anonymous
  throttle starts to bite.
- `ROUTING_CONTACT` — a URL or mailbox, sent in the User-Agent to Transitous and
  Photon, as both ask.

Leave `POSTGRES_HOST` and `VALHALLA_URL` alone. `docker-compose.yml` overrides
both for the API container; the values in `.env` are for tools run outside it.

## 3. Build and start the database and API

```bash
sudo docker compose build api
sudo docker compose up -d db
sudo docker compose run --rm api python -m safety.bootstrap
```

`bootstrap` is the whole first load in one command: it applies the 24 migrations,
loads the crosswalks and severity weights, enables the six cities, then backfills
a rolling year per city, loads the census population denominator and builds the
gold rollups in dependency order. Re-running it is safe.

**It is slow.** Philadelphia alone, at one month of backfill, took about three
minutes *(measured)*. A full twelve-month load of all six cities is hours, most
of it waiting on the city portals. Run it under `screen`/`tmux`, or expect to
leave the SSH session open. Exit status is non-zero if any city was left
unfinished; just run it again.

```bash
sudo docker compose up -d api
```

## 4. Install the routing tiles and start Valhalla

The tiles are not built on this server — a build needs ~4 GB RAM and ~6.5 GB
disk. GitHub Actions (`.github/workflows/valhalla-tiles.yml`) builds them monthly
and publishes a release tagged `valhalla-tiles-YYYYMMDD`.

```bash
sudo deploy/valhalla/fetch.sh
```

That picks the newest release, downloads the three assets, verifies them against
`SHA256SUMS`, smoke-tests every city by car, foot and bike in a throwaway
container on port 58003, and only then swaps `/srv/valhalla/current` and
restarts the live service. A build that fails either smoke test is rejected and
nothing live changes.

Then start it:

```bash
sudo docker compose --profile routing up -d valhalla
```

Valhalla is behind a profile deliberately: a plain `docker compose up -d` never
starts it, and nothing about a deploy depends on routing. With it down, the map
and transit directions carry on and drive/walk/bike report themselves
unavailable.

To keep tiles current, run `deploy/valhalla/fetch.sh` weekly from cron:

```
17 4 * * 0 /srv/safety/deploy/valhalla/fetch.sh >> /var/log/valhalla-fetch.log 2>&1
```

## 5. nginx and TLS

```bash
sudo cp deploy/nginx/safety.conf /etc/nginx/sites-available/safety
sudo ln -sf /etc/nginx/sites-available/safety /etc/nginx/sites-enabled/safety
sudo rm -f /etc/nginx/sites-enabled/default      # else it wins on port 80
sudo sed -i 's/ryanfioserver\.ddns\.net/<your-domain>/g' /etc/nginx/sites-available/safety
```

The file as committed already carries the `listen 443 ssl` lines and certificate
paths certbot added on the original host. For a new domain, strip the
Certbot-managed lines and the second `server` block first, then let certbot add
its own:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d <your-domain>
```

certbot adds TLS and the 80→443 redirect in place. Run it *after* the file is
installed, not before.

What that config does beyond proxying: rate limits `/api/v1/cells` to 2 r/s
(burst 10) and everything else under `/api/v1/` to 10 r/s (burst 20), caps
concurrent connections per address at 20, and gzips JSON. The API has no
authentication, so these limits are the only thing between a stranger and a
repeated full-city layer build. Keep them.

## 6. Verify

```bash
sudo SMOKE_TIMEOUT=60 deploy/lib/smoke.sh 8000 -
```

Prints `smoke ok on :8000`. That checks `/api/v1/health` answers 200 (it reads
the database), `GET /` serves the UI, and `/api/v1/cities` answers 200.

For the full check, see the end-to-end section of `CLAUDE.md`, which walks every
endpoint with expected responses.

## 7. Keeping data fresh

Nothing above is scheduled. `safety.ops` works out what each city is missing and
runs only that, so one cron entry covers refresh:

```
23 */6 * * * cd /srv/safety && /usr/bin/docker compose run --rm api python -m safety.ops >> /var/log/safety-ops.log 2>&1
```

Six-hourly matches the ETL's own cadence; a city whose portal is down is retried
after `OPS_RETRY_COOLDOWN_HOURS` (6).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `POSTGRES_PASSWORD is not set` | `.env` missing or unset. It has no default on purpose. |
| API healthy, map empty | `bootstrap` has not finished. Check `/api/v1/progress`. |
| drive/walk/bike "unavailable" | Valhalla is down or has no tiles. `/api/v1/directions/status` says which. |
| `bind source path does not exist` on valhalla | Run `deploy/valhalla/fetch.sh` first; the mount is deliberately not auto-created. |
| 429s under light use | nginx rate limits are per client address. Behind another proxy, every client shares one bucket — see the `real_ip` note in `CLAUDE.md`. |
| Routes rejected as "outside" | Both endpoints must be inside one covered city's bbox. |
