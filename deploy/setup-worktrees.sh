#!/usr/bin/env bash
# One clone, two working folders, each pinned to the branch its instance runs:
#
#   ~/Crime-Prevention-and-Personal-Safety-Application        testing -> dev
#   ~/Crime-Prevention-and-Personal-Safety-Application-main   main    -> prod (read-only view)
#
# Git refuses to check one branch out in two worktrees, so main can never be
# checked out in the testing folder or the other way round. The hooks in
# deploy/git-hooks/ go into the clone's shared hooks directory, so they guard
# both folders: no commits or merges on main, and pushes to main only as a
# fast-forward to origin/testing (deploy/promote.sh). Safe to re-run; re-run
# after changing a hook.
#
#   deploy/setup-worktrees.sh            # run from the testing folder
set -euo pipefail

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
main_wt=${MAIN_WORKTREE:-$repo-main}
hooks=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)/hooks

git -C "$repo" fetch --quiet origin
if [ "$(git -C "$repo" symbolic-ref --short HEAD)" != testing ]; then
    echo "run this from a clean checkout of testing ($repo is on $(git -C "$repo" symbolic-ref --short HEAD))" >&2
    exit 1
fi

if git -C "$repo" worktree list --porcelain | grep -qx "worktree $main_wt"; then
    echo "main worktree already at $main_wt"
else
    git -C "$repo" worktree add --quiet "$main_wt" main
    echo "added main worktree at $main_wt"
fi
git -C "$main_wt" merge --quiet --ff-only origin/main
git -C "$repo" branch --quiet --set-upstream-to=origin/main main
git -C "$repo" config branch.main.mergeoptions --ff-only

install -d "$hooks"
for hook in "$repo"/deploy/git-hooks/*; do
    install -m 0755 "$hook" "$hooks/$(basename "$hook")"
done
echo "installed hooks: $(cd "$repo/deploy/git-hooks" && echo *)"
git -C "$repo" worktree list
