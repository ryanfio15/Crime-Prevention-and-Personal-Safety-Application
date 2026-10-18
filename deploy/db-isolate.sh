#!/usr/bin/env bash
# Move one instance off the shared bootstrap superuser onto its own database role.
#
#   sudo deploy/db-isolate.sh dev|prod              # isolate (idempotent; safe to re-run)
#   sudo deploy/db-isolate.sh dev|prod --rollback   # put the instance back on `safety`
#
# Each instance gets one LOGIN role that owns its database and every application
# object in it -- safety_dev owns safety_dev, safety_prod owns safety -- NOSUPERUSER
# NOCREATEDB NOCREATEROLE. The API, ETL, ops and migrate all connect as it.
# `safety` stays the superuser for break-glass work and backups (container socket).
# See docs/DEPLOY.md "Database roles" for the why, the rollback and the caveats.
#
# What it does, in order (any failed pre-check exits before anything changes):
#   1. pre-checks: container up, `safety` is superuser, the database exists, the
#      instance .env points at it as `safety` (or already as the role: a re-run),
#      and the script and SQL are committed (what runs is what CI tested);
#   2. takes the deploy lock and the instance's ETL lock, and holds both until it
#      exits -- no deploy migrates and no ETL/ops runs on this instance meanwhile,
#      including during an automatic rollback;
#   3. copies the .env to /var/lib/safety-deploy/env-backup/<instance>.env.pre-isolation
#      (root 0600, once): it holds the superuser password, and the instance
#      directories are readable by OS user `safety`, which both instances run as;
#   4. creates the role (if missing) with a fresh random password, sent on stdin;
#   5. ALTER DATABASE ... OWNER, then deploy/db/transfer-ownership.sql (one ALTER
#      per object, lock_timeout 2s), retried on lock timeouts and deadlocks, and
#      requires its report to come back empty;
#   6. REVOKE CONNECT, TEMPORARY ... FROM PUBLIC; GRANT them to the role;
#   7. rewrites POSTGRES_USER / POSTGRES_PASSWORD in the .env, every other line kept;
#   8. restarts safety-api@<instance> and smokes it; on failure rolls back by itself.
#
# Never prints the .env or the password. No data is dropped: ownership changes are
# catalog updates, and the superuser can use every object whatever its owner.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0 $*" >&2; exit 1; }

instance=${1:-}
mode=isolate
case ${2:-} in
    "") ;;
    --rollback) mode=rollback ;;
    *) echo "usage: $0 dev|prod [--rollback]" >&2; exit 2 ;;
esac
case $instance in
    dev)  db=safety_dev; role=safety_dev;  port=8001 ;;
    prod) db=safety;     role=safety_prod; port=8000 ;;
    *) echo "usage: $0 dev|prod [--rollback]" >&2; exit 2 ;;
esac

repo=$(cd "$(dirname "$0")/.." && pwd)
inst=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance
env=$inst/.env
state=/var/lib/safety-deploy
bakdir=$state/env-backup
bak=$bakdir/$instance.env.pre-isolation
rotated=$bakdir/superuser-rotated
container=safety_db
sql=$repo/deploy/db/transfer-ownership.sql
smoke=/usr/local/lib/safety-deploy/smoke.sh

log() { echo "db-isolate[$instance]: $*"; }
die() { echo "db-isolate[$instance]: $*" >&2; exit 1; }

# The OS user that owns the instance's .env and data (N1): the same allow-listed
# lookup as instance_user in deploy/lib/release.sh (this script runs from the
# checkout and does not source the installed copy). Root-owned state only.
owner=$(tr -d '[:space:]' 2>/dev/null < "$state/$instance.user") || owner=
owner=${owner:-safety}
case "$instance:$owner" in
    prod:safety|dev:safety|dev:safety-dev) ;;
    *) die "refusing: instance $instance may not run as '$owner'" ;;
esac
getent passwd "$owner" >/dev/null || die "no such user: $owner"

