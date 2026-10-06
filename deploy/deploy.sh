#!/usr/bin/env bash
# Deploy one instance from what is on GitHub, not from this working copy.
#
#   deploy/deploy.sh prod    # origin/main    -> /srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod -> https://ryanfioserver.ddns.net
#   deploy/deploy.sh dev     # origin/testing -> /srv/safety/Crime-Prevention-and-Personal-Safety-Application/dev  -> https://ryanfioserverdev.ddns.net
#
# Run as your own user: it fetches with your GitHub key, then hands the
# extracted tree to /usr/local/lib/safety-deploy/install.sh (deploy/lib/
# install.sh, put there by deploy/install-deployer.sh) via sudo for the steps
# that touch /srv. Only committed and pushed code can reach an instance, so an
# uncommitted edit here never ends up in production. Pushes to main and testing
# also deploy on their own once CI passes (deploy/autodeploy.sh); this is the
# manual path for redeploying or for when the timer is stopped.
#
# Each instance keeps its own .venv, .env (which names its database and API
# port) and data/ across deploys; everything else is replaced, including files
# deleted on the branch.
#
# Both instances share the one PostGIS container (`safety_db`) and differ by
# database: `safety` for prod, `safety_dev` for dev. Never run `docker compose`
# from /srv/safety/Crime-Prevention-and-Personal-Safety-Application/dev -- prod's .env pins COMPOSE_PROJECT_NAME so its compose
# commands keep finding the existing volume, and dev's does not.
set -euo pipefail

case "${1:-}" in
    prod) branch=main ;;
    dev)  branch=testing ;;
    *)    echo "usage: $0 prod|dev" >&2; exit 2 ;;
esac
instance=$1
target=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)

git -C "$repo" fetch --quiet origin "$branch"
commit=$(git -C "$repo" rev-parse "origin/$branch")
echo "deploying origin/$branch ($commit) to $target"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
git -C "$repo" archive "$commit" | tar -x -C "$stage"

# Everything from here on -- rsync, pip, migrate, restart, health check -- is
# the same code the auto deployer runs, installed root-owned outside the repo.
# It waits for the deploy lock if the timer is mid-deploy.
sudo /usr/local/lib/safety-deploy/install.sh "$instance" "$stage" "$commit"
