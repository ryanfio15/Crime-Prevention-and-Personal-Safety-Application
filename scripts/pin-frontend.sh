#!/usr/bin/env bash
# Re-pin a dev-only front end (/classic/ or /testing/) to a version from git.
#
#   scripts/pin-frontend.sh <classic|testing> <ref> [source-dir]
#
# Replaces web-<name>/ with <source-dir> exactly as it was at <ref> (a commit,
# tag or branch). The default source is web/ for classic -- the main site at
# that commit -- and web-testing/ for testing, its own history. Examples:
#
#   scripts/pin-frontend.sh classic ui-classic        # the original UI
#   scripts/pin-frontend.sh classic f1f043b           # main site at a commit
#   scripts/pin-frontend.sh testing 0dd0b83           # roll the testing UI back
#   scripts/pin-frontend.sh testing HEAD web          # testing UI := main site
#
# The copy's service worker is then given its own cache name and prefix, which
# tests/test_extra_frontends.py requires: three workers share one origin, and a
# copy left with the main site's names would delete the main site's cache.
#
# Stages the result and stops. Review with `git diff --cached --stat`, then
# commit and push to testing; the copy deploys with everything else.
set -euo pipefail

usage() { sed -n '2,5p' "$0" >&2; exit 2; }

[[ $# -ge 2 && $# -le 3 ]] || usage
name=$1
ref=$2
case $name in
    classic) default_src=web ;;
    testing) default_src=web-testing ;;
    *) echo "unknown front end '$name' (expected classic or testing)" >&2; exit 2 ;;
esac
src=${3:-$default_src}
dest=web-$name

cd "$(git rev-parse --show-toplevel)"

sha=$(git rev-parse --verify --quiet "$ref^{commit}") \
    || { echo "no such commit: $ref" >&2; exit 1; }
git cat-file -e "$sha:$src" 2>/dev/null \
    || { echo "$src/ does not exist at $ref" >&2; exit 1; }
git cat-file -e "$sha:$src/sw.js" 2>/dev/null \
    || { echo "$src/ at $ref has no sw.js; refusing a copy without its own cache" >&2; exit 1; }

# Extract beside the target first, so a failure leaves the current copy alone.
tmp=$(mktemp -d "./.pin-$name.XXXXXX")
chmod 755 "$tmp"  # mktemp makes it 0700; this becomes web-<name>/
trap 'rm -rf "$tmp"' EXIT
git archive "$sha:$src" | tar -x -C "$tmp"

sw="$tmp/sw.js"
grep -q '^const CACHE_NAME = "' "$sw" && grep -q '^const CACHE_PREFIX = "' "$sw" \
    || { echo "$src/sw.js at $ref has no CACHE_NAME / CACHE_PREFIX lines to rename" >&2; exit 1; }
sed -i \
    -e "s/^const CACHE_NAME = \".*\";/const CACHE_NAME = \"safety-app-$name-shell-v1\";/" \
    -e "s/^const CACHE_PREFIX = \".*\";/const CACHE_PREFIX = \"safety-app-$name-\";/" \
    "$sw"

rm -rf "$dest"
mv "$tmp" "$dest"
trap - EXIT
git add -A "$dest"

short=$(git rev-parse --short "$sha")
echo "$dest/ is now $src/ at $short ($ref)."
if git diff --cached --quiet -- "$dest"; then
    echo "No changes: it was already that version."
else
    git diff --cached --stat -- "$dest" | tail -1
    echo "Staged. Commit with: git commit -m \"${name^} UI: pin to $short\""
fi