# psql as the bootstrap superuser over the container's local socket.
psql_su() { docker exec -i "$container" psql -U safety -X -q -v ON_ERROR_STOP=1 "$@"; }
scalar() { psql_su -d "$1" -Atc "$2"; }

restart_and_smoke() {
    local deployed expected=-
    deployed=$(cat "$state/$instance.deployed" 2>/dev/null || true)
    # ?sha: the commit must match when health reports one (smoke.sh).
    if [ -n "$deployed" ]; then expected="?$deployed"; fi
    systemctl restart "safety-api@$instance"
    "$smoke" "$port" "$expected"
}

# Put the pre-isolation .env back and serve on it. Ownership is left as it is:
# the superuser can use every object whatever its owner, so the old credentials
# work at once. Reversing the ownership too is a manual step (docs/DEPLOY.md).
rollback() {
    [ -f "$bak" ] || die "no $bak; nothing to roll back to"
    if [ -e "$rotated" ]; then
        die "the superuser password was rotated after isolation ($rotated), so $bak holds a dead password; roll back by hand with the new one (docs/DEPLOY.md \"Database roles\")"
    fi
    install -o "$owner" -g "$owner" -m 0600 "$bak" "$env"
    psql_su -d postgres -c "GRANT CONNECT, TEMPORARY ON DATABASE $db TO PUBLIC"
    log "restored the pre-isolation .env; restarting safety-api@$instance"
    restart_and_smoke || die "rollback smoke FAILED: safety-api@$instance is not serving -- investigate now"
    log "rolled back: safety-api@$instance serves as the superuser again"
}

# --- 1. pre-checks -------------------------------------------------------------
[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = true ] ||
    die "container $container is not running"
[ "$(scalar postgres "select rolsuper from pg_roles where rolname='safety'")" = t ] ||
    die "role safety is not a superuser here; refusing"
[ "$(scalar postgres "select count(*) from pg_database where datname='$db'")" = 1 ] ||
    die "database $db does not exist"
[ -f "$env" ] || die "$env is missing"
[ -x "$smoke" ] || die "$smoke is missing; run deploy/install-deployer.sh"

if [ "$mode" = isolate ]; then
    if grep -qx "POSTGRES_DB=$db" "$env"; then
        :
    elif [ "$instance" = prod ] && ! grep -q '^POSTGRES_DB=' "$env"; then
        :   # safety/config.py defaults to `safety`, which is prod's database
    else
        die "$env does not point at database $db; refusing"
    fi
    grep -qx 'POSTGRES_USER=safety' "$env" || grep -qx "POSTGRES_USER=$role" "$env" ||
        die "$env connects as neither safety nor $role; refusing"
    [ -f "$sql" ] || die "$sql is missing"
    # What runs on the host is what CI ran: no uncommitted edits to either file.
    git -c safe.directory="$repo" -C "$repo" diff --quiet HEAD -- deploy/db deploy/db-isolate.sh ||
        die "uncommitted changes under deploy/db or to this script in $repo; refusing"
fi

# --- 2. locks, held until exit ---------------------------------------------------
# The deploy lock first, as install.sh and autodeploy.sh take it: a deploy waiting
# meanwhile just skips its tick (autodeploy exits 0 after 600 s).
exec 8>"$state/deploy.lock"
log "waiting for the deploy lock"
flock -w 900 8 || die "the deploy lock has been held for 15 minutes; try again later"
# Then the instance's ETL lock, which every ETL/ops unit takes with flock -w 21600:
# waits out a running pull, and timers that fire meanwhile queue behind us.
# Root never creates or opens for writing a path inside the instance user's
# data/: a planted symlink there would be followed (protected_symlinks covers
# sticky directories only). A missing lock is created by the owner; an existing
# one must be a regular file, and is opened read-only -- flock works on any fd.
lockf=$inst/data/.etl.lock
if [ ! -e "$lockf" ] && [ ! -L "$lockf" ]; then
    # shellcheck disable=SC2016  # $1 expands in the inner sh
    runuser -u "$owner" -- sh -c 'umask 022; : >> "$1"' sh "$lockf"
