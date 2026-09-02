# Crime Prevention & Personal Safety Application
## System Design Document — Data Architecture & Multi-City Platform

---
### Ethan Charest

### Tyler Bagent

## 1. Purpose and Scope

This document defines the target architecture for a personal-safety application that aggregates publicly available, incident-level crime data from six initial metropolitan police departments — **Philadelphia, Chicago, Seattle, Los Angeles, Austin, and Washington, DC** — and presents it to users as *relative reported-crime activity* within roughly 500-meter geographic cells, rather than as an address-level "risk score."

It intentionally contains no code or pseudocode. It is meant to be the reference document a development team would use to scope database design, ETL pipelines, and backend services.

---

## 2. Target Users

Three primary user segments should drive design decisions, since they have different tolerances for latency, data freshness, and detail:

| Segment | Primary need | Design implication |
|---|---|---|
| **Residents / commuters** evaluating a neighborhood, apartment, or daily route | Quick, low-friction lookup of relative activity near an address or current location | Needs sub-second cell lookup, simple visual encoding (e.g., a 5-tier relative scale), works well on mobile with GPS |
| **Travelers / visitors** unfamiliar with a city | Orientation at the scale of "is this area typically quieter or busier than others nearby" | Needs map-first UI, offense-category filtering, no assumption of local knowledge |
| **Researchers, journalists, community groups, real-estate/relocation users** | Trend analysis, comparisons across time windows or neighborhoods, exportable views | Needs a documented API/query layer, historical time-series access, transparent methodology page |

A fourth, non-user-facing audience matters architecturally: **the six source agencies and their published terms of use**, which constrain redistribution, attribution, and commercial use, and must be respected per source (see Section 12).

---

## 3. Geographic Scope and the Cell Model

### 3.1 Coverage
Initial scope is limited to the incorporated city boundaries (or police-jurisdiction boundaries, where they differ slightly) of the six cities — not full metro areas or counties, since suburban/county law-enforcement agencies publish data in different formats, at different cadences, or not at all. This keeps the first release's data-quality bar consistent.

### 3.2 Recommendation: hexagonal cells (H3), not a square grid
The 500-meter idea is sound, but the underlying tiling scheme matters:

- A **square/fishnet grid** (e.g., snapping lat/lon to a fixed-meter grid in a projected coordinate system) is simple to reason about but has two practical problems: cell-to-cell distance is inconsistent (corner-adjacent cells are ~41% farther apart than edge-adjacent ones), and "which cell am I closest to" near a boundary is visually confusing on a map.
- **Uber's H3 hierarchical hexagonal grid** is the de facto standard for this use case (also used by ride-hailing, delivery, and urban-analytics platforms for exactly this kind of area-based aggregation). Hexagons have uniform adjacency, nest cleanly into coarser/finer resolutions, and every H3 index is a deterministic, pure function of (latitude, longitude, resolution) — no spatial database query is required to know which cell a point falls in.

**Concrete resolution recommendation:** H3 **resolution 8** has an average edge length of ~461 m and average cell area of ~0.74 km², which is the closest standard resolution to a "500-meter cell." Resolution 9 (~174 m edge, ~0.105 km² area) is a good secondary/drill-down resolution for dense urban cores where resolution-8 cells might otherwise mix very different sub-areas (e.g., a park abutting a commercial strip). Recommend storing **both res-8 and res-9 indexes** on every incident at ingestion time (computing both is essentially free), so the product can offer a "zoom to more detail" toggle without recomputation later.

### 3.3 Displaying "relative activity," not a risk score
Per the stated goal, each cell/time-window/offense-category combination should be expressed as a **relative measure** — e.g., a percentile or quintile of reported-incident density compared to other cells *in the same city* over the same time window — rather than a single absolute number implying a calibrated probability of victimization. This is both more honest about what the underlying data can support (see Section 13) and simpler to compute.

---

## 4. Data Sources: Current State (as verified)

All six cities publish open, machine-readable, incident-level crime data, but they differ meaningfully in platform, update cadence, and data-precision practices. This heterogeneity should directly shape the ingestion architecture (Section 8).

