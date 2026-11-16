# Deploy and Host Safety Map on Railway

A public web map of relative reported-crime activity in roughly 500 m hexagonal cells, built from official
incident-level open data for six US cities: Philadelphia, Chicago, Seattle, Los Angeles, Washington DC and Austin.
One click gives you your own copy at a public `https://….up.railway.app` address that keeps its data current by
itself.

## About Hosting Safety Map

The template deploys four services that are already wired together. Nothing needs configuring before you click
**Deploy**:

- **api**: the website and its API, the only public service;
- **db**: PostgreSQL with PostGIS, holding one rolling year of incidents per city;
- **valhalla**: street routing for walking, cycling and driving directions;
- **etl**: a background worker that loads the data and keeps it current.

About 10–20 minutes after you click Deploy, the site is up. Its address is on the **api** service, under
**Settings → Networking**. The first data load then runs by itself for one to a few hours, mostly waiting on the
cities' portals. Meanwhile the map shows a progress bar, and each city appears as soon as its own data is in. After
that the worker checks every hour for new incidents and deletes anything older than a year. You don't need to do
anything.

## Common Use Cases

- A shareable, always-current map of reported-crime activity for the six cities
- Comparing areas within a city by time of day and day of week
- Walking, cycling, driving and transit directions alongside the map

## Dependencies for Safety Map Hosting

- A Railway account with a payment method. Billing is by use; the database and the website are always on, and the
  worker uses noticeable memory only during the first load.
- Nothing else. Every password is generated for your deployment.

### Deployment Dependencies

- Source code and full documentation: https://github.com/ryanfio15/Crime-Prevention-and-Personal-Safety-Application
  (branch `railway-deployment`; Railway notes in `docs/RAILWAY.md`)
- Optional: a free [Socrata app token](https://evergreen.data.socrata.com/signup), set as `SOCRATA_APP_TOKEN` on
  the **etl** service, makes the first load faster for Chicago, Seattle, Los Angeles and Austin.

### Implementation Details

- **Which cities load:** the `BOOTSTRAP_CITIES` variable on the **etl** service, read once on the first start
  (default `phl,chi,sea,lax,dc,aus`).
- **Austin:** confirm the City of Austin's data reuse terms before showing its data publicly. To leave Austin out,
  set `BOOTSTRAP_CITIES` to `phl,chi,sea,lax,dc` before the first load starts.
- **Values are relative within each city** and are not comparable between cities.

## Why Deploy Safety Map on Railway?

Railway is a singular platform to deploy your infrastructure stack. Railway will host your infrastructure so you
don't have to deal with configuration, while allowing you to vertically and horizontally scale it.

By deploying Safety Map on Railway, you are one step closer to supporting a complete full-stack application with
minimal burden. Host your servers, databases, AI agents, and more on Railway.

## Taking it down

Open the project, then **Settings → Danger → Delete project**. This deletes the site and its data, and billing
stops.