fi
{ [ -f "$lockf" ] && [ ! -L "$lockf" ]; } || die "$lockf is not a regular file; refusing"
exec 9<"$lockf"
log "waiting for the ETL lock"
flock -w 3600 9 || die "an ETL run has held $inst/data/.etl.lock for an hour; try again later"

if [ "$mode" = rollback ]; then
    rollback
    exit 0
fi

# --- 3. root-only backup of the superuser .env -----------------------------------
install -d -o root -g root -m 0700 "$bakdir"
if [ ! -e "$bak" ]; then
    grep -qx 'POSTGRES_USER=safety' "$env" ||
        die "no $bak yet, but $env no longer connects as safety; refusing to back up the wrong file"
    install -o root -g root -m 0600 "$env" "$bak"
    log "saved the pre-isolation .env to $bak"
fi

# --- 4. role and password ---------------------------------------------------------
pw=$(openssl rand -hex 32)   # hex: needs no quoting in SQL or the .env
psql_su -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') THEN
    CREATE ROLE $role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
\$\$;
ALTER ROLE $role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$pw';
SQL
log "role $role ready (new password set)"

# --- 5. ownership -------------------------------------------------------------------
psql_su -d postgres -c "ALTER DATABASE $db OWNER TO $role"
report=""
for attempt in 1 2 3 4 5; do
    errf=$(mktemp)
    if report=$(psql_su -d "$db" -At -v from_role=safety -v to_role="$role" < "$sql" 2>"$errf"); then
        rm -f "$errf"
        break
    fi
    if grep -qE 'lock timeout|deadlock detected' "$errf"; then
        log "transfer attempt $attempt hit a lock timeout or deadlock; retrying in 5s"
        cat "$errf" >&2
        rm -f "$errf"
        [ "$attempt" -lt 5 ] || die "transfer still blocked after 5 attempts; re-run later (it resumes where it stopped)"
        sleep 5
        continue
    fi
    cat "$errf" >&2
    rm -f "$errf"
    die "transfer-ownership.sql failed; nothing else changed (re-running is safe)"
done
if [ -n "$report" ]; then
    printf '%s\n' "$report" >&2
    die "objects in $db are still owned by safety (above); the .env was not changed"
fi
log "transfer report empty: every application object in $db is owned by $role"

# --- 6. connect privileges --------------------------------------------------------
psql_su -d postgres <<SQL
REVOKE CONNECT, TEMPORARY ON DATABASE $db FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE $db TO $role;
SQL

# --- 7. the .env ------------------------------------------------------------------
# awk into a temp file beside it, then install: never a half-written .env. The
# password reaches awk through its environment, not its argv.
tmp=$(mktemp "$inst/.env.isolate.XXXXXX")
trap 'rm -f "$tmp"' EXIT
add_db=""   # prod may rely on the config default; write it down explicitly
if [ "$instance" = prod ]; then add_db=safety; fi
ROLE=$role PW=$pw ADD_DB=$add_db awk '
    /^POSTGRES_USER=/     { print "POSTGRES_USER=" ENVIRON["ROLE"]; u = 1; next }
    /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" ENVIRON["PW"]; p = 1; next }
    /^POSTGRES_DB=/       { d = 1 }
    { print }
    END {
        if (!u) print "POSTGRES_USER=" ENVIRON["ROLE"]
        if (!p) print "POSTGRES_PASSWORD=" ENVIRON["PW"]
        if (!d && ENVIRON["ADD_DB"] != "") print "POSTGRES_DB=" ENVIRON["ADD_DB"]
    }
' "$env" > "$tmp"
install -o "$owner" -g "$owner" -m 0600 "$tmp" "$env"
rm -f "$tmp"
unset pw
[ "$(grep -cx "POSTGRES_USER=$role" "$env")" = 1 ] || die "$env was not rewritten as expected"
log "$env now connects as $role"

# --- 8. restart, smoke, and roll back on failure ----------------------------------
if restart_and_smoke; then
    log "isolated: safety-api@$instance serves as $role"
else
    echo "db-isolate[$instance]: smoke FAILED as $role; rolling back" >&2
    rollback
    exit 1
fi
