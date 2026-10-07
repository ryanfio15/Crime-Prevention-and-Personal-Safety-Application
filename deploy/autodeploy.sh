#!/usr/bin/env bash
# Deploy one instance's branch from GitHub once CI has passed on it.
#
#   safety-autodeploy prod    # main    -> /srv/safety/Crime-Prevention-and-Personal-Safety-Application/prod
#   safety-autodeploy dev     # testing -> /srv/safety/Crime-Prevention-and-Personal-Safety-Application/dev
#
# Run every minute by safety-autodeploy@<instance>.timer, as root, from its
# installed copy /usr/local/sbin/safety-autodeploy (deploy/install-deployer.sh
# puts it there; editing this file changes nothing until that is re-run). It
# never executes anything from the repository: it fetches into a root-owned
# bare cache, extracts the commit and hands it to install.sh, which runs the
# repository's code as `safety`.
#
# Each tick: is the branch head new? Has the check run named `ci` (the job in
# .github/workflows/ci.yml) succeeded on it? Then deploy it. A head that fails
# CI, never gets CI, or fails to install is written to <instance>.skipped and
# left alone until something new is pushed -- there is no retry loop. Every
# decision is one journal line: journalctl -u safety-autodeploy@prod.
#
# State, all in /var/lib/safety-deploy (root, 0700):
#   repo.git                  bare cache of main and testing
#   <instance>.deployed       full sha last installed and seen healthy
#   <instance>.skipped        full sha given up on
#   <instance>.pending_since  "<sha> <epoch>": when that head was first seen with no CI
#   <instance>.checked_at     epoch of the last GitHub API call, for throttling
#   ratelimit_reset           epoch until which the API has told us to stop
#   deploy.lock               shared with install.sh and deploy/deploy.sh
set -euo pipefail

github_repo=ryanfio15/Crime-Prevention-and-Personal-Safety-Application
remote=https://github.com/$github_repo.git
state=/var/lib/safety-deploy
lock=$state/deploy.lock
cache=$state/repo.git
installer=/usr/local/lib/safety-deploy/install.sh

# Unauthenticated, the API allows 60 calls an hour per address, shared by both
# instances and anyone at a shell on this machine: one call per two minutes per
# instance leaves room. CI that never starts is given up on after half an hour.
check_interval=120
ci_wait=1800

case "${1:-}" in
    prod) branch=main ;;
    dev)  branch=testing ;;
    *)    echo "usage: $0 prod|dev" >&2; exit 2 ;;
esac
instance=$1
target=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance

# journald reads a leading <N> as the syslog priority, so failures show up in
# `journalctl -p err` without a logger dependency.
log()  { echo "$instance: $*"; }
warn() { echo "<4>$instance: $*" >&2; }
err()  { echo "<3>$instance: $*" >&2; }

# Before anything else, including the bootstrap seed. Wait for the lock rather
# than give up at once: both instances' timers share AccuracySec, so systemd
# fires them in the same instant, and with a bare `flock -n` the instance that
# loses that race loses it on every tick and never deploys. A deploy holds the
# lock for seconds to a few minutes; systemd will not start this unit again
# while a tick is still waiting. fd 9 stays open, and so the lock held, until
# this process exits.
exec 9>"$lock"
flock -w 600 9 || { log "another deploy has held the lock for 10 minutes; skipping this tick"; exit 0; }

ensure_cache() {
    if [ ! -d "$cache" ]; then
        git init --quiet --bare "$cache"
        git -C "$cache" remote add origin "$remote"
    fi
}

# First run for this instance: adopt whatever is already installed as the
# deployed commit, so the first tick deploys only a genuinely newer head
# (deploy/migrate-layout.sh normally seeds it already). Both branches are
# fetched because an instance may be running the other branch's commit (a
# fast-forward merge, a manual deploy). A flat checkout's DEPLOYED_COMMIT was
# short; rev-parse expands either.
if [ ! -f "$state/$instance.deployed" ]; then
    ensure_cache
    git -C "$cache" fetch --quiet origin \
        '+refs/heads/main:refs/remotes/origin/main' \
        '+refs/heads/testing:refs/remotes/origin/testing'
    installed=$(tr -d '[:space:]' 2>/dev/null < "$target/current/DEPLOYED_COMMIT") ||
        installed=$(tr -d '[:space:]' 2>/dev/null < "$target/DEPLOYED_COMMIT") || installed=
    if [[ ! $installed =~ ^[0-9a-f]{7,40}$ ]] ||
        ! full=$(git -C "$cache" rev-parse --quiet --verify "$installed^{commit}"); then
        err "cannot seed: DEPLOYED_COMMIT ('$installed') in $target is not a commit on main or testing; refusing to deploy unseeded"
        exit 1
    fi
    echo "$full" > "$state/$instance.deployed"
    log "seeded: $instance is running $full"
