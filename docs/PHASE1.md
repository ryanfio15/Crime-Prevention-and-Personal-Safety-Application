# Phase 1 — Philadelphia pipeline proof of concept

Implements roadmap Phase 1 from [`readme.md`](../readme.md) §14: the full
bronze → silver → gold pipeline validated end to end against one source, plus a
serving layer and a temporary front end so the result is visible.

Section references below (§n) point at the design document.

---

## Quick start

```bash
# 1. database  (Docker Desktop must be running)
docker compose up -d

# 2. python environment
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # POSIX: .venv/bin/python
cp .env.example .env

# 3. schema + reference data
.venv/Scripts/python.exe -m safety.migrate

# 4. load 24 months of Philadelphia  (~3 min: 25 monthly pulls, ~320k records)
.venv/Scripts/python.exe -m safety.etl.run backfill --city phl

# 5. serve
.venv/Scripts/python.exe -m uvicorn safety.api.main:app --port 8000
```

Then open **http://127.0.0.1:8000/**.

### Other pipeline commands

```bash
python -m safety.etl.run incremental --city phl       # pull since watermark, upsert
python -m safety.etl.run reprocess   --city phl --pull-id 2   # replay bronze, no refetch
python -m safety.etl.run gold        --city phl       # rebuild rollups only
python -m safety.etl.run status                       # registry, pulls, data quality
```

---

## What actually got built

| Design doc | Where it lives |
|---|---|
| §3.2 H3 res 8 + res 9 on every incident | `safety/h3grid.py`, computed in `etl/transform.py` at ingest |
| §3.3 relative activity, not a risk score | `gold.cell_activity.percentile` / `activity_tier`, `etl/gold.py` |
| §5 bronze / silver / gold | `data/bronze/` → `silver.incident` → `gold.*` |
| §6 standardized schema | `db/migrations/004_silver.sql` |
| §7 NIBRS crosswalk, versioned | `reference/crosswalk/philadelphia_v1.csv` → `reference.offense_crosswalk` |
| §8.1 per-source adapter | `etl/adapters/base.py` + `etl/adapters/philadelphia.py` |
| §8.3 idempotent incremental load | upsert on `incident_key`, `revision_lookback_days` re-read |
| §8.4 source registry | `reference.source_registry` (6 cities seeded, 5 disabled) |
| §8.5 validation before promotion | `etl/validate.py` → `etl.validation_issue` |
| §9.1 PostgreSQL + PostGIS, partitioned | `docker-compose.yml`, LIST(city) → RANGE(year) |
| §9.2 object storage for bronze | `etl/bronze.py`, local FS behind an S3-shaped interface |
| §9.3 precomputed rollups | `gold.cell_activity` / `cell_monthly` / `cell_offense_mix` |
| §9.4 serving layer | `safety/api/` — reads gold only |
| §9.5 cache invalidated per ETL refresh | in-process TTL cache in `api/main.py` (Redis stand-in) |
| §10 client-side cell resolution | `web/app.js` uses h3-js locally; `/cells/ring` is a key lookup |
| §12 attribution + "data as of" | `gold.city_snapshot`, header badge, methodology sheet |
| §13 responsible-design framing | `/api/v1/methodology`, "How to read this" sheet |

### Deliberately not built in Phase 1

- **Vector tiles** (§9.4). 551 res-8 / 3,600 res-9 hexagons ship as one GeoJSON
  document assembled by PostgreSQL; a tile server earns its keep at six cities,
  not one.
- **Redis** (§9.5). Same invalidation semantics, in process, one fewer container.
- **Airflow / Prefect / Dagster** (§8.4). The DAG shape
  (fetch → validate → transform → refresh) is `etl/run.py`; cadence already
  lives in the registry, so scheduling is a config lift, not a rewrite.
- **h3-pg extension.** H3 indexes are computed once in Python at ingest, which
  §6 requires anyway, so the database never needs to compute one.

---

## Numbers from the current load

| | |
|---|---|
| Source | `phl.carto.com` → `incidents_part1_part2`, Carto SQL API |
| Window | trailing 24 months |
| Fetched | 320,977 records across 25 monthly chunks |
| Rejected | 4,938 (1.5%) — missing/zeroed coordinates |
| Reprojected | 338 — published in EPSG:2272, converted (see below) |
| Loaded to silver | 316,024 across 3 year partitions |
| Unmapped offense codes | 0 of 30 distinct code/text pairs |
| Cell universe | 551 cells at res 8, 3,600 at res 9 |
| Gold rows | 83,020 activity · 192,296 monthly · 73,038 offense mix |
| Bronze snapshot | 32 files, 13 MB gzipped |

---

## Three things the source does that the design doc did not anticipate

