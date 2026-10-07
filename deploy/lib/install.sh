#!/usr/bin/env bash
# Build a release next to the live one, prove it works, then switch to it.
#
#   install.sh prod|dev <stage_dir> <full_sha>
#
# The one place the install steps live: deploy/deploy.sh (a human, via sudo)
# and deploy/autodeploy.sh (the safety-autodeploy@ timer) both end here. It
# runs as root from its installed copy, /usr/local/lib/safety-deploy/install.sh
# (deploy/install-deployer.sh puts it there), never from an instance tree.
#
# Root only moves files, compiles bytecode and calls systemctl. Everything that
# executes repository code -- pip, the import check, safety.migrate, the
# candidate server -- runs as `safety`.
#
#   1. release   I/releases/<sha> from the stage (reused if already complete)
#   2. venv      I/venvs/<hash> for its requirements.txt (reused if it exists)
#   3. import    the entry points import from the release
#   4. migrate   from the release. Migrations must work with the previous
#                release too (expand, then contract in a later commit): they
#                run before anything below can reject the build, and a
#                rollback does not undo them.
#   5. candidate the release serves on a spare port and passes smoke.sh
#   6. switch    I/current -> the release, restart safety-api@<instance>
#   7. verify    smoke.sh on the live port
#   8. rollback  if 7 fails: I/current back, restart, verify the old commit
#
# A failure in 1-5 leaves the live site untouched. Exit status: 0 deployed;
# 75 refused because an ETL run is using the instance (try again later);
# anything else failed, after rolling back if it got as far as switching.
#
# Test hook: /var/lib/safety-deploy/<instance>.inject_fail containing
# `candidate` or `live` fails that stage's smoke check on purpose, to exercise
# the rejection and rollback paths. Root-only (the directory is 0700); absent
# means a normal deploy.
set -euo pipefail

state=/var/lib/safety-deploy
lock=$state/deploy.lock
lib=/usr/local/lib/safety-deploy

# One deploy at a time across both instances: they share a database server and
# the bare cache. autodeploy.sh takes the lock itself before it even looks at
# GitHub and says so through the environment; sudo resets the environment, so a
# manual run always queues here instead.
[ "${SAFETY_DEPLOY_LOCK_HELD:-}" = 1 ] ||
    exec env SAFETY_DEPLOY_LOCK_HELD=1 flock -w 900 "$lock" "$0" "$@"

case "${1:-}" in
    # Hardcoded rather than read from .env: root does not parse a file the
    # `safety` user can write. Must match API_PORT in each instance's .env.
    # The candidate ports are loopback-only and used for seconds per deploy.
    prod) port=8000; cport=18000 ;;
    dev)  port=8001; cport=18001 ;;
    *)    echo "usage: $0 prod|dev <stage_dir> <full_sha>" >&2; exit 2 ;;
esac
instance=$1
stage=${2:?usage: $0 prod|dev <stage_dir> <full_sha>}
sha=${3:?usage: $0 prod|dev <stage_dir> <full_sha>}
target=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance
rel=$target/releases/$sha
cunit=safety-candidate-$instance

[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "not a full sha: $sha" >&2; exit 2; }
[ -d "$stage" ] || { echo "no such stage directory: $stage" >&2; exit 2; }

# shellcheck disable=SC1091
. "$lib/release.sh"

# journald reads a leading <3> as priority err; keep a terminal readable.
err() { if [ -t 2 ]; then echo "$*" >&2; else echo "<3>$*" >&2; fi; }

previous=$(current_sha "$target")
if [ -z "$previous" ] || [ ! -d "$target/releases/$previous" ]; then
    err "$target/current does not point at a release; run deploy/migrate-layout.sh $instance first"
    exit 2
fi

# Swapping releases under a running pull is safe for the pull (its release is
# kept), but migrate could block on its locks. Come back on the next tick.
if etl_running "$instance"; then
    echo "ETL is running on $instance; not deploying now" >&2
    exit 75
fi

inject=$(tr -d '[:space:]' 2>/dev/null < "$state/$instance.inject_fail") || inject=
[ -z "$inject" ] || echo "inject_fail is set to '$inject' for $instance"

as_safety() {
    runuser -u safety -- env -C "$rel" HOME=/srv/safety PYTHONDONTWRITEBYTECODE=1 "$@"
}

echo "1/7 release $sha"
build_release "$target" "$sha" "$stage"

venv=$(readlink "$rel/.venv")
echo "2/7 venv ${venv##*/}"
ensure_venv "$target" "${venv##*/}" "$rel/requirements.txt"

echo "3/7 import check"
as_safety .venv/bin/python -c "import safety.api.main, safety.migrate, safety.etl.run"

echo "4/7 migrate"
# lock_timeout: a migration waiting behind a long query fails after 30s instead
# of queueing every API request behind its own lock request.
as_safety PGOPTIONS='-c lock_timeout=30s' .venv/bin/python -m safety.migrate

echo "5/7 candidate on :$cport"
stop_candidate() {
    systemctl stop "$cunit" 2>/dev/null || true
    systemctl reset-failed "$cunit" 2>/dev/null || true
}
stop_candidate
trap stop_candidate EXIT
systemd-run --quiet --unit="$cunit" --collect -p Type=exec \
    --uid=safety --gid=safety \
    -p EnvironmentFile="$target/.env" -p Environment=PYTHONDONTWRITEBYTECODE=1 \
    --working-directory="$rel" \
    -p NoNewPrivileges=true -p PrivateTmp=true -p ProtectSystem=full -p ProtectHome=true \
    -p RuntimeMaxSec=300 \
    -- "$rel/.venv/bin/python" -m uvicorn safety.api.main:app --host 127.0.0.1 --port "$cport"
if [ "$inject" = candidate ]; then
    echo "inject_fail: failing the candidate check on purpose" >&2
    ok=false
elif "$lib/smoke.sh" "$cport" "$sha"; then
    ok=true
else
    ok=false
fi
if ! $ok; then
    journalctl -u "$cunit" -n 20 --no-pager -o cat >&2 || true
    err "$instance: rejected $sha at the candidate check; still serving $previous"
    exit 1
fi
stop_candidate

echo "6/7 switch $previous -> $sha"
switch_current "$target" "$sha"
systemctl restart "safety-api@$instance" || true

echo "7/7 verify on :$port"
if [ "$inject" = live ]; then
    echo "inject_fail: failing the live check on purpose" >&2
    ok=false
elif "$lib/smoke.sh" "$port" "$sha"; then
    ok=true
else
    ok=false
fi
if ! $ok; then
    journalctl -u "safety-api@$instance" -n 20 --no-pager -o cat >&2 || true
    echo "rolling back to $previous"
    switch_current "$target" "$previous"
    systemctl restart "safety-api@$instance" || true
    if "$lib/smoke.sh" "$port" "?$previous"; then
        err "$instance: rolled back $sha -> $previous"
    else
        err "$instance: rolled back $sha -> $previous, but $previous is not healthy either; see journalctl -u safety-api@$instance"
    fi
    exit 1
fi

echo "$sha" > "$state/$instance.deployed"
prune_releases "$target" "$sha" "$previous"
echo "$instance is up on :$port at $sha"
