# Phase 2 — Philadelphia to six cities

Implements roadmap Phase 2 from [`readme.md`](../readme.md) §14: the remaining
five adapters and crosswalks, built on Phase 1's pipeline as the template, and
exercising the three API paradigms and the cadence differences to confirm the
source-registry/adapter split actually isolates city-specific quirks as §11
claims.

Section references below (§n) point at the design document.

---

## Where each city stands

| City | Adapter | Crosswalk | Boundary | Enabled |
|---|---|---|---|---|
| Philadelphia | ✅ Carto SQL | ✅ 30 rows, hand-written | ✅ police districts | ✅ |
| Chicago | ✅ Socrata | ✅ generated, reviewed (16 corrections) | TIGER PLACE | ✅ |
| Washington DC | ✅ Esri MapServer | ✅ 9 rows, hand-written | TIGER PLACE | ✅ |
| Seattle | ✅ Socrata | ✅ NIBRS code-only | TIGER PLACE | ✅ |
| Los Angeles | ✅ Socrata | ✅ NIBRS code-only | TIGER PLACE | ✅ |
| Austin | ❌ no point locations published | — | TIGER PLACE | ❌ |

A city is enabled only once its adapter, crosswalk and boundary have all landed
*and* its first backfill has been read against the checks below. Enabling is a
one-line `UPDATE`, deliberately manual: it is the point at which a city's
numbers start being shown to people.

---

## §11 was mostly right

The claim under test was that onboarding a city is one adapter, one crosswalk and
one registry row, with no change to `etl/validate.py`, `etl/transform.py`,
`etl/gold.py`, `safety/api/` or `web/`.

**Held up.** The adapter contract, the validator, silver promotion, the gold
rollups, partitioning and the census denominator are all genuinely city-agnostic
and needed no changes to accept a second city. Percentiles are within-city by
construction (§3.3), so there is no cross-city normalization anywhere to get
wrong.

**Did not hold up.** Seven things had leaked out of Philadelphia's adapter, or
were single-city assumptions nobody had had reason to notice:

1. **The county filter is not the city.** `census.py` narrowed census blocks to
   the registry's `county_fips` and then summed them as "this city". Only
   Philadelphia is coterminous with its county — Chicago sits inside a Cook
   County with roughly twice its population, Austin spans three counties and
   fills none of them. Every citywide population figure, including the
   `ambient_population` the methodology page publishes, would have been too large
   by a factor of two or more. Blocks are now trimmed to the ones intersecting
   the coverage boundary (`census._trim_to_boundary`), and blocks that merely
   straddle the edge are kept, since the areal apportionment already credits a
   cell with only the overlapping share.
2. **The clock-hour backfill measured every city at once.** Its verdict rests on
   a *share* of rows (`_HOUR_ALIGNMENT_FLOOR = 0.99`), pooled across
   `silver.incident` with no `source_id` filter. One source storing a genuine UTC
   instant would have dragged that share below the floor and refused the backfill
   for every city, logging a diagnosis true of none of them. Now per source.
3. **The methodology endpoint stated Philadelphia's facts as the product's.**
   "Philadelphia publishes police dispatch times" over Seattle's recorded
   occurrence times is a plain factual error about the data on screen. The
   timestamp caveat is now assembled from `silver.incident.occurred_basis` — what
   the records actually carry — plus two new registry columns,
   `occurrence_basis_note` and `denominator_examples_note`.
4. **The cache invalidated globally.** `serving_version` took `max()` across
   every city, so refreshing Los Angeles threw away Philadelphia's cached layers.
   Now per city, and the whole-cache-clear eviction is an LRU, because one pass
   over a large city's resolution-10 layer would otherwise evict every small
   city on its way through.
5. **The backfill window lived in the Philadelphia adapter** and ignored
   `backfill_start_date`, which is the knob that stops Seattle crossing its May
   2019 RMS break and Los Angeles crossing the March 2024 dataset freeze (§4,
   §7.3). Moved to `safety/etl/windows.py` and now honours the floor.
6. **The crosswalk required an exact code *and* text match.** Right for
   Philadelphia's 30 stable labels; brittle for Chicago's ~400 IUCR codes whose
   descriptions drift, where a rewording upstream would send real incidents to
   `unmapped`. A second match tier takes `raw_offense_text_key = '*'` as "any
   description under this code", consulted only when no exact row matches.
7. **The cell universe was written a row at a time.** `executemany` over ~25,000
   resolution-10 cells and ~150,000 adjacency pairs is survivable for
   Philadelphia and roughly four times that for Los Angeles. Both are now `COPY`.

None of the seven changes what Philadelphia shows. The regression gate below is
what proves it rather than asserting it.

---

## Boundaries: one loader, not five

Philadelphia's coverage polygon is `ST_Union(police_districts)` from its own
Carto endpoint — the police-jurisdiction boundary §3.1 explicitly allows. That
does not generalize: the other five publish boundaries across three API
paradigms, in three formats, none of them the paradigm their incident data uses.

So the five new cities take theirs from TIGER/Line PLACE
(`safety/etl/boundary.py`): one archive format, on the same Census Bureau host
`census.py` already reads, at the same vintage as the population denominator — a
boundary and a block layer from different years disagree at the edges, and the
retention check would report that disagreement as a bug. The place is resolved by
name plus incorporation status, an ambiguous match raises rather than guessing,
and the resolved `PLACEFP` is written back to the registry so the match is
auditable afterwards.