**1. ~0.1% of coordinates are published in the wrong CRS.** 338 records carry
`point_x` / `point_y` in NAD83 / Pennsylvania South State Plane *feet*
(EPSG:2272) rather than WGS84 — the City's internal projection, apparently
never converted on export. They sit at real Philadelphia addresses (verified
against `location_block`), so the adapter reprojects them and tags
`coordinate_source = 'reprojected_epsg2272'`; discarding them would have been
the easy wrong answer. §8.5 anticipates *missing* and *zeroed* coordinates but
not *wrongly projected* ones — worth carrying into the other five adapters.

**2. `dc_key` is a numeric, not a string.** CSV renders it
`202601000996.00000000`. The integer part is the incident number. Handled in
`PhiladelphiaCartoAdapter.normalize`.

**3. The timestamp is a dispatch time, not an occurrence time.** PPD publishes
when police were dispatched. Carried explicitly as
`silver.incident.occurred_basis = 'dispatch'` and stated in the methodology,
rather than flattened into a generic "occurred at".

---

## Design decisions worth knowing

**Cell universe = boundary fill ∪ occupied cells.** `h3shape_to_cells` fills by
*center containment*, so 45 res-8 cells that incidents actually landed in sit
outside the fill (they straddle the city edge). Both sets are unioned:
without the occupied cells an edge incident would have nowhere to aggregate;
without the boundary fill a zero-incident cell would drop out of the
denominator and inflate every other cell's percentile.

**Tier 0 is not the bottom of the ramp.** "Nothing was reported here" and "this
is the quietest fifth of the city" are different claims, so they get different
colours and different labels.

**Percentile is over the whole city cell universe**, including zero cells —
that is what §3.3's "compared to other cells in the same city" means.

**Windows are anchored to the newest reported date, not to today.** Anchoring
to `now` would render a source's publication lag as an absence of crime.

**`product_category` follows the UCR violent/property split, not NIBRS
crime-against.** NIBRS files robbery under Crimes Against Property; a
personal-safety product cannot show robbery as "property". Both classifications
are stored, so the API can serve either.

---

## The front end

`web/` is a temporary, deliberately dependency-light client: MapLibre GL JS and
h3-js from CDN, no build step, served as static files by the same FastAPI
process.

- H3 hexagons over Philadelphia, coloured by incident count on a **quantile**
  scale — the distribution is heavily skewed (median 134, p95 844, max 3,600 at
  res 8 / 12 months), so a linear ramp would flatten the whole city.
- Toggle to the §2 five-tier relative scale.
- Filters: time window, offense category, cell size (res 8 / 9), colour mode.
- Click a hexagon for count, city rank, density, cell + neighbours (via the
  §10 k-ring endpoint), category breakdown, monthly trend, and the top offense
  types with their NIBRS codes.
- "Use my location" computes the H3 index **on the client** from GPS — §10's
  point that cell membership needs no server round trip.
- Table view: the colour scale is never the only channel.
- Polls `/api/v1/version` each minute and reloads when the ETL refreshes.

Colour follows a validated sequential ramp: one hue, monotone lightness,
selected for the dark surface rather than flipped from the light one.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/health` · `/version` | status, data-as-of, refresh stamp |
| `GET /api/v1/cities` · `/cities/{id}` | city metadata, coverage, attribution |
| `GET /api/v1/cells` | the hexagon layer as GeoJSON (`res`, `window`, `category`, `bbox`, `min_count`) |
| `GET /api/v1/cells/{h3}` | one cell: activity, category split, monthly series, offense mix |
| `GET /api/v1/cells/ring` | §10 cell + k-ring, indexed key lookup |
| `GET /api/v1/cells/lookup` | lat/lng → cell → rollup (the geocoded-address path) |
| `GET /api/v1/summary` | citywide totals per category |
| `GET /api/v1/categories` | product categories with their NIBRS detail |
| `GET /api/v1/quality` | §8.5 validation findings and recent pulls |
| `GET /api/v1/methodology` | §12 / §13 text |

Interactive docs at `/docs`.

---

## What Phase 2 needs

Per §11, onboarding a city should be config + one adapter. Concretely:

1. Flip `enabled = true` on the registry row (all five are already seeded with
   their API type, cadence, publication lag, and terms).
2. Write one adapter implementing `fetch_incidents` / `fetch_boundary` /
   `parse_incidents` / `normalize`, and register it in
   `safety/etl/adapters/__init__.py`.
3. Add `reference/crosswalk/<city>_v1.csv` and re-run `safety.migrate`.
4. Add a boundary source for that city.

Nothing in `etl/validate.py`, `etl/transform.py`, `etl/gold.py`, `safety/api/`,
or `web/` should need to change — that claim is what Phase 2 is testing.

Still open from §15: the geocoding provider for address search, and the
advertised staleness tolerance per city.
