#!/usr/bin/env bash
# Install an already-extracted tree into one instance and prove it is serving.
#
#   install.sh prod|dev <stage_dir> <full_sha>
#
# The one place the install steps live: deploy/deploy.sh (a human, via sudo)
# and deploy/autodeploy.sh (the safety-autodeploy@ timer) both end here. It
# runs as root from its installed copy, /usr/local/lib/safety-deploy/install.sh
# (deploy/install-deployer.sh puts it there), never from an instance tree.
#
# Root only moves files and calls systemctl. Everything that executes repository
# code -- pip resolving requirements.txt, safety.migrate -- runs as `safety`, so
# a bad commit can do no more than the API itself already could.
#
# Exit status: 0 deployed and healthy; 75 refused because an ETL run is using
# the instance (callers treat that as "try again later"); anything else failed,
# possibly half way -- see docs/DEPLOY.md, "Continuous deployment".
set -euo pipefail

state=/var/lib/safety-deploy
lock=$state/deploy.lock

# One deploy at a time across both instances: they share a database server and
# the bare cache. autodeploy.sh takes the lock itself before it even looks at
# GitHub and says so through the environment; sudo resets the environment, so a
# manual run always queues here instead.
[ "${SAFETY_DEPLOY_LOCK_HELD:-}" = 1 ] ||
    exec env SAFETY_DEPLOY_LOCK_HELD=1 flock -w 900 "$lock" "$0" "$@"

case "${1:-}" in
    # Hardcoded rather than read from .env: root does not parse a file the
    # `safety` user can write. Must match API_PORT in each instance's .env.
    prod) port=8000 ;;
    dev)  port=8001 ;;
    *)    echo "usage: $0 prod|dev <stage_dir> <full_sha>" >&2; exit 2 ;;
esac
instance=$1
stage=${2:?usage: $0 prod|dev <stage_dir> <full_sha>}
sha=${3:?usage: $0 prod|dev <stage_dir> <full_sha>}
target=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance

[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "not a full sha: $sha" >&2; exit 2; }
[ -d "$stage" ] || { echo "no such stage directory: $stage" >&2; exit 2; }

# Swapping the tree under a running pull would mix two versions of the code in
# one process, and migrate could block on its locks. Refuse and let the caller
# come back on its next tick.
if systemctl is-active --quiet "safety-etl@$instance" "safety-etl-hourly@$instance"; then
    echo "ETL is running on $instance; not deploying now" >&2
    exit 75
fi

echo "installing $sha into $target"
rsync -a --delete \
    --exclude /.venv --exclude /.env --exclude /data \
    "$stage/" "$target/"
echo "$sha" > "$target/DEPLOYED_COMMIT"
chown -R safety:safety "$target"

# HOME so pip's cache lands in /srv/safety/.cache, not root's home.
as_safety() { runuser -u safety -- env -C "$target" HOME=/srv/safety "$@"; }
as_safety .venv/bin/pip install --quiet -r requirements.txt
# lock_timeout: a migration waiting behind a long query fails after 30s instead
# of queueing every API request behind its own lock request.
as_safety PGOPTIONS='-c lock_timeout=30s' .venv/bin/python -m safety.migrate

systemctl restart "safety-api@$instance"

# Healthy means this exact commit answered, not merely that something did: the
# health endpoint reports DEPLOYED_COMMIT as read at startup.
for _ in $(seq 1 60); do
    served=$(curl -fsS --max-time 5 "http://127.0.0.1:$port/api/v1/health" 2>/dev/null |
        jq -r '.commit // empty' 2>/dev/null) || served=
    if [ "$served" = "$sha" ]; then
        echo "$sha" > "$state/$instance.deployed"
        echo "$instance is up on :$port at $sha"
        exit 0
    fi
    sleep 1
done
echo "$instance did not report $sha on /api/v1/health within 60s; see: journalctl -u safety-api@$instance" >&2
exit 1