An adapter opts in by returning `None` from `fetch_boundary`; `ensure_boundary`
falls through to the shared loader. Philadelphia keeps its polygon and is
untouched.

---

## Running it

```bash
docker compose up -d
.venv/Scripts/python.exe -m safety.migrate            # applies 011, loads reference data

# Per city, in this order.
.venv/Scripts/python.exe -m safety.etl.run backfill --city chi
.venv/Scripts/python.exe -m safety.etl.run census   --city chi
.venv/Scripts/python.exe -m safety.etl.run gold     --city chi
.venv/Scripts/python.exe -m safety.etl.run weights  --city chi
.venv/Scripts/python.exe -m safety.etl.run status
```

`backfill` fetches the boundary first if it is missing, and `census` does too —
the block trim needs it.

### Across every enabled city

Three flags make one scheduled job cover six sources on five cadences. The
deployed configuration is the first line below every few hours plus the second
weekly — see [`DEPLOY.md`](DEPLOY.md) for the Railway service layout.

```bash
python -m safety.etl.run incremental --all --due-only --skip-hourly
python -m safety.etl.run hourly --all
```

- **`--all`** runs every enabled source, stalest first, and does not let one
  city's outage stop the other five. Exits non-zero if any failed, so a scheduler
  notices — a silent partial failure across six sources is worse than a noisy one.
- **`--due-only`** skips sources whose registry cadence says they cannot have new
  data yet, so the schedule only decides how often to *ask*. This is what keeps
  §8.2's cadence in the registry instead of smeared across six cron expressions.
  It keys on when a pull last *ran*, not when one last succeeded:
  `last_success_at` does not move on a `no_new_data` outcome, which is the normal
  result for a bi-weekly source, so keying on success would leave Los Angeles
  permanently overdue and re-checked every tick. Exits 0 when nobody is due.
- **`--skip-hourly`** leaves the time-of-day layers out of the frequent run. They
  are the most expensive thing the pipeline builds — the same ranking recomputed
  24 times, at two resolutions and two windows, per scheme — and the
  slowest-moving, since both their windows are a year or wider. The cost of
  skipping is real and worth knowing: between weekly rebuilds the hourly view
  reflects the last build while the all-hours percentile it is compared against is
  current, so "change from usual" is briefly derived from two different windows.

The cadence arithmetic is a pure function, `run.is_due(cadence,
days_since_check)`, so it can be exercised without a database and returns the
reason alongside the decision — a schedule that quietly skips a city is
indistinguishable from one that is broken.

### A Socrata app token

Optional, and worth setting for four Socrata cities: `SOCRATA_APP_TOKEN` in
`.env` moves requests off the shared anonymous per-IP quota. Not a credential —
it identifies the caller for rate-limiting only. Everything works without it; a
24-month backfill across four cities is where the anonymous throttle bites.

---

## Verification

### The regression gate: Philadelphia must not move

`python scripts/storage.py baseline` then `... gate` runs everything below and
interprets the result, including the row-count drop the storage reclaim makes on
purpose. The SQL is kept here because the gate is the argument, not the script.

Run **before** touching anything, to capture a baseline:

```sql
CREATE TABLE IF NOT EXISTS public.phl_baseline AS
SELECT h3_index, h3_res, time_window, track, scheme_version, safety_percentile
FROM gold.cell_safety WHERE source_id = 'phl';
```

Then after `safety.migrate` and a `gold --city phl` rebuild:

```sql
SELECT count(*)                                              AS cells,
       corr(b.safety_percentile, s.safety_percentile)        AS correlation,
       max(abs(b.safety_percentile - s.safety_percentile))   AS max_shift
FROM public.phl_baseline b
JOIN gold.cell_safety s USING (h3_index, h3_res, time_window, track, scheme_version);
```

**`correlation` must be exactly 1.0 and `max_shift` exactly 0.** Anything else
means one of the seven Stage-0 changes reached Philadelphia, and the two that
could are §0.1 (the crosswalk's new fallback tier) and §0.8 (the new NIBRS weight
rows). Both are provably inert for `phl`: it has an exact crosswalk row for all 30
code/text pairs it publishes, so tier 2 is never consulted, and it has a
`raw_offense_text_key` weight for all 30, which outranks `nibrs_code` in
`_WEIGHT_LOOKUP`. The gate is what turns "provably" into "proved".

### Per new city, in order

1. **`status`** — `records_rejected / records_fetched` in the low single percents.
   Philadelphia's baseline is 1.5% (missing and zeroed coordinates). Double
   digits is an adapter bug, not source noise.
2. **Unmapped offenses** — `etl.validation_issue` `unmapped_offense_code` should
   be zero. Non-zero means crosswalk rows are missing. The records are already
   loaded and flagged rather than dropped (§8.5), so fix the CSV, re-run
   `safety.migrate`, and `reprocess` from the stored bronze snapshot — no refetch.
3. **Census retention** — `build_cell_exposure` warns below 95% of block
   population retained. A warning means `county_fips` or the boundary is wrong.
   Check the boundary trim's own line first: it reports how many blocks it
   dropped, and a suspiciously round number there is usually a county-list
   problem.