fi

if ! head=$(git ls-remote --exit-code "$remote" "refs/heads/$branch" | cut -f1) ||
    [[ ! $head =~ ^[0-9a-f]{40}$ ]]; then
    warn "could not read the head of $branch from GitHub; trying again next tick"
    exit 0
fi

deployed=$(cat "$state/$instance.deployed")
skipped=$(cat "$state/$instance.skipped" 2>/dev/null) || skipped=
if [ "$head" = "$deployed" ]; then
    log "up to date at $head"
    exit 0
fi
if [ "$head" = "$skipped" ]; then
    exit 0
fi

now=$(date +%s)
reset=$(cat "$state/ratelimit_reset" 2>/dev/null) || reset=0
if [[ $reset =~ ^[0-9]+$ ]] && [ "$now" -lt "$reset" ]; then
    log "pending: $head; GitHub API rate limited until $(date -d "@$reset" '+%H:%M:%S')"
    exit 0
fi
checked=$(cat "$state/$instance.checked_at" 2>/dev/null) || checked=0
if [[ $checked =~ ^[0-9]+$ ]] && [ $((now - checked)) -lt "$check_interval" ]; then
    exit 0
fi
echo "$now" > "$state/$instance.checked_at"

# filter=latest keeps only the newest run per name, so a re-run that passed
# supersedes the attempt that failed. The app check stops another integration
# from publishing a check called `ci` and deploying through it.
body=$(mktemp)
headers=$(mktemp)
stage=
cleanup() { rm -rf "$body" "$headers" ${stage:+"$stage"}; }
trap cleanup EXIT
code=$(curl -sS --max-time 20 -o "$body" -D "$headers" -w '%{http_code}' \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$github_repo/commits/$head/check-runs?check_name=ci&filter=latest") || code=000
if [ "$code" != 200 ]; then
    next=$(tr -d '\r' < "$headers" | awk -F': ' 'tolower($1) == "x-ratelimit-reset" { print $2 }')
    if [[ $next =~ ^[0-9]+$ ]]; then
        echo "$next" > "$state/ratelimit_reset"
    fi
    log "pending: $head; GitHub API answered HTTP $code"
    exit 0
fi

read -r count status conclusion < <(jq -r '
    [.check_runs[] | select(.app.slug == "github-actions")] as $runs
    | "\($runs | length) \($runs[0].status // "-") \($runs[0].conclusion // "-")"' "$body")

if [ "$count" = 0 ]; then
    first=$(awk -v h="$head" '$1 == h { print $2 }' "$state/$instance.pending_since" 2>/dev/null) || first=
    if [[ ! $first =~ ^[0-9]+$ ]]; then
        first=$now
        echo "$head $now" > "$state/$instance.pending_since"
    fi
    if [ $((now - first)) -ge "$ci_wait" ]; then
        echo "$head" > "$state/$instance.skipped"
        err "skipped $head: no ci check run appeared within $((ci_wait / 60)) minutes; push again or re-run CI and rm $state/$instance.skipped"
        exit 1
    fi
    log "pending: $head; waiting for CI to start"
    exit 0
fi
if [ "$status" != completed ]; then
    log "pending: $head; CI is $status"
    exit 0
fi
if [ "$conclusion" != success ]; then
    echo "$head" > "$state/$instance.skipped"
    err "skipped $head: CI concluded $conclusion"
    exit 1
fi

log "deploying $head (CI passed)"
ensure_cache
git -C "$cache" fetch --quiet origin "+refs/heads/$branch:refs/remotes/origin/$branch"
if ! git -C "$cache" cat-file -e "$head^{commit}" 2>/dev/null; then
    # The branch moved between ls-remote and fetch; the next tick sees the new head.
    warn "$head not found after fetching $branch; trying again next tick"
    exit 0
fi
stage=$(mktemp -d "$state/stage.XXXXXX")
git -C "$cache" archive "$head" | tar -x -C "$stage"

rc=0
SAFETY_DEPLOY_LOCK_HELD=1 "$installer" "$instance" "$stage" "$head" || rc=$?
case $rc in
    0)  log "deployed $head" ;;
    75) log "deferred $head: ETL running on $instance; trying again next tick" ;;
    *)  echo "$head" > "$state/$instance.skipped"
        err "skipped $head: install failed (exit $rc); $instance is on its previous release (rolled back if it got as far as switching), see the lines above"
        exit 1 ;;
esac
