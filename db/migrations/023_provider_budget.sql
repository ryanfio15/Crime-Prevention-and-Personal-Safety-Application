-- 023: provider daily budgets shared by every API replica (safety/routing/budget.py).
--
-- The Transitous and Photon allowances (settings.transit_daily_budget,
-- geocode_daily_budget) were counted in each API process. One uvicorn on one
-- host made that exact; a Kubernetes deployment runs several replicas behind a
-- load balancer, and per-process counting would multiply Transitous's "under
-- 720 plans a day" by the replica count and refill it on every rolling deploy.
--
-- With ROUTING_BUDGET_BACKEND=db, SharedDailyBudget charges one row per
-- provider per UTC day here with an atomic upsert. The home server keeps
-- `memory` and never touches this table. Rows are tiny (two providers, one row
-- each per day); nothing prunes them, and a year is ~730 rows.
--
-- In public rather than one of the data schemas: it is serving-side state, not
-- reference data, ETL bookkeeping or a data layer. The database owner (the
-- application role) can create in public on PostgreSQL 15+.

CREATE TABLE IF NOT EXISTS public.provider_budget (
    provider  text    NOT NULL,
    day       date    NOT NULL,
    used      integer NOT NULL DEFAULT 0 CHECK (used >= 0),
    PRIMARY KEY (provider, day)
);
