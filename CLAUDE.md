# CLAUDE.md

## Rules

**Rule 0 — read the Git section first.** Every agent working on this repository
reads [section 6, "Git"](#6-git) before doing anything else. It is the running
record of what each commit changed and why, and it is the only place the
pruning decisions are written down — without it you will not know why a file
you expect to exist does not.

**Rule 0b — keep it current.** Whenever you commit or push, append the details
to the Git section in the same change: the commit subject, what moved, why, and
anything a later reader would otherwise have to rediscover. A commit that does
not update the Git section is incomplete. Newest entry at the top.

The rules below are the ones this document was originally built under, and the
order the work was done in.

1. **Clone the repository into a clean Linux environment.** Not the working tree —
   a fresh `git clone` from GitHub, so nothing uncommitted or locally cached can
   make the deploy look like it works.
2. **Download every resource, package and requirement needed to compose the
   containers.** Nothing hand-placed, nothing assumed already present.
3. **Document each required component, and how to install it, in `DEPLOY.md`.**
   `DEPLOY.md` is for a bare Linux server and stays short.
4. **Get the site running with all containers live** before documenting anything
   as working.
5. **Run the unit tests and record their context here** — which exist, which
   matter most, what the bulk of them cover.
6. **This file is the specification for pruning.** Anything in the repository not
   described here is to be removed. Nothing is archived; GitHub holds the
   history.

### What was actually executed, and what was not

Run on 2026-11-24. Every figure and status in this document came from that run.

- Fresh `git clone` of `origin/main` at `2d5c6d9` into a scratch directory,
  isolated under its own Compose project, ports, and volumes. The existing local
  dev stack was left untouched.
- `docker compose build api` → image built, 322 MB, no source compilation.
- 24 migrations applied to an empty PostGIS; 1,224 crosswalk rows, 2 severity
  schemes, 66 weights loaded.
- `python -m safety.bootstrap --cities phl` with a one-month backfill → 38,076
  Philadelphia incidents pulled live from `phl.carto.com`, census denominator and
  all gold rollups built. Exit 0.
- All three containers reported healthy at once; `deploy/lib/smoke.sh` passed.
- Every HTTP endpoint exercised (section 3), including live geocoding via Photon,
  live transit via Transitous, and drive/walk/bike via Valhalla on the real
  1.1 GB tile release.
- 471 Python tests and 75 JS tests passed.
- The `DEPLOY.md` apt sequence was executed in a clean `ubuntu:24.04` container.

**Deviations, stated plainly.** These are the limits of the verification, not
things the document hides:

- **Not a real VM.** The clean environment was a container and a fresh clone on a
  Windows host running Docker Desktop with a Linux engine, not a provisioned
  Linux server. Package installation and the clone-to-running sequence are
  genuinely tested; kernel, systemd, UFW/iptables and certbot behaviour on a real
  host are not.
- **nginx and TLS were not executed.** Section 5 of `DEPLOY.md` is transcribed
  from the config's own install header and the live host's working file. It is
  the one part of the deploy nobody has re-run end to end here.
- **One city, one month.** Not the full six-city, twelve-month load. The pipeline
  is proven; the hours and the disk figures for all six are extrapolations and
  are marked as such.
- **Harness-only changes** in the scratch clone: `container_name` lines dropped
  and published ports moved, because the live dev stack already held
  `safety_db`, 55432 and 8000. Neither change belongs in the deliverable.

### One change this required

`docker-compose.yml` defined only `db` and `valhalla`. There was no `api`
service — the API ran either from the systemd release tree or from a hand-started
container. "Clone, compose, site is live" therefore did not exist. An `api`
service was written and verified: builds from the local `Dockerfile`, overrides
`POSTGRES_HOST=db` and `VALHALLA_URL=http://valhalla:8002` for in-network
addressing, mounts a named volume at `/data/bronze`, publishes
`127.0.0.1:8000`, waits on the database's healthcheck, joins both the default
and `routing` networks, and has its own healthcheck against `/api/v1/health`.
This is an addition, not a prune.

---

## 1. What the service is

A crime-density map of six US cities with safety-aware directions. One rolling
year of incident data per city, aggregated into H3 hexagons at three resolutions,
scored against a published severity scale and a census population denominator,
served as GeoJSON to a MapLibre frontend. Directions come in four modes, each
annotated with the activity level of the cells the route passes through.

Cities: `phl` Philadelphia, `chi` Chicago, `sea` Seattle, `lax` Los Angeles,
`dc` Washington DC, `aus` Austin.

Three containers:

| Service | Image | Role |
|---|---|---|
| `db` | `postgis/postgis:17-3.5` | Everything persistent. Published on `127.0.0.1:55432`. |
| `api` | built from `./Dockerfile` | FastAPI + the static frontend, one process. Published on `127.0.0.1:8000`. Also runs the ETL with a different command. |
| `valhalla` | `ghcr.io/valhalla/valhalla:3.9.1` (digest-pinned) | Street routing. Published on `127.0.0.1:58002`, behind the `routing` profile. |

nginx on the host terminates TLS and proxies to the API. Nothing but nginx is
reachable from outside.

## 2. Necessary components

### 2.1 Application package — `safety/` (52 files, all required)

**Entry points.** `safety/api/main.py` is the ASGI app: nine endpoints, the
response cache, the connection pool, and it mounts `web/` at `/`.
`safety/migrate.py` applies `db/migrations/` under an advisory lock with
checksums. `safety/etl/run.py` is the ETL CLI (`backfill`, `incremental`,
`enable`, `gold`, `log`, `weights`). `safety/ops.py` is the convergence planner —
it works out what a city is missing and runs only that, which is why one cron
entry suffices. `safety/bootstrap.py` chains migrate → enable → converge, and is
the documented first load.

**Support.** `config.py` (every setting, read from `.env` via pydantic-settings),
`db.py` (connections, partitions, `wait_for_db`), `h3grid.py` (cell geometry and
lookup), `joblock.py` (the cluster-wide ETL advisory lock), `ranking_sql.py`,
`schemawait.py`, `shards.py`.

**`safety/api/`.** `repository.py` all SQL for the serving layer;
`ratelimit.py` the in-process per-client limiter; `security.py` the security
headers; `directions.py` the four routing endpoints and their request
validation.

**`safety/etl/`.** Six adapters (`austin`, `chicago`, `los_angeles`,
`philadelphia`, `seattle`, `washington_dc`) over a shared `socrata.py` and
`base.py`; `bronze.py` raw snapshots, `transform.py` → silver,
`gold.py` the rollups, `census.py` the TIGER population denominator,
`boundary.py`, `location.py`, `windows.py`, `retention.py` (the rolling year),
`withdrawn.py` (records retracted upstream), `validate.py`, `pending.py`,
`progress.py`.

**`safety/routing/`.** `service.py` orchestration, `providers.py` the three
provider clients, `scoring.py` route activity scoring, `detours.py` safer-route
search, `breaker.py` circuit breaker, `budget.py` + `ttlcache.py` the daily
allowances and caching, `cities.py` point-in-city, `polyline.py`, `schema.py`,
`errors.py`.

### 2.2 Schema — `db/migrations/` (24 files, all required)

`001`–`024`, applied in order. **Immutable**: each is checksummed on apply and
`safety/migrate.py` fails if a file's content changes after the fact. They are
not to be renumbered, squashed, edited or deleted, ever. Schemas:
`reference` (registry, crosswalks, severity), `etl` (ledger, pulls, withdrawn),
`silver` (normalised incidents, partitioned by year), `gold` (the rollups the
API reads), `public` (`schema_migration`, `provider_budget`).

### 2.3 Reference data — `reference/` (8 files, all required)

Six crosswalk CSVs mapping each city's local offence codes to the common
taxonomy, plus `severity/schemes.csv` and `severity/weights_nscs_v1.csv`.
Loaded by `safety/migrate.py` on every run — 1,224 crosswalk rows, 2 schemes,
66 weights *(measured)*. Without these the ranking has no severity scale.

### 2.4 Frontend — `web/` (16 files, all required)

`index.html`, `app.js` (the map, 159 KB served), `directions.js`, `styles.css`,
`theme.js`, `hours.js`, `html.js`, `progress.js`, `sw.js` + `sw-register.js`
(service worker), and five Nunito `.woff2` weights under `fonts/` with their
OFL licence. Served by the API itself, not a separate container. All eleven
assets verified 200 *(measured)*.

**`web/vendor/` — vendored third-party libraries (3 files, all required).**
`maplibre-gl.js` (954,516 B), `maplibre-gl.css` (69,430 B), `h3-js.umd.js`
(188,853 B) — maplibre-gl 5.9.0 and h3-js 4.2.1, the exact published builds.
These used to load from `unpkg.com` with subresource integrity, which protected
the bytes but not availability: an unpkg outage, or a client with no route to
it, left the page with no map. They are served from this origin now, they are in
the service worker's cached shell, and the CSP allows no third-party script or
style origin at all. `ci.yml` re-checks their SHA-384 hashes on every run and
fails if `index.html` mentions unpkg again.

The one remaining third-party origin is `basemaps.cartocdn.com`, which serves
the basemap raster tiles. That is imagery, not code; it cannot be vendored, and
the CSP still allows it under `img-src` and `connect-src`. With it unreachable
the hexagons and all data still draw — only the map backdrop is missing.

Hashes, for bumping a version (`openssl dgst -sha384 -binary <f> | openssl base64 -A`):

```
maplibre-gl.css  sha384-gNYNsUmuZqDYiT3gbirWTV5K7rt71RoveS/yXAaU09d4ZUmeDVTD3XoqB6uJAIFR
maplibre-gl.js   sha384-81ppUWZZO+pveTPk4qU9cZuScEFULHfuOMHVJHFZ7xVBlWQTG1zsPrVIMGAl1WMb
h3-js.umd.js     sha384-ViAXEmpUtfR6LTFA4VPFElyF4q6ysOu7yi4fs2v5udUWy6l162CdHpDx1SyS870v
```

### 2.5 Container and deploy files

Required: `Dockerfile`, `docker-entrypoint.sh` (chowns `BRONZE_ROOT`, then drops
root via `setpriv`), `docker-compose.yml`, `.dockerignore`, `.env.example`,
`requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `LICENSE`.

Surviving from `deploy/`:

- `deploy/nginx/safety.conf` — the production front end: rate-limit zones, gzip,
  proxy headers, the two-tier limiter.
- `deploy/nginx/safety-dev.conf` — the second instance on the same host. Keep
  only if a dev instance is still wanted; otherwise prune.
- `deploy/valhalla/fetch.sh`, `smoke.sh`, `build.sh`, `regions.tsv` — tile
  install, tile smoke test, the Actions-side build, and the city list. `build.sh`
  runs in CI, not on the server, but the workflow needs it.
- `deploy/lib/smoke.sh` — the end-to-end HTTP check. Section 3 and `DEPLOY.md`
  both depend on it.

Surviving from `.github/workflows/`:

- `ci.yml` — the test gate. Its `shellcheck` and `bash -n` steps enumerate
  `deploy/k8s/*.sh` and `deploy/railway/*.sh` and must be trimmed to the
  surviving scripts, or CI goes red on the first push after pruning.
- `valhalla-tiles.yml` — builds and publishes the monthly tile release.
  `DEPLOY.md` section 4 has no source of tiles without it.

### 2.6 Configuration

`safety/config.py` is the single source of truth and `.env.example` documents
every key. `POSTGRES_PASSWORD` is the only value with no default — by design, so
a well-known password cannot reach a deployment. Settings that matter most:
`BACKFILL_MONTHS`, `RETENTION_DAYS`, `ENABLE_RATE_LIMIT`, `DIRECTIONS_ENABLED`,
`SOCRATA_APP_TOKEN`, `ROUTING_CONTACT`, `TRANSIT_DAILY_BUDGET`,
`GEOCODE_DAILY_BUDGET`, `BOOTSTRAP_CITIES`.

## 3. End-to-end verification over HTTP

Against the running stack. Replace `127.0.0.1:8000` with the public origin to
check through nginx and TLS. Every status and size below was observed on the
verification run *(measured)*.

### 3.1 The one-command check

```bash
SMOKE_TIMEOUT=60 deploy/lib/smoke.sh 8000 -
```

→ `smoke ok on :8000`. Health answers 200, `GET /` serves the UI,
`/api/v1/cities` answers 200. The third argument is the expected commit; `-`
skips that check.

### 3.2 Read endpoints

```bash
B=http://127.0.0.1:8000
curl -s $B/api/v1/health
curl -s $B/api/v1/cities
curl -s $B/api/v1/quality
curl -s $B/api/v1/progress
curl -s $B/api/v1/methodology
curl -s $B/api/v1/version
```

| Endpoint | Expect | Observed |
|---|---|---|
| `/api/v1/health` | 200, `status: ok`, non-zero `incidents`. Reads the database — a 503 here means the pool or schema is wrong. | 221 B, 38,076 incidents |
| `/api/v1/cities` | 200, one object per enabled city with bbox, centre, coverage dates, `cell_count_r8/r9`. Drives the city picker. | 1,927 B |
| `/api/v1/quality` | 200, per-city provenance and freshness. | 1,524 B |
| `/api/v1/progress` | 200, backfill progress. The right place to look while `bootstrap` runs. | 175 B |
| `/api/v1/methodology` | 200, the scoring explanation the UI links to. | 12,081 B |
| `/api/v1/version` | 200, pipeline version. Polled once a minute per open tab. | 123 B |

### 3.3 The map layer — the expensive one

```bash
curl -s -o /dev/null -w '%{http_code} %{size_download} %{time_total}\n' \
  "$B/api/v1/cells?city=phl&resolution=8"
curl -s "$B/api/v1/cells?city=phl&resolution=8" | jq '.features[0].properties'
```

200, 429,261 B in 0.06 s for Philadelphia at resolution 8 *(measured)*. A
GeoJSON `FeatureCollection`; each feature is a hexagon with `h3`, `rank`,
`tier`, `count`, `per_km2`. Resolutions 8, 9 and 10 are valid — 10 is the
largest and slowest. Cached gzip-compressed and served without
recompression.

Also: `/api/v1/cells/{h3_index}` for one cell, and `/api/v1/cities/{source_id}`.

### 3.4 Frontend

```bash
for p in / /app.js /styles.css /directions.js /sw.js /fonts/nunito-latin-700-normal.woff2; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "$B$p"
done
curl -s -o /dev/null -w '%{http_code}\n' $B/docs     # Swagger, if ENABLE_DOCS
```

All 200. `GET /` was 27,175 B. In a browser the map should draw hexagons, the
city picker list six cities, and the Directions button be present (it hides
itself when `DIRECTIONS_ENABLED` is off).

### 3.5 Directions and geocoding

```bash
curl -s $B/api/v1/directions/status | jq .modes

curl -s -X POST $B/api/v1/geocode -H 'Content-Type: application/json' \
  -d '{"q":"City Hall, Philadelphia","city":"phl"}'

curl -s -X POST $B/api/v1/directions -H 'Content-Type: application/json' \
  -d '{"from":{"lat":39.9480,"lng":-75.1800},"to":{"lat":39.9650,"lng":-75.1600},"mode":"walk"}'
```

`/directions/status` must show all four modes `available: true`. Observed with
all three containers up *(measured)*:

| Mode | Provider | Result |
|---|---|---|
| `drive` | Valhalla | 200, 3 routes, 325 s / 4,594 m, 16 steps, 0.40 s |
| `walk` | Valhalla | 200, 3 routes, 2,416 s / 3,390 m, 26 steps, 0.07 s |
| `bike` | Valhalla | 200, 3 routes, 787 s / 3,588 m, 8 steps, 0.04 s |
| `transit` | Transitous | 200, 5 routes, 1,560 s / 3,957 m, 1 transfer, 0.93 s |

Geocoding returned real Photon results for City Hall. Every route carries an
`activity` block — that is the safety annotation, and its absence means the
gold layer is not loaded.

**Request shape matters** and is easy to get wrong by hand: the body uses
`from`/`to` (not `origin`/`destination`), geocode takes `q` (not `query`), and
both models are `extra="forbid"` — an unexpected key is a 422, not an ignored
field.

### 3.6 Correct failures

These are part of a healthy system, not faults:

| Request | Expect |
|---|---|
| Either endpoint outside a covered city bbox | 422 `{"code":"outside"}` |
| Any Valhalla mode with `valhalla` down | 503 `{"code":"provider_unavailable"}`; transit and the map unaffected |
| `/api/v1/cells` faster than 2 r/s through nginx | 429 after burst 10 |
| Extra or misnamed body field | 422 |
| Pool saturated or query past `API_STATEMENT_TIMEOUT_MS` | 503 with `Retry-After` |

Confirmed on the run: both the `outside` 422 and the `provider_unavailable` 503
behaved exactly as above.

## 4. Unit tests

**Keep the whole suite.** 31 Python test files, 471 tests collected (413 without
a database, 58 marked `db`), plus 6 JS files with 75 tests. All green
*(measured)*. They cost nothing at runtime and are the only safety net for the
pruning.

### 4.1 Running them

```bash
python -m pytest -q -m "not db"                      # 413, no database
SAFETY_TEST_DB=1 python -m pytest -q -m db           # 58, needs live PostGIS
node --test tests/js/*.test.mjs                      # 75, needs Node 22+
```

`requirements-dev.txt` adds only `pytest`; the runtime image deliberately
excludes it. Inside the container the entrypoint drops to the unprivileged
`safety` account, so running tests there needs `--user root --entrypoint sh`.

**One trap, met on the verification run.** `tests/test_joblock.py` takes the real
ETL advisory lock. If an ETL, `safety.ops` or `bootstrap` is running, two tests
fail with `assert 75 == 3` — a lock-contention artifact, not a defect. Both
passed once the ETL finished. Do not run the `db` tests concurrently with an
ETL.

### 4.2 The ones that matter most

Ranked by what breaks if they go red.

1. **`test_gold_invariants.py`** (31 tests, `db`) — the gold layer against real
   PostGIS on a synthetic city. This is the product's correctness: if the
   rollups are wrong, the map is confidently wrong and nothing else notices.
2. **`test_adapters.py`** (27) — characterisation tests for all six adapters'
   `normalize()`, against captured fixtures in `tests/fixtures/adapters/`. Catches
   a city silently changing its feed shape.
3. **`test_cells_endpoint.py`** (14) — `/api/v1/cells`: cached gzip bytes served
   without recompression, bbox and resolution handling. The hottest, most
   expensive endpoint.
4. **`test_routing_providers.py`** (41) — the three provider clients against
   answers captured from the live services (`tests/fixtures/routing/`). The
   largest single file, and the providers are outside our control.
5. **`test_directions_endpoint.py`** (17) + **`test_geocode_endpoint.py`** (24) —
   the routing endpoints end to end with providers stubbed, including the
   validation that produced the 422s in section 3.5.
6. **`test_migrate.py`** (5) — migration checksums and the advisory lock. The
   guard that makes applied migrations immutable.
7. **`test_retention.py`** (11) — the rolling year: cutoff arithmetic, silver
   deletion, bronze cleanup. Wrong here means unbounded growth or silently
   dropped data.
8. **`test_ratelimit.py`** (6) + **`test_security_headers.py`** (9) — the only
   protections on an unauthenticated public API. `test_ratelimit` specifically
   covers keying on the *proxy-resolved* client address, which is what makes the
   nginx `real_ip` configuration matter.
9. **`test_api_errors.py`** (5) — saturation and runaway queries answer 503 with
   `Retry-After` rather than hanging every endpoint including health.
10. **`test_withdrawn.py`** (20) + **`test_withdrawn_db.py`** (8) — records
    retracted upstream: window, outage guard, mode, deletion and recovery.

### 4.3 What the rest cover

- **Correctness of the pipeline:** `test_windows.py` (13) reporting and fetch
  windows; `test_ops_plan.py` (13) what convergence decides to run;
  `test_schedule.py` cadence arithmetic; `test_progress.py` (14) the backfill
  progress bar; `test_h3grid.py` cell geometry; `test_lax_crosswalk.py` (4)
  LAPD's legacy codes; `test_constants_parity.py` (6) that the serving layer's
  duplicated ETL constants have not drifted.
- **Serving layer:** `test_cache.py` (8) thread-safety, single-flight, byte
  accuracy; `test_provenance_cache.py` (3) refresh-stamped `/quality`;
  `test_route_queries_db.py` (4, `db`) the routing SQL.
- **Routing internals:** `test_routing_scoring.py` (20) activity scoring on
  synthetic cells; `test_routing_cache_budget.py` (14) TTL/LRU/single-flight,
  daily budget, circuit breaker; `test_routing_polyline.py` (7) polyline
  precisions; `test_shared_budget.py` (7) one allowance across replicas.
- **Operational machinery:** `test_bootstrap.py` (5); `test_joblock.py` (4,
  `db`); `test_schemawait.py` (3); `test_shards.py` (20) the multi-database
  split.
- **JS (75):** `directions.test.mjs`, `hours.test.mjs`, `html.test.mjs`
  (escaping), `progress.test.mjs`, `theme.test.mjs`, `breakpoint.test.mjs` — the
  pure frontend logic, no DOM or network.

The pattern throughout: pure logic is tested directly, anything crossing the
network is tested against captured fixtures, and anything whose correctness is a
SQL property is tested against a real PostGIS behind the `db` marker. Code
comments and test docstrings cite requirement IDs (`F4`–`F20`) and design
sections (`S6`, `S13`) from `docs/DESIGN.md`, which is kept for exactly that
reason (section 5).

## 5. What was removed, and what was kept

Done — 102 files deleted. Nothing was archived; `origin/main` before this commit
holds all of it, and the Git section below records the commit to go back to.

**Removed from `deploy/` — 80 of 86 files.** All systemd units
(`deploy/systemd/`, 14) and the release-tree machinery they drove: `deploy.sh`,
`autodeploy.sh`, `promote.sh`, `install-deployer.sh`, `logs.sh`,
`migrate-layout.sh`, `setup-worktrees.sh`, `db-isolate.sh`, `os-isolate.sh`,
`deploy/lib/{install,release,backup,notify}.sh`,
`deploy/db/transfer-ownership.sql`, `deploy/git-hooks/` (3). All of
`deploy/k8s/` (24), `deploy/railway/` (9), `deploy/fabric/` (2). Also
`deploy/nginx/safety-dev.conf` — the second instance on the old host, which the
container deploy has no equivalent of.

**Kept from `deploy/` — 6 files**, listed in section 2.5:
`nginx/safety.conf`, `valhalla/{fetch,smoke,build}.sh`, `valhalla/regions.tsv`,
`lib/smoke.sh`. All four scripts were checked for dependencies on removed files
first; each is self-contained.

**Removed from `docs/` — 15 of 16 files.** `ARCHITECTURE.md`, `DEPLOY.md`,
`DEPLOYMENT.md`, `KUBERNETES.md`, `OPERATIONS.md`, `PHASE1.md`, `PHASE2.md`,
`RAILWAY.md`, `ui5/UI5_SPEC.md`, and `ui5/fonts/` (6 files, byte-identical
duplicates of `web/fonts/`).

**`docs/DESIGN.md` is kept** — your decision, and the right one. It is the
requirements specification the `F4`–`F20` and `S6`/`S13` identifiers throughout
the code comments and ~30 test docstrings refer to. One file, no runtime cost,
and without it a large amount of surviving commentary stops meaning anything.

**Removed `scripts/` — all 5.** `build_austin_crosswalk.py`,
`build_chicago_crosswalk.py`, `build_nibrs_crosswalk.py` were one-time
generators for the crosswalk CSVs, which are committed under `reference/`;
verified not imported by any runtime module. `fetch_valhalla_tiles.py` was the
container-host tile path, superseded by `deploy/valhalla/fetch.sh`.
`storage.py` was a disk-reclaim investigation tool.

**Removed at the root.** `.railwayignore`, and `.github/workflows/image.yml`
(built the ghcr image for Kubernetes and Railway, and asserted the existence of
files that are now gone).

**Known consequence, unchanged.** Surviving code comments still cite the removed
docs: 6 references to `docs/PHASE2.md`, 5 to the old `docs/DEPLOY.md`, 4 to
`docs/KUBERNETES.md`, 3 to `docs/OPERATIONS.md`, 2 to `docs/DEPLOYMENT.md`
*(measured)*. Those citations now point at nothing. The comments themselves are
substantial and worth keeping, so they were left alone rather than
bulk-rewritten; `docs/DESIGN.md` references are still live. Rewriting the dead
citations is a separate pass across `safety/` and has not been done.

`readme.md` was left in place untouched, to be rewritten separately.

## 6. Git

The running record. **Newest first.** Append an entry whenever you commit or
push (Rule 0b). An entry says what changed, why, and what a later reader would
otherwise have to rediscover.

### Prune to the container deploy, and vendor the CDN libraries

- **Branch:** `prune-for-delivery`, cut from `main` at `051bf37`.
- **Scope:** 102 files deleted, 9 modified, 6 added. The repository goes from 279
  tracked files to 177 plus `web/vendor/` and the two new documents.
- **Verified:** the pruned tree was built and run from a clean copy before
  committing — image builds, 24 migrations apply to an empty PostGIS,
  `smoke.sh` passes, the three vendored assets serve 200 from this origin, the
  CSP carries no third-party script origin, and **546 tests pass** (413 non-db,
  58 db, 75 JS). The full data and routing path (38,076 live Philadelphia
  incidents, all four direction modes through Valhalla and Transitous) was
  verified earlier on identical application code; the prune touched no file
  under `safety/etl/` or `safety/routing/`.

**Added**

- `CLAUDE.md`, `DEPLOY.md` — this document and the bare-server deploy guide.
- `web/vendor/{maplibre-gl.js,maplibre-gl.css,h3-js.umd.js}` — see below.

**The unpkg dependency is gone.** `web/index.html` loaded maplibre-gl 5.9.0 and
h3-js 4.2.1 from `unpkg.com`, pinned with subresource integrity. SRI protects
the *bytes*, not *availability*: an unpkg outage, or a client network with no
route to it, left the page with no map at all. The three files are vendored
under `web/vendor/` and served from this origin. On vendoring, each download's
SHA-384 was checked against the `integrity` attribute it was replacing and all
three matched exactly, so the vendored copies are provably the same builds that
were being loaded. Consequences, all in this commit:

- `web/index.html` — local `vendor/` paths; `integrity`/`crossorigin` dropped
  (meaningless same-origin); the `<head>` comment rewritten to explain the
  change and how to bump a version.
- `safety/api/security.py` — `script-src` and `style-src` are now `'self'` with
  no `https://unpkg.com`. **No third-party script origin is permitted at all.**
- `tests/test_security_headers.py` — asserted the old
  `script-src 'self' https://unpkg.com` string; now asserts `script-src 'self'`
  and that `unpkg.com` appears nowhere in the policy.
- `web/sw.js` — the three files joined the cached shell, so the map now also
  survives going offline after one load; `CACHE_NAME` bumped `v5` → `v6` to
  retire the old shell. Its "deliberately not cached" note was rewritten: that
  list existed *because* the libraries were cross-origin.
- `web/sw-register.js` — stale "'self' and unpkg" comment corrected.
- `.github/workflows/ci.yml` — new step re-checks all three SHA-384 hashes and
  fails if `index.html` mentions unpkg again.

`basemaps.cartocdn.com` remains and cannot be vendored — it is basemap imagery,
not code. With it unreachable the hexagons and all data still draw.

**`docker-compose.yml` gained the `api` service.** It defined only `db` and
`valhalla`, so there was no way to clone, compose and have the site running —
the API came from the systemd release tree. The new service builds from the
local `Dockerfile`, overrides `POSTGRES_HOST=db` and
`VALHALLA_URL=http://valhalla:8002` (the `.env` values name host-published
loopback ports that do not exist inside the container), mounts the named volume
`safety_bronze` at `/data/bronze`, publishes `127.0.0.1:8000`, gates on the
database healthcheck, joins both the default and `routing` networks, and
healthchecks `/api/v1/health`.

**`.github/workflows/ci.yml` was trimmed, not just edited.** It referenced
pruned files and would have gone red on the first push. Removed: the
byte-compile of `deploy/railway`; the `bash -n`/`shellcheck` sweeps over
`deploy/{,lib/,k8s/,railway/,git-hooks/}`; the superuser-to-owner role dance
(`deploy/db/transfer-ownership.sql`, the base-schema-from-`main` check, the P2
superuser-migration self-test), which modelled the systemd host where migrations
ran as a non-superuser owner role — the container deploy connects as one role;
and the previous-commit-on-the-migrated-schema check, which existed for the old
in-place rollback. Kept and retargeted: compile, imports, both pytest passes,
the JS tests, migrate twice for idempotency, partition creation, the ETL log
query, and `smoke.sh`. The job id and name stay `ci` — branch protection refers
to them.

**Traps recorded while doing this work**

- `tests/test_joblock.py` takes the **real** ETL advisory lock. Run it while an
  ETL, `safety.ops` or `bootstrap` is running and two tests fail with
  `assert 75 == 3`. That is lock contention, not a defect (section 4.1).
- A local uncommitted fix in `deploy/railway/up.sh` (exporting
  `MSYS_NO_PATHCONV=1` so Git Bash on Windows stops rewriting container mount
  paths) was discarded with the rest of `deploy/railway/`. Recorded here in case
  the Railway path is ever revived.
- `main` was one commit behind `origin/main` (`2d5c6d9`, "Add close buttons to
  the Near me and Cities panels") when this work started. The branch was cut
  from the local `051bf37`, so **that commit is not an ancestor of this branch**
  — merge or rebase onto `origin/main` before delivery.
