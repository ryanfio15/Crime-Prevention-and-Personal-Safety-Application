#!/usr/bin/env bash
# Deploy one instance from what is on GitHub, not from this working copy.
#
#   deploy/deploy.sh prod    # origin/main    -> /srv/safety/prod -> https://ryanfioserver.ddns.net
#   deploy/deploy.sh dev     # origin/testing -> /srv/safety/dev  -> https://ryanfioserverdev.ddns.net
#
# Run as your own user: it fetches with your GitHub key, then uses sudo for the
# steps that touch /srv. Only committed and pushed code can reach an instance,
# so an uncommitted edit here never ends up in production.
#
# Each instance keeps its own .venv, .env (which names its database and API
# port) and data/ across deploys; everything else is replaced, including files
# deleted on the branch.
#
# Both instances share the one PostGIS container (`safety_db`) and differ by
# database: `safety` for prod, `safety_dev` for dev. Never run `docker compose`
# from /srv/safety/dev -- prod's .env pins COMPOSE_PROJECT_NAME so its compose
# commands keep finding the existing volume, and dev's does not.
set -euo pipefail

case "${1:-}" in
    prod) branch=main ;;
    dev)  branch=testing ;;
    *)    echo "usage: $0 prod|dev" >&2; exit 2 ;;
esac
instance=$1
target=/srv/safety/$instance
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)

git -C "$repo" fetch --quiet origin "$branch"
commit=$(git -C "$repo" rev-parse --short "origin/$branch")
echo "deploying origin/$branch ($commit) to $target"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
git -C "$repo" archive "origin/$branch" | tar -x -C "$stage"
echo "$commit" > "$stage/DEPLOYED_COMMIT"

sudo rsync -a --delete \
    --exclude /.venv --exclude /.env --exclude /data \
    "$stage/" "$target/"
sudo chown -R safety:safety "$target"

as_safety() { sudo -u safety env -C "$target" "$@"; }
as_safety .venv/bin/pip install --quiet -r requirements.txt
as_safety .venv/bin/python -m safety.migrate
sudo systemctl restart "safety-api@$instance"

port=$(sudo grep -E '^API_PORT=' "$target/.env" | cut -d= -f2)
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$port/api/v1/health" >/dev/null 2>&1; then
        echo "$instance is up on :$port at $commit"
        exit 0
    fi
    sleep 1
done
echo "$instance did not answer /api/v1/health within 30s; see: journalctl -u safety-api@$instance" >&2
exit 1
