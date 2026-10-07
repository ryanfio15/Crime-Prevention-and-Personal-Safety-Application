#!/usr/bin/env bash
# One-time move of an instance from a flat checkout to the release layout.
#
#   sudo deploy/migrate-layout.sh prod|dev prepare
#   sudo deploy/migrate-layout.sh prod|dev cleanup
#
# prepare: builds I/releases/<sha> for the commit the instance is running now,
#   a fresh venv for it, and I/current. Stops nothing and deletes nothing: the
#   old flat tree and its .venv keep serving until the new unit templates are
#   installed (sudo deploy/install-deployer.sh --units) and safety-api@ is
#   restarted. Also seeds /var/lib/safety-deploy/<instance>.deployed.
# cleanup: once safety-api@<instance> is verifiably running from I/current,
#   removes the old flat files -- only names the deployed commit's own top
#   level contains, plus DEPLOYED_COMMIT and .venv. Never .env, data, venvs,
#   releases or current.
#
# Both phases are safe to re-run. Needs the root-owned helpers installed first
# (sudo deploy/install-deployer.sh). The full order is in docs/DEPLOY.md,
# "Moving an instance to the release layout".
set -euo pipefail

state=/var/lib/safety-deploy
lock=$state/deploy.lock
lib=/usr/local/lib/safety-deploy
cache=$state/repo.git
remote=https://github.com/ryanfio15/Crime-Prevention-and-Personal-Safety-Application.git

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0 $*" >&2; exit 1; }
[ -f "$lib/release.sh" ] || { echo "run sudo deploy/install-deployer.sh first" >&2; exit 1; }

# Same lock as every deploy: the layout must not change under one.
[ "${SAFETY_DEPLOY_LOCK_HELD:-}" = 1 ] ||
    exec env SAFETY_DEPLOY_LOCK_HELD=1 flock -w 900 "$lock" "$0" "$@"

case "${1:-}" in
    prod) port=8000 ;;
    dev)  port=8001 ;;
    *)    echo "usage: $0 prod|dev prepare|cleanup" >&2; exit 2 ;;
esac
instance=$1
phase=${2:-}
target=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance

# shellcheck disable=SC1091
. "$lib/release.sh"

prepare() {
    if [ ! -d "$cache" ]; then
        git init --quiet --bare "$cache"
        git -C "$cache" remote add origin "$remote"
    fi
    git -C "$cache" fetch --quiet origin \
        '+refs/heads/main:refs/remotes/origin/main' \
        '+refs/heads/testing:refs/remotes/origin/testing'

    # Re-run after a deploy: current already names the release in use.
    # stage is global: the EXIT trap runs after this function has returned.
    local sha installed
    sha=$(current_sha "$target")
    if [ -z "$sha" ]; then
        installed=$(tr -d '[:space:]' < "$target/DEPLOYED_COMMIT" 2>/dev/null) || installed=
        if [[ ! $installed =~ ^[0-9a-f]{7,40}$ ]] ||
            ! sha=$(git -C "$cache" rev-parse --quiet --verify "$installed^{commit}"); then
            echo "$target/DEPLOYED_COMMIT ('$installed') is not a commit on main or testing" >&2
            exit 1
        fi
    fi
    echo "$instance: preparing release $sha"

    stage=$(mktemp -d "$state/stage.XXXXXX")
    trap 'rm -rf "$stage"' EXIT
    git -C "$cache" archive "$sha" | tar -x -C "$stage"
    build_release "$target" "$sha" "$stage"

    local rel=$target/releases/$sha venv
    venv=$(readlink "$rel/.venv")
    ensure_venv "$target" "${venv##*/}" "$rel/requirements.txt"
    runuser -u safety -- env -C "$rel" HOME=/srv/safety PYTHONDONTWRITEBYTECODE=1 \
        .venv/bin/python -c "import safety.api.main, safety.migrate, safety.etl.run"

    [ -L "$target/current" ] || switch_current "$target" "$sha"
    [ -f "$state/$instance.deployed" ] || echo "$sha" > "$state/$instance.deployed"
    echo "$instance: prepared; current -> $(readlink "$target/current"), deployed = $(cat "$state/$instance.deployed")"
}

cleanup() {
    local sha pid cwd unit name path
    sha=$(current_sha "$target")
    if [ -z "$sha" ] || [ ! -f "$target/releases/$sha/.release-complete" ]; then
        echo "$target/current does not point at a complete release; run prepare" >&2
        exit 1
    fi
    # A pull running from the flat tree would lose its files mid-run.
    if etl_running "$instance"; then
        echo "ETL is running on $instance; try again when it has finished" >&2
        exit 75
    fi

    # Verified, not assumed: every unit names I/current, the live process is
    # really running inside the release, and it passes the smoke checks.
    for unit in "safety-api@$instance.service" "safety-etl@$instance.service" "safety-etl-hourly@$instance.service"; do
        [ "$(systemctl show -p WorkingDirectory --value "$unit")" = "$target/current" ] ||
            { echo "$unit does not run from $target/current; install the new units first" >&2; exit 1; }
    done
    pid=$(systemctl show -p MainPID --value "safety-api@$instance")
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || cwd=
    if [ "$pid" = 0 ] || [ "$cwd" != "$target/releases/$sha" ]; then
        echo "safety-api@$instance is not running from $target/releases/$sha (cwd '$cwd'); restart it first" >&2
        exit 1
    fi
    "$lib/smoke.sh" "$port" "?$sha"

    while read -r name; do
        case $name in
            ''|.|..|*/*|.env|data|venvs|releases|current) continue ;;
        esac
        path=$target/$name
        if [ -L "$path" ]; then
            rm -f -- "$path"
        elif [ -e "$path" ]; then
            echo "removing $path"
            rm -rf --one-file-system -- "$path"
        fi
    done < <(git -C "$cache" ls-tree --name-only "$sha"; echo DEPLOYED_COMMIT; echo .venv)
    echo "$instance: cleaned up; $target now holds:"
    ls -A "$target"
}

case $phase in
    prepare) prepare ;;
    cleanup) cleanup ;;
    *)       echo "usage: $0 prod|dev prepare|cleanup" >&2; exit 2 ;;
esac
