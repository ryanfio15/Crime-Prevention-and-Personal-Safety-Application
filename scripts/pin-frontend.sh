#!/usr/bin/env bash
# Rebuild the frozen dev-only front end served at /dev/ (web-dev/).
#
#   scripts/pin-frontend.sh dev [ref] [source-dir]
#
# Replaces web-dev/ with <source-dir> (default web/) exactly as it was at <ref>
# (default the ui-dev tag: the main site before the map-only redesign). With no
# arguments beyond "dev" it reproduces the committed copy byte for byte, which
# is how to undo an accidental edit:
#
#   scripts/pin-frontend.sh dev
#
# web-dev/ is meant never to change; tests/test_dev_frontend.py pins every
# file's hash and fails CI on any difference. Pinning a different ref is a
# deliberate change and needs that test's hashes updated in the same commit.
#
# The copy's service worker is then given its own cache name and prefix, which
# tests/test_extra_frontends.py requires: two workers share one origin, and a
# copy left with the main site's names would delete the main site's cache.
#
# Stages the result and stops. Review with `git diff --cached --stat`.
set -euo pipefail

usage() { sed -n '2,4p' "$0" >&2; exit 2; }

[[ $# -ge 1 && $# -le 3 ]] || usage
name=$1
[[ $name == dev ]] || { echo "unknown front end '$name' (expected dev)" >&2; exit 2; }
ref=${2:-ui-dev}
src=${3:-web}
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