4. **`weights`** — the share of incidents carrying a published severity weight,
   and which offenses are on a derived fallback. Below 0.95 the ranking is closer
   to the coarse UCR bucket than to the severity scale. Expect a non-empty list
   for a NIBRS city: the scale is a 1977 survey of 204 criminal events and NIBRS
   has more offense codes than that. Adding `nibrs_code` rows is the fix;
   inventing a number and marking it `sourced = true` is not.
5. **Hour coverage** — `hour_known_share` on the city snapshot, and
   `log_hour_shift`'s line. A non-zero modal shift means the adapter is
   mislabelling a UTC instant as local wall clock, which corrupts
   `occurred_year` and every window boundary — not just the hourly layer.
6. **Shrinkage** — the realised own-figure weight per resolution against
   Philadelphia's 68% / 24%. `eb_prior_persons = 2000` is calibrated to
   Philadelphia's ~4,180 ambient per resolution-8 cell, so a sparse city shrinks
   harder. If it is far off, add a `schemes.csv` row and point that city at it
   with `severity_scheme_version` — the registry already supports a per-city
   scheme, so this needs no code.
7. **The map** — `http://127.0.0.1:8000/`, switch cities, and confirm the frame,
   legend domain, detail panel, hourly view and methodology sheet all follow the
   selection, with no Philadelphia string surviving on another city's page.

### The keyword bucketer

```bash
python -m safety.etl.location     # 33 cases, all from real published values
```

Location-type bucketing is the §7.5 low-confidence problem — keyword matching,
no versioned crosswalk. Two things substring matching got wrong until the cases
caught them, both worth knowing before editing the keyword lists: "PARKING LOT /
GARAGE (NON RESIDENTIAL)" matched `residential` and "VEHICLE NON-COMMERCIAL"
matched `commercial`, so negated phrases are stripped first; and "STATION" made
"GAS STATION" transit while " BUS" made "SMALL BUSINESS" transit, so broad tokens
are out in favour of specific compounds.

---

## Chicago

`safety/etl/adapters/chicago.py` — the Socrata template the three remaining
Socrata cities follow.

- **Chunking is monthly**, like Philadelphia's, even though Socrata does offer
  `$offset`. Deep offsets degrade, and a snapshot partitioned by calendar month
  is directly comparable against the previous one, which is what makes the §8.5
  volume-anomaly check mean anything. A month at or above the 50,000-row `$limit`
  raises rather than truncating — a short month would look exactly like a quiet
  one and would feed every percentile in the city.
- **Timestamps are Socrata floating** (no offset, Chicago local wall clock) and
  are read as such. `occurred_basis` is `occurrence`, not `dispatch` — a real
  difference from Philadelphia, and the reason that column exists.
- **`reported_at` is deliberately left NULL.** Chicago publishes `updated_on`,
  which is when the *record* was last modified in the RMS, sometimes a bulk
  re-publication. §6 wants `reported_at` to be when the offence was reported, for
  lag analysis. Filling it with the wrong quantity would make every lag figure
  derived from it meaningless. `updated_on` is still fetched and lands in bronze
  verbatim, so that analysis stays possible from the snapshot.
- **`case_number` is not the key.** It is the department's RD number and one case
  can produce several offence rows; `id` is the stable per-record identifier.
- **`location_description` is populated**, unlike Philadelphia's dataset, so this
  is the first city that fills `location_type` at all.
- **Incrementals filter on `date`, not `updated_on`.** A record revised today but
  occurring two years ago is not re-read by an incremental pull, only by a
  backfill. `revision_lookback_days` covers the window revisions actually cluster
  in, and bronze is what makes a deeper reprocess cheap (§5, §8.3).

### The crosswalk is generated, and needs review

```bash
python scripts/build_chicago_crosswalk.py > reference/crosswalk/chicago_v1.csv
```

Philadelphia's 30 rows are hand-written, which is right for 30 rows. Chicago's
IUCR list is an order of magnitude longer, and hand-typing it would put
transcription errors into the one table that decides what every incident in the
city is classified as. So the codes and descriptions come from Chicago's own
published IUCR dataset and are never retyped; the NIBRS mapping is a rule-based
first pass.

**Every row lands `approximate` or `ambiguous` and is meant to be read before it
is trusted.** Review in this order: anything still `ambiguous` (no rule claimed
it), then everything mapped to `violent`, then `quality_of_life` — §13 singles
out over-enforced offence types and that is where they land.

The rules read **both** halves of IUCR's description, and that is not a detail.
IUCR files aggravated battery under primary description "BATTERY" with the
aggravation in the secondary, so a rule reading only the primary maps it to
simple assault (13B) instead of aggravated assault (13A). The severity scale
scores those 6.17 and 8.50, and 17.76 for the firearm variant, so the error would
flow straight into the ranking. Same for IUCR's theft subtypes — pocket-picking,
retail theft, theft from building all live in the secondary description.

### The review, done

Every row was read in that order. Sixteen were wrong, and are corrected by code in
`_OVERRIDES` in the generator rather than in the CSV, so a regeneration cannot
undo them:

- **Rule order.** "AGGRAVATED RITUAL MUTILATION - OTHER DANGEROUS WEAPON" matched
  `WEAPON` before `RITUAL` and landed in weapon-law violations; "AGG. RITUAL
  MUTILATION" is spelled with a full stop the `AGGRAVATED` rule does not match.
  Both are 13A. "INTIMIDATION / EXTORTION" was intimidation (13C); it is 210.
