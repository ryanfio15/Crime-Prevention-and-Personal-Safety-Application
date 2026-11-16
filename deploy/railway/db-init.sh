#!/bin/sh
# First-start init for the Railway `db` service (deploy/railway/db.Dockerfile).
#
# The application connects as `safety_app`, a non-superuser that owns the
# database, exactly as on the home server and under CloudNativePG. PostGIS is
# already created in POSTGRES_DB by the image's 10_postgis.sh, running as the
# superuser; migration 001's CREATE EXTENSION IF NOT EXISTS then finds it and
# does nothing, which is what lets a non-superuser run every migration.
#
# SAFETY_APP_PASSWORD is a service variable set by deploy/railway/up.sh; the
# api and etl services read it through ${{db.SAFETY_APP_PASSWORD}}.
set -eu

: "${SAFETY_APP_PASSWORD:?SAFETY_APP_PASSWORD must be set on the db service}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$SAFETY_APP_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
CREATE ROLE safety_app LOGIN PASSWORD :'app_password';
ALTER DATABASE :"db" OWNER TO safety_app;
ALTER SCHEMA public OWNER TO safety_app;
SQL
