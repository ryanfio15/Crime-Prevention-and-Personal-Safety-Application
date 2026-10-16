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
# A PR merged on GitHub leaves a merge commit on main that testing never gets,
# after which main cannot fast-forward. Bring it into testing first; that push
# deploys dev and runs CI, and the next promote goes through.
if ! git merge-base --is-ancestor "$main" "$testing"; then
    testing_wt=$(git worktree list --porcelain | awk '/^worktree /{wt=$2} /^branch refs\/heads\/testing$/{print wt}')
    if [ -z "$testing_wt" ] || [ -n "$(git -C "$testing_wt" status --porcelain)" ] ||
        [ "$(git -C "$testing_wt" rev-parse HEAD)" != "$testing" ]; then
        echo "main has commits testing lacks, and the testing worktree is not a clean copy of origin/testing;" >&2
        echo "merge origin/main into testing yourself, push, and promote again" >&2
        exit 1
    fi
    echo "main has commits testing lacks (e.g. a PR merge); merging origin/main into testing first"
    git -C "$testing_wt" merge --no-edit origin/main
    git -C "$testing_wt" push origin testing
    echo "pushed testing; once CI passes on it (about a minute), run deploy/promote.sh again"
    exit 0
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