- **`SEX OFFENSE` is a catch-all primary.** Adultery, fornication, bigamy,
  marrying a bigamist, criminal transmission of HIV and non-consensual image
  dissemination were all fondling (11D) on the violent track; they are 90Z /
  `other`. Solicitation of a sexual act is a prostitution offence (40A), and
  public indecency joins IUCR's own `PUBLIC INDECENCY` bucket.
- **Under-classified.** Armed violence (an Illinois felony committed while
  armed) was disorderly conduct; it is 13A. Child abuse was a nonviolent family
  offence; it is 13B. A telephone threat had no rule; it is 13C. Looting was
  disorderly conduct; it is theft (23H).
- **Over-classified.** Unlawful visitation interference was kidnapping; it is a
  custody dispute (90F).

The 40 codes no rule claimed were read and their residual 90Z confirmed: court
orders, registration, licensing and administrative violations with no Group A
target. They are listed in `_CONFIRMED_RESIDUAL` so they are no longer flagged.

**First backfill, measured** (24 months to 2026-09-27): 495,311 fetched, 0.67%
rejected (all `missing_coordinates`), zero unmapped codes, census retention
99.8% at resolution 8 and 99.3% at 9, every incident with a clock hour and a
zero modal shift. Published-weight coverage is **70.6%**, under the 0.95 bar.
The gap is vandalism (290), retail theft and theft from building (23C, 23D),
trespass (90J), intimidation (13C) and the 90Z residual — offences the 1977
survey has no vignette for. They rank on the documented derived weights; the fix
is a sourced weight row, not an invented one.

---

## Fitting six cities on one volume

`DEPLOY.md` sizes the database at 20 GB and is right about why: the gold layer
scales with *cells*, not with incidents, and six cities are roughly ten times
Philadelphia's area. On a volume smaller than that — a 5 GB plan, say — the
question is which of those cells anything actually asks about.

The obvious lever is to drop resolution 10, and measurement says it is a bigger
lever than it looks: **69% of a Philadelphia-only database is resolution-10 rows.**
The cell universe is 551 / 3,600 / 24,770 cells at resolutions 8 / 9 / 10, so
resolution 10 is 86% of every table keyed by cell.

It is still the wrong one to reach for *first*, for two reasons. It is the only one
of these that costs the product a feature. And it does not touch the table
`DEPLOY.md` names as dominant: `HOURLY_RESOLUTIONS` is `(8, 9)`, so
`gold.cell_hour_safety` has never held a resolution-10 row, and dropping resolution
10 would not save a byte of it.

So: four things first that are invisible to what the product shows, then resolution
10 narrowed rather than dropped — which recovers most of its cost while keeping the
drill-down. After all five, resolution 10 still accounts for over half the database,
and dropping it outright remains available and remains the largest single lever.

Migration `012_storage_reclaim.sql` carries the one-time deletes; the pipeline
changes that stop the rows coming back are listed beside each.

**1. One severity scheme, not two.** `scheme_version` is in the primary key of
both `gold.cell_safety` and `gold.cell_hour_safety`, so a second enabled scheme
is a second complete copy of the ranking *and* of the 24-bucket hourly layer.
`nscs_v1` was kept enabled after `nscs_v2_percapita` superseded it so the two
stayed diffable with `safety-compare` — a comparison that had already been made.
It is now `enabled = false` in `schemes.csv`, and `safety.migrate` grows a
`prune_disabled_schemes` pass that reclaims the rows a disabled scheme left
behind, since no refresh ever revisits them. The scheme row and its weight table
stay: `nscs_v2_percapita` inherits its weights from there, and `safety --scheme
nscs_v1` still builds it on demand when there is a reason to compare again.

One side effect is a fix. `point_sources_at_scheme` only fills a city's NULL
scheme pointer when exactly one scheme is enabled, so with two shipping enabled
it always declined, and a fresh deploy that skipped `--activate` served no safety
layer at all. That is the failure `DEPLOY.md` documented under
"the ranking is empty". With one enabled scheme it resolves itself.

**2. The resolution-10 ranking was built and never served.** `Scheme.resolutions`
returned all three resolutions for an area-denominated scheme, on the correct
reasoning that area is exact at any cell size. But
`repository.SAFETY_RESOLUTIONS` is `(8, 9)` and `_require_safety_res` refuses a
resolution-10 ranking whatever produced it — the API will not serve two rankings
on two different denominators under one name. That was ~1.9M rows per city that
nothing could read. `gold.SAFETY_RESOLUTIONS` now caps every scheme at `(8, 9)`,
and a scheme parameter can no longer widen what the pipeline stores past what the
API serves.

This one also buys back time, and a lot of it. The comment on `neighbor_mean`
records that the ring-1 blend at resolution 10 runs over 49,540 rows and *had* cost
two minutes per window before it was rewritten as a grouped join — "most of a
Philadelphia gold refresh". Four windows times two tracks of that work is now
simply not done.

**3. Census block polygons are a build-time input.** `reference.census_block`
holds a MultiPolygon per 2020 tabulation block under a GiST index — 17,554 of them
for Philadelphia — and the serving layer never reads the table. Only the boundary
trim and the areal apportionment do, and both write their results into
`gold.cell_exposure` and `gold.city_snapshot`. So `geom` becomes nullable and there
is a new command:

```bash
python -m safety.etl.run release-geometry --city chi
```

