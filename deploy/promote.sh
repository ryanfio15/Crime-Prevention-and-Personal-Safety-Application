#!/usr/bin/env bash
# Send what dev is running to prod: fast-forward main to origin/testing, once
# CI has passed on it. The push to main is what makes prod deploy
# (deploy/autodeploy.sh); prod runs the same candidate and rollback checks dev
# did. Then brings the main worktree (deploy/setup-worktrees.sh) up to date.
#
#   deploy/promote.sh            # from either worktree
set -euo pipefail

repo_url=https://api.github.com/repos/ryanfio15/Crime-Prevention-and-Personal-Safety-Application
git fetch --quiet origin
testing=$(git rev-parse origin/testing)
main=$(git rev-parse origin/main)

if [ "$testing" = "$main" ]; then
    echo "main is already at testing (${testing:0:7}); nothing to promote"
    exit 0
fi
if ! git merge-base --is-ancestor "$main" "$testing"; then
    echo "origin/main (${main:0:7}) is not an ancestor of origin/testing (${testing:0:7})." >&2
    echo "main has commits testing lacks; merge main into testing, push, and try again" >&2
    exit 1
fi

conclusion=$(curl -fsS -H 'Accept: application/vnd.github+json' \
    "$repo_url/commits/$testing/check-runs?check_name=ci&filter=latest" |
    jq -r '[.check_runs[] | select(.app.slug == "github-actions")][0].conclusion // "pending"')
if [ "$conclusion" != success ]; then
    echo "CI on ${testing:0:7} is '$conclusion', not success; not promoting" >&2
    exit 1
fi

echo "promoting ${testing:0:7} to main:"
git log --oneline "$main..$testing"
git push origin "$testing:refs/heads/main"

main_wt=$(git worktree list --porcelain | awk '/^worktree /{wt=$2} /^branch refs\/heads\/main$/{print wt}')
if [ -n "$main_wt" ]; then
    git -C "$main_wt" merge --quiet --ff-only origin/main
    echo "updated $main_wt"
fi
echo "prod deploys ${testing:0:7} within a few minutes: journalctl -u safety-autodeploy@prod -f"
