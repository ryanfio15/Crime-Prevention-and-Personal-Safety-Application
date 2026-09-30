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
| Chicago | ✅ Socrata | ⚠️ generated, needs review | TIGER PLACE | ❌ |
| Washington DC | ❌ Esri FeatureServer | ❌ | TIGER PLACE | ❌ |
| Seattle | ❌ Socrata | ❌ | TIGER PLACE | ❌ |
| Los Angeles | ❌ Socrata | ❌ | TIGER PLACE | ❌ |
| Austin | ❌ Socrata | ❌ | TIGER PLACE | ❌ |

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

---

## Still to build

**Washington DC** (`esri_featureserver`) is next, and is the paradigm test.
The seeded `incident_dataset = 'crime_incidents'` is a placeholder: an Esri
endpoint addresses layers numerically (`/MapServer/<n>/query`), so the first task
is confirming the layer index. Pagination is `resultOffset` /
`resultRecordCount` with `exceededTransferLimit`. Annual layers plus a rolling
last-30-days feed, and the annual ones have no usable "modified since", so
incrementals diff against the previous bronze snapshot. DC publishes both WGS84
and Maryland State Plane coordinates — prefer the former, reproject the latter
where it is the only real pair, `(0,0)` in both is `missing_coordinates`. MPD
publishes `REPORT_DAT` and `START_DATE`, so `occurred_basis` varies per record
rather than per city.

**Seattle** (`tazs-3rd5`): NIBRS post-May-2019, `backfill_start_date` already
floors the window above the break. Publishes `offense_start_datetime` — a real
occurrence time.

**Los Angeles**: the seeded `nibrs-offenses` is a placeholder, not a Socrata
4x4 — resolve the real IDs first. Offences only; Victims is a different grain.
Bi-weekly, so "checked, nothing new" is the normal outcome. The city that
motivates the `COPY` change.

**Austin** (`fdj4-gpfu`): NIBRS Group A from 2019, published as annual datasets,
so pagination is per-year-file plus snapshot diffing — reuse DC's shape.

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