It refuses unless every cell at the exposure resolutions already has a figure,
empties the column, and `VACUUM FULL`s the table — cheap here, because the
rewritten table no longer has the column that made it large. Restoring is a TIGER
re-download (`census --city chi`), which is the cost being accepted.

**This is the smallest of the four, by a long way.** Philadelphia's polygons are
4.3 MB with a 1.8 MB GiST index, against a 17 MB table and a 1,251 MB database.
TIGER block geometry is far simpler than a first guess suggests — most blocks are
convex and few-sided. The reason to do it is that the exposure work below is worth
having on its own, not the 6 MB.

Two things fall out of making this safe. `build_cell_exposure` is now
**incremental**: it apportions only cells with no exposure row, because
`build_cell_universe` never deletes a cell and H3 geometry is fixed, so an
existing figure cannot go stale on the cell's side. That removes the most
expensive query in the pipeline from every gold refresh — a PostGIS intersection
per (cell, block) pair, recomputing a decennial figure that had not moved — and
`census` itself passes `rebuild=True`, which is the case where it genuinely has.
And when the universe *does* grow after a release (an incident landing in a cell
no previous pull reached), the build raises rather than writing zeros: a zeroed
denominator reads on the map as "nobody lives here", not as a missing input. The
gold refresh logs that as an error and carries on, so one stranded edge cell does
not take the other five cities' rollups down with it.

**4. An unused GiST index on 1.6M points.** `004` declared `incident_geom_gix`
alongside the H3 indexes. The H3 ones carry every rollup; the point geometry has
never been queried, because S9.3's first rule is that reads touch gold only and
every `ST_*` call in the ETL is against `census_block.geom` or
`city_boundary.geom`. Dropped; the column stays, since it is part of the S6
canonical schema and cheap next to its index. Worth confirming against
`pg_stat_user_indexes.idx_scan` on an established database before trusting the
reasoning — though note that on the database these figures come from *every* index
reports zero scans, including the primary key, so the statistics had been reset and
say nothing either way. The code reading is the actual evidence.

**5. Resolution-10 ring-1 adjacency, the same defect as 2.** `gold.cell_neighbor`
has exactly two readers, `_SAFETY_SQL` and `_HOUR_SAFETY_SQL`, and both are the
safety ranking — which item 2 just capped at `(8, 9)`. So six pairs per
resolution-10 cell were being `COPY`ed in on every refresh and never joined
against: 146,628 of Philadelphia's 170,558 rows, ~90 MB, and roughly four times
that for Los Angeles. `build_cell_universe` now clears that resolution instead of
building it, and clears rather than skips, so widening `SAFETY_RESOLUTIONS` later
refills it with no migration.

### Then resolution 10, narrowed rather than dropped

`gold.cell_activity` is dense on purpose — one row per cell per window per
category, because S3.3 ranks a cell against the whole city and a cell with no
reported incidents is part of that distribution. Twenty rows per cell, and at
resolution 10 that made it the largest table here: ~230,000 res-10 cells across
six cities is ~4.6M rows, nearly all `incident_count = 0`.

`ACTIVITY_WINDOWS` / `ACTIVITY_CATEGORIES` narrow resolution 10 to the two widest
windows and category `all` — 2 rows per cell instead of 20, so ~90% of the cost
of the drill-down without losing the drill-down. The statistical case is the one
that already kept the hourly layer off resolution 10: a ~0.015 km² cell over 30
days, split five ways, is a field of ties, and a percentile over ties is not a
reading. The map's default view is unaffected at every resolution.

What it costs is two UI states, both of which had to be built rather than left to
look like data:

- The window and category controls disable what is not built at the selected cell
  size and say why, the same way the hour and colour-by controls already did
  (`syncActivityScope`). An out-of-scope request answers 400 with the reason.
- The detail panel's category chart would otherwise have rendered "No incidents
  reported in this cell" for a cell that has plenty — `by_category` filters out
  the `all` row, so at resolution 10 it is empty. `cell_detail` now publishes
  `by_category_available`, and the panel points at the per-offence list, which is
  built at every resolution and is the finer answer to the same question.

### Measured, on Philadelphia

One city, 347 km², 317,822 incidents, **1,251 MB**. The hourly layer had never been
built in this database, so `gold.cell_hour_safety` is absent from these figures —
read everything below knowing that the table `DEPLOY.md` expects to dominate is
not in it.

| Table | Size | Rows | of which res 10 |
|---|---|---|---|
| `gold.cell_activity` | 342 MB | 578,420 | 495,400 (86%) |
| `silver.incident` (3 partitions) | 479 MB | 317,822 | — |
| `gold.cell_safety` | 154 MB | 264,576 | 198,160 (75%) |
| `gold.cell_monthly` | 131 MB | 529,445 | 335,519 (63%) |
| `gold.cell_offense_mix` | 71 MB | 250,074 | 177,158 (71%) |
| `gold.cell_neighbor` | 42 MB | 170,558 | 146,628 (86%) |
| `gold.cell_geometry` | 19 MB | 28,921 | 24,770 (86%) |
| `reference.census_block` | 17 MB | 17,554 | — |

What the four changes actually free, at ~615 bytes per gold row — but see the
caveat under the table, because that figure is not the row width:

