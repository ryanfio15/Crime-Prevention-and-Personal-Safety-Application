#!/bin/sh
# Fix the volume mount, then drop privileges.
#
# Mounted volumes arrive owned by root regardless of what the image sets -- both
# on Railway and with a plain `docker run -v` -- while the application runs as
# the unprivileged `safety` account. The ETL would then fail with a permission
# error partway through its first pull, when it tries to write a bronze snapshot
# (safety/etl/bronze.py:102). The container therefore starts as root, fixes the
# one path the application needs to write, and immediately gives up root.
#
# Only BRONZE_ROOT is touched. The API sets no writable paths at all.
set -e

if [ -n "${BRONZE_ROOT:-}" ] && [ "$(id -u)" = "0" ]; then
    mkdir -p "$BRONZE_ROOT"
    chown -R safety:safety "$BRONZE_ROOT"
fi

# setpriv over su/sudo: no extra package, no intermediate shell, and the
# application keeps PID 1 so platform stop signals reach it directly.
if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=safety --regid=safety --init-groups "$@"
fi

# Already unprivileged (someone passed --user); nothing to drop.
exec "$@"
