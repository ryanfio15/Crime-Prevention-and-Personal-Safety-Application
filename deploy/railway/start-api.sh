#!/bin/sh
# The `api` service's start command on Railway (deploy/railway/up.sh):
#
#   /usr/local/bin/docker-entrypoint.sh sh deploy/railway/start-api.sh
#
# 1. Wait for the database. On a brand-new project the PostGIS volume's first
#    initdb takes several minutes, much longer than migrate's own one-minute
#    wait, and crash-looping through it reads like a configuration error.
# 2. Apply migrations and reference data (python -m safety.migrate). This is
#    the Kubernetes migrate Job's work. It runs here rather than as a Railway
#    pre-deploy command so the whole service is configured from the API in one
#    place. Railway keeps the previous deployment serving until this one passes
#    its health check, so migrations stay expand/contract, as everywhere else
#    (docs/DEPLOYMENT.md, "Migrations during a deploy").
# 3. Serve, as the image's own CMD does. Railway does not run a custom start
#    command through a shell, so `$PORT` is expanded here, by this script.
#
# Started through docker-entrypoint.sh explicitly, so the process drops to the
# unprivileged `safety` account whether or not Railway keeps the image's
# ENTRYPOINT in front of a custom start command.
set -eu

python -c 'from safety.db import wait_for_db; wait_for_db(attempts=600, delay_seconds=2)'
python -m safety.migrate

exec uvicorn safety.api.main:app \
    --host 0.0.0.0 --port "${PORT:-8080}" \
    --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}"