| Change | Rows removed | Freed |
|---|---|---|
| Narrow res-10 `cell_activity` | 445,860 | **~263 MB** |
| Disable `nscs_v1` | 231,368 | **~135 MB** |
| Drop res-10 `cell_neighbor` | 146,628 | **~90 MB** |
| Drop `incident_geom_gix` | — | **~26 MB** |
| Release `census_block.geom` | — | **~6 MB** |

**~520 MB of 1,251, or 42%, for one city** — and the four cell-scaled items grow
with area, so the proportion roughly holds as cities are added. `cell_safety` goes
from 264,576 rows to 33,208; `cell_activity` from 578,420 to 132,560;
`cell_neighbor` from 170,558 to 23,930.

**That 615 bytes is size-on-disk per row, not row width, and the difference is
mostly dead space.** Compacting the same database showed `gold.cell_activity`
falling from 342 MB to 156 MB at an *identical* 578,420 rows — over half of it was
bloat, because every gold refresh is delete-then-insert. Compacted, the real rates
are ~283 B/row for `cell_activity`, ~377 for `cell_safety`, ~129 for
`cell_neighbor`.

Both numbers are true of different questions. Rows × 615 B approximates the *file
space* those rows were occupying, bloat included, which is what a volume runs out
of — so the table above is a fair guide to what a delete plus a compaction returns.
Rows × 283 B is what the surviving data will actually weigh once compacted. Use the
first to predict a reclaim, the second to predict a steady state, and do not mix
them: extrapolating capacity from the first over-provisions by about a factor of
two.

Note the overlap: every res-10 `cell_safety` row was an `nscs_v1` row, because
`nscs_v2_percapita` never built at res 10. On *this* database the migration's res-10
delete is therefore subsumed by the prune. It is not redundant in general — it is
what cleans up if an area scheme is ever re-enabled, and `SAFETY_RESOLUTIONS` is
what stops the rows coming back at all.

The exposure change pays off separately, in time rather than space: a Philadelphia
gold refresh with nothing pending went from **2.65s to 0.03s** on that step, and
that gap widens with city size.

#### The regression gate, applied to this change

The same standard the Stage-0 changes were held to. Captured `gold.cell_activity`,
re-ran `refresh_cell_activity`, and compared every row that exists in both:

| res | rows compared | max percentile shift | rank / tier / count differences |
|---|---|---|---|
| 8 | 11,020 | **0.0** | **0** |
| 9 | 72,000 | **0.0** | **0** |
| 10 | 49,540 | **0.0** | **0** |

Exactly the 18 out-of-scope res-10 (window, category) combinations disappear and
nothing else moves — including the res-10 rows that are kept, which is what proves
the category filter sitting ahead of the window functions cannot shift the
partitions that remain.

### How to get the real ones on your own database

`scripts/storage.py` is the toolkit for all of this — a script rather than SQL to
paste, because `psql` is not on the PATH in the usual dev environment and the
regression gate is a multi-statement comparison:

```bash
python scripts/storage.py sizes      # what is actually big (read-only)
python scripts/storage.py baseline   # capture, BEFORE safety.migrate
python scripts/storage.py gate       # prove Philadelphia did not move
python scripts/storage.py compact    # hand freed pages back to the filesystem
```

`gate` is the §0 regression gate above, and it knows the row count is *supposed*
to fall — it reports the intentionally-dropped rows separately from the ones it
compares, and fails only on a percentile that moved.

`compact` exists because a migration cannot `VACUUM`: it runs in a transaction, so
nothing reclaims 012's deletes until something does it explicitly. Plain `VACUUM`
would be enough if the space were only wanted back by the same tables — it is not,
the point is a new city's partitions — so it is `VACUUM FULL`, which locks each
table and needs free disk for a second copy of the largest one. Read `sizes` before
running it.

Read `n_dead_tup` as carefully as the size (and note `n_live_tup` is an estimate,
so it can exceed a real `count(*)`). Every gold refresh is
delete-then-insert inside a transaction, which leaves dead tuples equal to a full
layer each time; on a multi-GB `cell_hour_safety` rebuilt weekly, autovacuum may
not keep up, and steady-state disk can sit near twice the logical size. That is
worth checking before concluding the data does not fit — and after these deletes,
plain `VACUUM` makes the space reusable by the same tables, which is where it
goes, while only `VACUUM FULL` hands it back to the filesystem for a new city's
partitions to use.

### Further levers, unused

Measured against the *post-change* Philadelphia database (~730 MB), in order of
size. Resolution 10 is still ~380 MB of it — 52% — so the first two are the same
lever at different depths:

- **`gold.cell_monthly` + `gold.cell_offense_mix` at resolution 10 — ~133 MB.** The
  two biggest remaining res-10 tables, and bigger than items 3, 4 and 5 above
  combined. Both are sparse (rows only where the count is positive), so they scale
  with incidents rather than cells, but 24 months of monthly buckets per res-10
  cell is still 512,677 rows for one city. Costs the res-10 detail panel its
  sparkline and its offence list — and item 4's narrowing just made that list the
  answer to the category question, so this is more expensive than it looks. Do not
  take this one without taking the next.
- **Drop resolution 10 entirely — ~380 MB, 52%.** Costs the ~75 m drill-down, which
  is a real feature and the finest thing the product offers. But it is by far the
  largest single number here, and if 5 GB will not hold six cities any other way,
  this is the honest place to give something up rather than shaving statistics.