| City | Portal / platform | API technology | Update cadence (as published) | Location precision | Offense taxonomy |
|---|---|---|---|---|---|
| **Philadelphia** | OpenDataPhilly | Carto SQL API (`phl.carto.com`) | Daily | Point lat/long provided per incident | PPD "Part I" / "Part II" (UCR-style, department-specific text codes) |
| **Chicago** | Chicago Data Portal | Socrata SODA API (`data.cityofchicago.org`) | Daily; dataset excludes the **most recent 7 days** | Addresses/coordinates reduced to the **block level** for privacy | IUCR (Illinois Uniform Crime Reporting) codes, with a published code-to-description crosswalk |
| **Seattle** | Seattle Open Data | Socrata SODA API (`data.seattle.gov`) | Ongoing, "2008–present" dataset | Block-level | Migrated to **NIBRS** with a new RMS in May 2019 — pre-2019 and post-2019 records use different classification schemes, which must be handled as a schema break, not a continuous series |
| **Los Angeles** | LA Open Data | Socrata SODA API (`data.lacity.org`) | The legacy "Crime Data 2020–Present" dataset was **frozen in March 2024**; current data is now published as separate **NIBRS Offenses** and **NIBRS Victims** datasets (launched Oct 2024) refreshed **bi-weekly** | Addresses reduced to nearest hundred block; some records have unresolved (0,0) coordinates | NIBRS as of the 2024 RMS migration; legacy years use the older LAPD crime-code system |
| **Austin** | Austin Open Data | Socrata SODA API (`data.austintexas.gov`) | Published as annual "Crime Reports [year]" datasets; NIBRS Group A offense data available from 2019 forward | Address-level, city-published disclaimer that figures may differ from official APD statistics | NIBRS Group A |
| **Washington, DC** | Open Data DC | **Esri ArcGIS Hub / FeatureServer REST API** (not Socrata) | Published as annual "Crime Incidents in [year]" datasets plus a rolling "last 30 days" dataset | Geocoded to DC's Master Address Repository and snapped to street block; unresolved points show (0,0) | MPD offense text categories (violent/property groupings used in "Crime Cards") |

**Architecturally significant takeaways from this table:**
1. Three different API paradigms are in play (Socrata SODA for four cities, Carto SQL for Philadelphia, Esri ArcGIS REST for DC) — this rules out a single generic HTTP client and requires a **per-source adapter pattern** (Section 8).
2. Update cadence ranges from daily (Philadelphia, Chicago, historically Seattle) to **bi-weekly** (Los Angeles, post-2024). The product's "how fresh is this data" messaging and any caching/TTL strategy must be set per city, not globally.
3. Every city already truncates precise addresses to a block, intersection, or hundred-block level before publication, for victim-privacy reasons. This is good news for the application's own privacy posture (Section 13) — but it means true precision is already capped well above building-level, and 500 m cells are a reasonable — arguably necessary — floor of granularity, not an arbitrary product choice.
4. At least two cities (Seattle, LA) have a **hard classification-scheme discontinuity** mid-series from RMS migrations. Any trend or "change over time" feature must be aware of these breakpoints per city.

### 4.1 Core fields to capture per incident
Based on the union of what these sources expose:

| Field | Notes |
|---|---|
| Source incident ID | Preserve verbatim; not globally unique across cities, so must be namespaced |
| Occurred date/time | Some sources report only a date or round to a reporting period; capture a precision flag |
| Reported date/time (if distinct from occurred) | Useful for lag analysis; not always present |
| Latitude / longitude | As published (already truncated per city practice above) |
| Offense description / code (raw) | Store the source's own text and code, unaltered |
| Offense category (source's own grouping, e.g., Part I/II, IUCR, NIBRS Group A/B) | Needed for the standardization crosswalk |
| Location type (when available) | e.g., residence, street, parking lot, transit — inconsistently populated across cities |
| Reporting agency / source city | Required for provenance and for city-scoped comparisons |
| Dataset vintage / year-partition | Several cities publish per-year files; track which file/version an incident came from |

---

## 5. Data Architecture: Layered (Bronze / Silver / Gold) Model

Given the heterogeneity above, a single "load everything into one table" approach is fragile. A three-layer pipeline is recommended:

**Layer 1 — Raw / landing ("bronze").**
Each city's data is ingested and stored **exactly as received** (CSV/JSON/GeoJSON snapshots), partitioned by source city and pull date, in cheap object storage. This preserves auditability ("what did the city actually publish on this date"), lets you reprocess history if the standardization logic changes, and decouples ingestion from transformation so a malformed upstream file never corrupts production tables.

**Layer 2 — Standardized incident table ("silver").**
A single canonical schema that every city's raw data is transformed into (Section 6). This is the layer application logic and analytics query against; it is *not* re-derived from bronze on every request — it's materialized by the ETL pipeline.

**Layer 3 — Aggregated, cell-level rollups ("gold").**
Precomputed tables keyed by (city, H3 cell, time bucket, standardized offense category) with incident counts and relative-activity rankings. This is what the mobile/web app actually queries at read time — it should never need to scan raw incident rows to render a map.

This separation directly answers one of the stated open questions: **preprocessing and aggregation should happen upstream of the application, in the ETL pipeline, not dynamically on each user request.** Dynamic on-the-fly aggregation over raw points does not scale to a interactive map with many concurrent users, and it re-does identical work repeatedly. The gold layer should be refreshed incrementally as new source data lands, and pre-computed for the resolutions and time windows the product actually needs.

---

## 6. Standardized Schema (Silver Layer)

A single canonical incident record should carry, at minimum:

- A synthetic, globally unique record key (derived from source city + source incident ID, since raw IDs collide across cities)
- Source city, source agency, and a pointer to the exact source dataset/version it came from
- Standardized occurred timestamp (UTC-normalized, with an explicit **precision flag**: exact time / date-only / period-only) — because not every city reports incident time with the same granularity
- Raw latitude/longitude as published
- **Precomputed H3 index at resolution 8 and resolution 9** (see Section 3.2) — computed once at ingestion, never recomputed at query time
- Raw offense text/code exactly as published, plus the raw source-side category (Part I/II, IUCR, NIBRS Group)
- **Standardized offense category** (see Section 7) and a coarse **standardized severity bucket** analogous to UCR Part I/Part II, so cities that only report NIBRS can still be compared against cities still reporting Part I/II-style summaries
- Standardized, bucketed location-type (e.g., residential / commercial / street-public way / transit / school / park-recreation / other-unknown) mapped from each city's free-text or coded location field where available, defaulting to "unknown" where not
- Ingestion timestamp and pipeline/version metadata, for debugging and reproducibility

---

## 7. Standardizing Offense Classifications Across Cities

This is the hardest data-modeling problem in the project, because the six cities are not all on the same base standard, and even the ones that are (Seattle, LA post-2024, Austin) may map local NIBRS offense codes slightly differently in edge cases.

**Recommended approach — crosswalk to NIBRS, not to a bespoke internal taxonomy:**

1. **Adopt the FBI's NIBRS offense classification as the standardization target.** Three of the six sources (Seattle, Austin, LA post-2024) already publish in NIBRS. This minimizes the amount of custom mapping needed for those sources and gives the application a citable, external standard rather than an invented one.
2. **Build a per-city crosswalk table** (a maintained reference dataset, not application code) mapping each city's raw offense code/text to the closest NIBRS offense code, and secondarily to the coarser UCR Part I/Part II severity split (which remains useful because it's a coarser, more forgiving bucket when a precise NIBRS mapping is ambiguous — Philadelphia and Chicago, for instance, still publish in Part I/II style categories, and legacy years of Seattle/LA/Austin do too).
3. **Version the crosswalk.** When a source changes its RMS or code list (as Seattle did in 2019 and LA did in 2024), the crosswalk must be versioned by effective date so historical records keep the mapping that was valid when the pipeline first classified them, and reprocessing is explicit rather than silently rewriting history.
4. **Preserve the raw code alongside the mapped one, always.** Never discard the source's original classification — downstream users (especially the research/journalist segment) will want to verify or dispute a mapping.
5. **Treat "location type" as a separate, lower-confidence standardization problem**, since it is the least consistently populated field across sources; a simple rules-based bucketing (keyword matching against each city's free-text values) is adequate — this does not need a maintained crosswalk table the way offense codes do.

---

## 8. Ingestion, APIs, and Automated Updates

### 8.1 Per-source connector / adapter pattern
Because the six cities use three different API technologies (Socrata SODA, Carto SQL, Esri ArcGIS REST) with different pagination, filtering, and authentication conventions, the ingestion layer should define one common internal interface — "fetch new/updated records since timestamp X for city Y" — implemented by a **separate adapter per source**. This isolates the platform-specific quirks (e.g., Chicago's rolling 7-day publication lag, LA's split "current NIBRS" vs. "frozen legacy" datasets, DC's separate annual + rolling-30-day datasets, Austin's per-year dataset files) inside small, independently testable modules, so that adding a seventh city later means writing one new adapter, not touching shared logic.

### 8.2 Scheduling, driven by each source's actual cadence
A single global "refresh every night" job is wrong here, given the cadence differences documented in Section 4. Recommend:
- Daily incremental pulls for Philadelphia and Chicago (matching their daily publication).
- A daily *check*, but effectively bi-weekly *new data*, for Los Angeles's current NIBRS datasets — the orchestrator should be tolerant of "checked, nothing new" as the normal case.
- City-specific schedules stored in configuration (a small "source registry" table — see 8.4), not hard-coded in job logic, so cadence assumptions can be corrected without a code deployment.
- A separate, more frequent job (e.g., every few hours) against any source that specifically publishes a rolling "last N days" feed (DC, and historically Philadelphia's short-term feed), for the segment of users who care about very recent activity — while making clear in the UI that "recent" data is preliminary and subject to later revision/reclassification, which all these agencies explicitly caveat.

### 8.3 Idempotent, incremental loading
Each adapter should support pulling only records new or modified since the last successful run (via a "since" filter where the source API supports one, or via full-file diffing against the previous bronze snapshot where it doesn't — DC's annual-file model and Austin's per-year files effectively require the latter). Loads must be idempotent (safe to re-run) and should upsert by the synthetic incident key, since agencies do periodically revise or reclassify incidents after initial publication — a known characteristic of all six sources, which describe their data as preliminary.

### 8.4 Orchestration and a "source registry"
A workflow orchestrator (the class of tool represented by Airflow, Prefect, or Dagster) should run one DAG/pipeline per city: fetch (via that city's adapter) → validate → transform into the silver schema → refresh affected gold rollups. A small **source registry table** — one row per city/dataset — should hold each source's base URL, API type, expected cadence, last-successful-pull timestamp, and current crosswalk-version reference. This registry is what makes "add a seventh city" primarily a configuration and adapter-writing exercise rather than a schema change.

### 8.5 Validation before promotion to silver
Before a raw pull is transformed and promoted, automated checks should catch the failure modes these sources are known to have: missing/zeroed coordinates (explicitly called out by LA and DC as a real occurrence), duplicate incident IDs within a pull, offense codes not present in the current crosswalk version (flag for manual crosswalk review rather than silently dropping), and record counts wildly outside the historical norm for that source (a signal the upstream site changed its export format).

---

## 9. Database and Backend Architecture

### 9.1 Core datastore: PostgreSQL + PostGIS
A single PostgreSQL instance with the PostGIS extension is the right foundation:
- Native spatial types and indexing (GiST) support any ad hoc spatial queries the research/journalist segment needs (radius search, polygon overlays like neighborhood boundaries).
- H3's official PostgreSQL bindings allow H3 index computation and hierarchy operations to live in the database itself as well as in ingestion code, so both the ETL pipeline and any ad hoc analyst query can agree on the same cell definitions.
- Table partitioning by city and by year on the silver incident table keeps individual partitions small, makes backfilling or reprocessing a single city's history low-risk, and matches the natural partitioning of the upstream data (several sources already publish per-year files).

### 9.2 Object storage for the bronze layer
Raw per-pull snapshots belong in cheap, versioned object storage (S3-compatible), not in the relational database — they're write-once, read-rarely, and exist for auditability/reprocessing, not for query performance.

### 9.3 Precomputed gold tables, not on-demand aggregation
As established in Section 5, the gold layer (city, H3 cell, time bucket, offense category → count and relative-activity ranking) should be materialized by the ETL pipeline and simply read by the API layer. This is the single biggest lever for making the app feel instant on a map: the read path never touches millions of raw incident rows.

### 9.4 API and serving layer
A backend service layer (a typical choice would be a Python or Node-based REST/GraphQL service) sits in front of the gold tables and exposes the operations the client actually needs: cell-level activity by location, nearby-cells-in-a-radius, city metadata/boundaries, and offense-category filters. For the **map rendering** itself specifically, serving **pre-rendered vector tiles** of the H3 hexagon grid (via a tool like a PostGIS-aware tile server) is far more efficient than shipping raw geometry or per-incident points to the client — the client renders polygons that are already colored/bucketed server-side.

### 9.5 Caching
A cache layer (e.g., Redis) in front of the gold tables absorbs the highly repetitive read pattern of "what's the activity level near this cell" for popular areas, and should be invalidated per-city on each successful ETL refresh rather than on a fixed global TTL, since refresh cadence differs by city (Section 8.2).

### 9.6 Supporting future prediction/modeling work
The layered design directly supports adding predictive or trend-modeling features later without re-architecting: the gold-layer rollups (counts by cell, offense category, and time bucket) are exactly the feature set a spatio-temporal model would train on. Any such model should be treated as a downstream consumer of the gold layer, writing its own separate "modeled/forecast" table rather than overwriting reported-incident rollups — so the application can always distinguish "what was actually reported" from "what a model estimated," and so the product can choose not to expose predictive output at all in early releases without touching the core pipeline. (See Section 13 for why this distinction matters beyond engineering hygiene.)

---

## 10. Resolving a User's Location to a Cell

Because H3 cell membership is a **pure, deterministic function of (latitude, longitude, resolution)** — not a spatial database lookup against stored polygons — this is the one piece of the system that does not need a database query at all:

- The mobile/web client can compute the H3 index locally, on-device, from the user's GPS coordinates using a standard H3 client library (implementations exist for JavaScript, Swift, Kotlin/Java, etc.), instantly and offline.
- The client then makes a single, simple lookup request to the API for that specific H3 index's precomputed gold-layer statistics — an indexed key lookup, not a spatial search.
- The same approach applies to a **searched address** rather than current GPS location: geocode the address to lat/lon (via a geocoding provider), compute the H3 index client- or server-side, then do the same indexed lookup.
- For "show me the map around here," the client requests the precomputed statistics for the current cell plus its ring of neighboring cells (an O(1) operation in H3 — "give me the cells within k rings of this cell" — no bounding-box spatial query needed), which is what populates the surrounding map view.

This is a strong argument, independent of the 500 m product requirement, for choosing H3 over a custom or irregular grid: **the "which cell is the user in" problem disappears entirely** rather than becoming a spatial-query performance concern.

---

## 11. Scalability and Adding Future Cities

The architecture generalizes cleanly if two things stay decoupled from city-specific logic:
1. **Everything about a source's own quirks** (API type, cadence, raw field names, raw offense codes) lives in that city's adapter and crosswalk — never in shared pipeline or application code.
2. **Everything downstream of the silver layer** (gold rollups, API, map tiles, client) is written entirely in terms of the standardized schema and H3 cells, and is completely city-agnostic.

Under this split, onboarding a new city is: confirm it publishes incident-level open data with coordinates and offense codes → write one adapter → build one offense crosswalk → add one row to the source registry. No changes to the serving layer, API, or client are needed. This also means the six initial cities don't need to be treated as special cases anywhere except in their own adapters — the same pattern that handles city seven will already be handling city one through six.

---

## 12. Licensing, Attribution, and Terms of Use

Each of the six sources publishes its data under its own terms, and several include explicit disclaimers worth designing around rather than around ignoring:
- Philadelphia's dataset terms state the City retains rights in the database and disclaims accuracy warranties.
- Chicago's, Austin's, and DC's dataset pages explicitly describe the data as **preliminary and subject to revision**, extracted from records-management systems that are still being updated by investigators after initial reporting.
- Austin's own disclaimer notes its published figures "may differ from official Austin Police Department crime data."

Design implications: (a) the product should carry a clear, persistent methodology/attribution page naming each source agency and dataset, since re-display of government open data typically requires attribution even when redistribution itself is permitted; (b) any "last updated" or "data as of" indicator per city should be a first-class, visible UI element, not a footnote, given how explicitly several agencies flag their data as preliminary; (c) legal review of each city's specific terms-of-use page is a prerequisite before launch, not a formality — terms can and do differ city to city and can change.

---

## 13. Responsible-Design Considerations (Non-Functional but Important)

These aren't optional polish — they materially affect what the product should and shouldn't claim:

- **Reported crime is not the same as actual crime.** Incident data reflects what was reported to and recorded by police, which is shaped by reporting rates and enforcement patterns that vary by neighborhood and offense type — historically underreported categories (e.g., some sexual offenses) and historically over-enforced ones (e.g., some drug or quality-of-life offenses) will not have proportionally comparable reporting rates. The product should present activity levels as "reported incident density," explicitly, not as a proxy for true safety or risk.
- **Avoid a single address-level "risk score."** The design in this document already reflects this — cell-based, relative-to-city-distribution activity levels are more defensible than a precise per-address number, both statistically (the underlying data doesn't support that precision) and in terms of potential for misuse (e.g., steering, informal redlining-adjacent use).
- **Keep any future predictive/forecast output clearly separate from reported-incident data** (Section 9.6), and be cautious about building or exposing forward-looking "hotspot prediction" features at all — this is the same technical territory as predictive policing tools, which have a well-documented history of amplifying existing enforcement disparities through feedback loops. If offered, predictive features should be framed for personal situational awareness only, never marketed or made available for law-enforcement resource allocation, and should carry visible uncertainty/methodology disclosures.
- **No demographic overlays.** The system should not join crime data to race, income, or other demographic layers for display purposes — doing so invites exactly the kind of address/neighborhood profiling this document's own privacy-conscious cell design is trying to avoid.

---

## 14. Phased Implementation Roadmap

**Phase 1 — Single-city pipeline proof of concept.** Build and validate the full bronze → silver → gold pipeline against one source (Philadelphia is a reasonable first choice: daily cadence, direct point coordinates, one well-documented API). Validate the H3 resolution-8 cell aggregation and relative-activity ranking end to end.

**Phase 2 — Multi-source generalization.** Add the remaining five adapters and crosswalks one at a time, using Phase 1's pipeline as the template, specifically exercising the three API paradigms (Socrata, Carto, Esri) and the cadence differences (daily vs. bi-weekly) to confirm the source-registry/adapter split actually isolates city-specific quirks as intended.

**Phase 3 — Serving layer and client.** Build the gold-layer API, tile serving, caching, and the client-side H3 lookup flow (Section 10); ship a map-first MVP covering all six cities with relative-activity display only (no predictive features).

**Phase 4 — Hardening and expansion readiness.** Add validation/monitoring around data-quality failure modes (Section 8.5), formalize the source-registry-driven onboarding process, and use it to add a seventh city as a test of the generalization claim in Section 11 before broader expansion.

---

## 15. Open Questions to Resolve Before Build

- Which geocoding provider (for address search) will be used, and does its terms of service allow the intended query volume and caching approach?
- What historical time depth is actually needed at launch — full history back to each city's earliest available year, or a bounded rolling window (e.g., trailing 24 months)? This significantly affects initial backfill scope and storage.
- Should offense-category filtering in the UI expose the standardized NIBRS categories directly, or a simplified product-level grouping (e.g., "violent," "property," "other") layered on top of them?
- What is the target refresh/staleness tolerance the product is willing to advertise per city, given the Chicago 7-day lag and the LA bi-weekly cadence specifically?
