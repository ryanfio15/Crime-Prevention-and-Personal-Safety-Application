-- Move every application object in this database from :from_role to :to_role.
--
--   psql -d <db> -X -q -At -v from_role=safety -v to_role=safety_dev -f deploy/db/transfer-ownership.sql
--
-- Run by deploy/db-isolate.sh on the host and, against the same PostGIS image, by
-- the CI job (twice, to prove it is idempotent). Output is the report at the end:
-- empty means nothing the source role should hand over is still owned by it.
--
-- One statement per object (psql \gexec, autocommit): a lock is held only for its own
-- ALTER, never accumulated across the schema, and a partial run is harmless (the
-- superuser can use everything whatever its owner) and is finished by re-running.
-- Extension members and extension schemas (postgis's spatial_ref_sys, postgis_topology's
-- `topology`, the tiger geocoder's `tiger` and `tiger_data`, ...) are left with the
-- extension's owner.
-- Indexes, TOAST, owned (serial/identity) sequences and row types follow their table.
-- No REASSIGN OWNED: on the bootstrap superuser it errors, and it would also move shared
-- objects (another instance's database) -- see docs/DEPLOY.md "Database roles".
\set ON_ERROR_STOP on
SET lock_timeout = '2s';

-- Schemas
SELECT format('ALTER SCHEMA %I OWNER TO %I', n.nspname, :'to_role')
FROM pg_namespace n
WHERE n.nspowner = :'from_role'::regrole
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_namespace'::regclass
                  AND d.objid = n.oid AND d.deptype = 'e')
  -- An extension's own schema is not recorded in pg_depend (pg_extension.extnamespace
  -- points at it instead): `topology` and `tiger` on this image.
  AND NOT EXISTS (SELECT 1 FROM pg_extension e WHERE e.extnamespace = n.oid)
  -- Created by postgis_tiger_geocoder's install script for loaded TIGER data, and not
  -- recorded as a member either. A schema owner can drop what is inside it, so the
  -- extension schemas stay with the superuser.
  AND n.nspname <> 'tiger_data'
\gexec

-- Relations: tables (incl. partitioned parents and every partition), views, matviews,
-- standalone sequences, foreign tables. ALTER TABLE does not recurse to partitions,
-- which is why each partition is listed on its own.
SELECT format('ALTER %s %s OWNER TO %I',
         CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                        WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
         c.oid::regclass, :'to_role')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = :'from_role'::regrole AND c.relkind IN ('r','p','v','m','S','f')
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                  AND d.objid = c.oid AND d.deptype = 'e')
  AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d
           WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a','i')))
ORDER BY c.relkind = 'p' DESC, c.oid      -- parents first; order is cosmetic
\gexec

-- Functions / procedures / aggregates (none today; future-proofing)
SELECT format('ALTER %s %s OWNER TO %I',
         CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
         p.oid::regprocedure, :'to_role')
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proowner = :'from_role'::regrole
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                  AND d.objid = p.oid AND d.deptype = 'e')
\gexec

-- Standalone types: domains, enums, ranges, standalone composites (none today)
SELECT format('ALTER %s %s OWNER TO %I', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
         t.oid::regtype, :'to_role')
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_class c ON c.oid = t.typrelid
WHERE t.typowner = :'from_role'::regrole AND t.typtype IN ('d','e','r','c')
  AND (t.typrelid = 0 OR c.relkind = 'c') AND t.typcategory <> 'A'
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass
                  AND d.objid = t.oid AND d.deptype = 'e')
\gexec

-- Report: anything in this database the source role still owns that the statements
-- above should have handed over. Expected: 0 rows.
--
-- Read straight from the catalogs with the same filters as above, not from
-- pg_shdepend: PostgreSQL records no ownership dependencies for the bootstrap
-- superuser (oid 10 -- the image's POSTGRES_USER, `safety` on the host and in CI),
-- so a pg_shdepend report would come back empty whatever was left behind.
-- Extensions themselves, extension members, indexes, TOAST tables, row types and
-- owned sequences are excluded: they have no OWNER TO of their own, or follow
-- their table or extension.
SELECT 'schema' AS kind, quote_ident(n.nspname) AS object
FROM pg_namespace n
WHERE n.nspowner = :'from_role'::regrole
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_namespace'::regclass
                  AND d.objid = n.oid AND d.deptype = 'e')
  -- An extension's own schema is not recorded in pg_depend (pg_extension.extnamespace
  -- points at it instead): `topology` and `tiger` on this image.
  AND NOT EXISTS (SELECT 1 FROM pg_extension e WHERE e.extnamespace = n.oid)
  -- Created by postgis_tiger_geocoder's install script for loaded TIGER data, and not
  -- recorded as a member either. A schema owner can drop what is inside it, so the
  -- extension schemas stay with the superuser.
  AND n.nspname <> 'tiger_data'
UNION ALL
SELECT 'relation', c.oid::regclass::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = :'from_role'::regrole AND c.relkind IN ('r','p','v','m','S','f')
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                  AND d.objid = c.oid AND d.deptype = 'e')
  AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d
           WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a','i')))
UNION ALL
SELECT 'routine', p.oid::regprocedure::text
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proowner = :'from_role'::regrole
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                  AND d.objid = p.oid AND d.deptype = 'e')
UNION ALL
SELECT 'type', t.oid::regtype::text
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_class c ON c.oid = t.typrelid
WHERE t.typowner = :'from_role'::regrole AND t.typtype IN ('d','e','r','c')
  AND (t.typrelid = 0 OR c.relkind = 'c') AND t.typcategory <> 'A'
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass
                  AND d.objid = t.oid AND d.deptype = 'e')
UNION ALL
-- Any other kind of object (for a non-bootstrap source role, e.g. a rollback from
-- safety_dev back to safety, where pg_shdepend is populated): event triggers,
-- publications, operators, text-search objects and the like this script does not move.
SELECT 'other', pg_describe_object(s.classid, s.objid, 0)
FROM pg_shdepend s
WHERE s.refobjid = :'from_role'::regrole AND s.deptype = 'o'
  AND s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND s.classid NOT IN ('pg_extension'::regclass, 'pg_namespace'::regclass,
                        'pg_class'::regclass, 'pg_proc'::regclass, 'pg_type'::regclass)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = s.classid
                  AND d.objid = s.objid AND d.deptype IN ('e','a','i'));