- **Bronze off the database volume.** A different volume, so this only helps a
  combined budget: `BRONZE_ROOT=/tmp/bronze` costs `reprocess --pull-id` and
  nothing the website reads.

## The hourly layer, measured with two cities on it

Everything above was measured on a database where `gold.cell_hour_safety` had never
been built. On Railway, with Philadelphia and Chicago loaded and the hourly job
having run, it is not a footnote — it is the largest table there is:

| Table | Size |
|---|---|
| `gold.cell_hour_safety` | **1,042 MB** |
| `gold.cell_activity` | 525 MB |
| `gold.cell_monthly` | 337 MB |
| `gold.cell_safety` | 206 MB |
| `gold.cell_hour_profile` | 197 MB |
| *(+ silver partitions, geometry, census)* | |
| **total** | **3,316 MB** |

31% of the volume, for two of six cities, and it is the only layer multiplied by
24 — cells × windows × 24 hours × 2 tracks. Los Angeles alone is about four times
Philadelphia's cell count. It does not reach six cities on 5 GB.

So `HOURLY_WINDOWS` is now `("last_12m",)`, which halves that table and
`cell_hour_profile` with it — about **620 MB** at two cities, and it scales.
Migration `013_hourly_one_window.sql` clears what the old scope built.

**This is the first change here that costs a real feature.** Everything in 012 was
either unreadable or statistically empty; the 24-month time-of-day view was
neither. `last_24m` is the more redundant of the pair — at a year wide the hourly
distribution is already stable and the second year largely restates it — but that
is an argument for which one to drop, not that dropping one is free. It notably
does *not* transfer to the all-hours layers, where 24 months is the widest evidence
the product has.

Two things make it cheap to undo or to operate:

- **No rebuild is needed to apply it.** The surviving `last_12m` rows were not
  derived from the window being dropped: each window is ranked independently, and
  `baseline_percentile` is denormalized from `cell_safety`'s matching window. A
  deploy plus `storage.py compact` is the whole operation — which matters, because
  rebuilding the hourly layer is the most expensive job in the pipeline.
- **No migration is needed to reverse it.** Both `refresh_cell_hour_safety` and
  `refresh_cell_hour_profile` now `DELETE` across every window before skipping the
  ones out of scope, the same shape as `refresh_cell_activity`. Put `last_24m` back
  in the tuple, run `hourly --all`, and the rows return.

### Still not enough for six cities

After 012 and 013, two cities sit near 2.2 GB. That extrapolates to roughly 6–7 GB
for six, and Los Angeles is the one that breaks it. The remaining levers, in order,
are `cell_monthly` + `cell_offense_mix` at resolution 10, then resolution 10
outright. Four of the five remaining cities have no adapter yet, so there is time —
but the volume is the constraint that decides how many cities this deployment can
hold, not the adapters.

## Shared pieces for the NIBRS cities

`safety/etl/adapters/socrata.py` is the Chicago adapter's fetch, retry, monthly
chunking, truncation guard and floating-timestamp parsing lifted out unchanged;
Chicago, Seattle and Los Angeles each subclass it and keep only their column
list, occurrence field and `normalize`.

Seattle and Los Angeles publish the NIBRS offence code on every record, so their
crosswalks are code-only (`*` text) and `exact`, generated by
`scripts/build_nibrs_crosswalk.py` from one table of NIBRS codes. The product
category and severity bucket for each code are decided once there, following the
calls Philadelphia and Chicago already made — robbery violent, weapon-law
`other`, enforcement-driven offences `quality_of_life` — so the two cities cannot
drift apart.

**Weight coverage is the open item for every NIBRS-keyed city.** The busiest
offences without a published NSCS vignette are the same everywhere: vandalism
(290), shoplifting and theft from building (23C, 23D), identity theft (26F),
intimidation (13C) and the 90Z residual. Philadelphia avoids this only because
its 30 offence texts carry their own `raw_offense_text_key` weights. Measured
published-weight coverage: Chicago 70.6%, Seattle 63.7%, Los Angeles 62.7%. The
fix is sourced weight rows keyed on `nibrs_code`, not invented ones.

## Seattle

`safety/etl/adapters/seattle.py`, dataset `tazs-3rd5`.

- `offense_date` is the offence start time (`occurred_basis = occurrence`);
  `report_date_time` fills `reported_at`. Both Socrata floating.
- `offense_id` is the key; `report_number` repeats across a report's offences.
- **SPD withholds the location of about one record in six**, publishing the
  literal `REDACTED` in latitude, longitude and block — mostly offences where a
  location would identify a victim. They are rejected as `missing_coordinates`
  (labelled `withheld_by_source`), so **Seattle's rejection rate is ~16% by
  publication policy, not adapter error**, and the map under-counts the offence
  types SPD protects. There is no projected pair to fall back on.
- Code `999`, "Not Reportable to NIBRS" (found property, death investigations),
  is ~8% of SPD's records and is **not promoted**: every silver incident carries a
  severity weight above zero, so loading them ranked cells on non-crimes. They
  stay in bronze. Code `500`, SPD's no-contact-order extension, maps to 90Z.
- No premise field, so `location_type` is `unknown` throughout.
- Midnight is heaped (~14% of records at hour 0 in a sample week) — SPD's
  unknown-time value. Not corrected; it is visible in the hourly view.

**First backfill, measured** (24 months): 164,220 fetched, 16.4% rejected
(25,396 withheld coordinates, 1,514 outside the city boundary), 72 records with
no offence code (flagged, filed `other`), census retention 100.0% / 99.6%, every
incident with a clock hour.

## Los Angeles

`safety/etl/adapters/los_angeles.py`. The seeded `nibrs-offenses` was a
placeholder; the real dataset is **`k7nn-b2ep`, LAPD NIBRS Offenses**, fixed in
`015_onboard_sources.sql`. The Victims dataset (`gqf2-vm2j`) is a different grain
and is not read.

- `date_occ` is a floating timestamp that is always midnight; the time is in
  `time_occ` as `HHMM`, and the hour comes from there. Midnight and noon are
  mildly heaped (~2.7% and ~2.3%).
- `uniquenibrno` is the key; `caseno` repeats per offence.
- Coordinates are rounded to the hundred block (`hndrdth_lat/lon`); `(0,0)` is
  missing. `premis_desc` ("244 - TOBACCO SHOP") has its code stripped before
  keyword bucketing, so LA is the second city with real `location_type`.
- The NIBRS series ramps up through early 2025: October–December 2024 carry
  roughly 60–75% of a typical month. `backfill_start_date` stays 2024-10-01, so
  the 24-month window slightly under-weights its oldest quarter.
- About 19,000 rows a month, well inside the 50,000 `$limit`.

**First backfill, measured**: 416,660 fetched, 0.16% rejected, 121 records
(0.03%) with no NIBRS code — traffic infractions, property reports and legacy
LAPD codes — flagged and filed `other`. Census retention 99.7% / 98.8%; every
incident with a clock hour and a zero modal shift. 1,959 resolution-8 cells.

## Washington DC

`safety/etl/adapters/washington_dc.py` — the Esri paradigm, against
`FEEDS/MPD/MapServer`.

- **One layer per year, ids out of order** (2026 is layer 41, 2025 is 7). Layers
  are discovered by the name "Crime Incidents - <year>" on every pull, so a new
  January layer needs no code change. "Last 30 Days" duplicates the current-year
  layer and is not read; DC is therefore a `daily` source, not `rolling`.
- **Paging**: `resultOffset` / `resultRecordCount=1000` ordered by OBJECTID while
  `exceededTransferLimit` is set. Each layer's last page is short, which trips the
  `volume_anomaly` warning ("chunk far below the pull median") once per layer —
  expected, not a gap.
- **Filter on `REPORT_DAT`**, the field the layers are partitioned by, widened a
  day each side so a report filed near midnight on 31 December is not lost
  between two layers. Upserts make the overlap free.
- **Timestamps are epoch milliseconds in true UTC** — checked against `SHIFT`,
  which agrees with the New York clock for 89% of records and the raw UTC clock
  for 59%. Converted to America/New_York wall clock before storage: the
  opposite of the Socrata cities, whose floating values must not be converted.
- **`occurred_basis` varies per record**: `START_DATE` where present
  (occurrence), else `REPORT_DAT` (report). In the first backfill 3 of 48,928
  fell back to report.
- **`START_DATE` reaches back decades** for cold cases (earliest 1989). The
  crosswalk rows are therefore effective from 1900; at 2008 those homicides went
  unmapped.
- **Part I only.** Nine offence texts, no codes — the text is used as the code
  and the crosswalk is hand-written. No simple assault, vandalism or drug
  offences are published, so DC's violent track is aggravated-only. Because all
  nine have published NSCS vignettes, DC is the one city at 100% published-weight
  coverage.
- `CCN` is the key; WGS84 is published, Maryland State Plane (EPSG:26985) is the
  fallback.

**First backfill, measured**: 48,928 fetched across the 2024–2026 layers, 0
rejected, 9 duplicate CCNs dropped, zero unmapped after the date fix, census
retention 100.0% / 99.9%, every incident with a clock hour and a zero modal
shift. 177 km² of coverage, 273 resolution-8 cells.

## Austin — not loadable

Neither of Austin's current APD datasets publishes a location finer than a
census block group. `fdj4-gpfu` ("Crime Reports") has dropped its coordinate and
address columns, and the NIBRS dataset (`i7fg-wrk5`, "NIBRS Group A Offense
Crimes") carries only `census_block_group` and `zip_code`. A block group is
larger than a resolution-8 cell, and placing incidents at block-group centroids
would manufacture hot spots at those points. Austin stays seeded and disabled,
with no adapter; the trigger to revisit is APD publishing block-level points
again.

## Still to build

- **Sourced severity weights** for the NIBRS codes in the coverage gap above.
- **Austin**, if APD restores point locations.

### Out of scope, with triggers

- **Vector tiles** (§9.4). Phase 1 said a tile server "earns its keep at six
  cities". Maybe — but GZip plus the existing `bbox` filter should still cover
  it. The trigger is a measured resolution-10 payload for Los Angeles the browser
  cannot hold, not the city count.
- **Redis** (§9.5). The per-city LRU is cheaper until there is a second API
  process.
- **Airflow / Prefect / Dagster** (§8.4). `--all` is what a cron calls; cadence
  already lives in the registry.
- **Geocoded address search**, and the advertised per-city staleness tolerance —
  both still open from §15, neither blocking a city.
- **Cross-city comparison.** Not an omission. Percentiles are computed against
  each city's own distribution (§3.3), so two cities' figures are not on the same
  scale and the UI never places them side by side.
