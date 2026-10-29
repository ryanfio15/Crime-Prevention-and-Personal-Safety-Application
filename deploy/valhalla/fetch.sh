#!/usr/bin/env bash
# Install the newest Valhalla routing tiles on this host:
#
#   sudo /usr/local/lib/safety-deploy/valhalla/fetch.sh
#
# Run weekly by safety-valhalla-fetch.timer, as root (it writes /srv/valhalla
# and talks to Docker). The tiles are built elsewhere -- the valhalla-tiles
# GitHub workflow runs deploy/valhalla/build.sh monthly and publishes a release
# tagged valhalla-tiles-YYYYMMDD -- because a build needs ~4 GB RAM and
# ~6.5 GB disk this host cannot spare. Installing one needs ~1.2 GB and no
# build at all.
#
#   1. Ask the GitHub API for the newest valhalla-tiles-* release published by
#      the workflow (author github-actions[bot]; a release a person made by
#      hand is ignored). If it is already what /srv/valhalla/current points at,
#      exit: most weeks this is one API call. A newest release older than 45
#      days is logged as a warning -- the monthly build has stopped (GitHub
#      disables scheduled workflows after 60 days without repo activity).
#   2. Preflight: a lock (one fetch at a time), /srv/valhalla owned by root,
#      and at least MIN_FREE_GB free.
#   3. Download the three assets into build-<tag>.partial/ and check them
#      against the release's SHA256SUMS.
#   4. Serve them from a candidate container on 127.0.0.1:58003, confined and
#      limited like the live one, and route every city's smoke trip by car,
#      foot and bike (smoke.sh); one retry after a restart, so a slow cold
#      start is not mistaken for a bad build. Nothing live has changed yet, so
#      a bad build costs nothing but the download.
#   5. Swap the `current` symlink atomically, `docker restart safety_valhalla`
#      (a bind mount resolves the symlink only at start), smoke the live port
#      and check it reports the new tileset. If that fails, the symlink goes
#      back, the container is restarted on the old build and the new build is
#      deleted.
#   6. Keep KEEP_PREVIOUS earlier builds for a manual rollback (default 1; 0
#      keeps none and so allows no rollback); delete the rest.
#
# A release that fails either smoke test is appended to /srv/valhalla/rejected
# and not tried again (the run fails, so the OnFailure alert fires once, not
# every week with a 1.2 GB download and a restart each time); FORCE=1 retries
# it. The next good release supersedes it on its own.
#
# A deliberate rollback (docs/OPERATIONS.md, "Valhalla routing tiles") creates
# /srv/valhalla/hold, which stops this script replacing the build until the
# file is removed -- otherwise next week's run would undo the rollback.
#
# If safety_valhalla does not exist yet (first install), the build is still
# installed and the script says how to start the container. Valhalla is not
# part of /health or a deploy's smoke test, so none of this can block a deploy.
#
# Settings (environment): VALHALLA_ROOT (/srv/valhalla), VALHALLA_REPO
# (owner/name on GitHub), KEEP_PREVIOUS (1), MIN_FREE_GB (3), FORCE=1 to
# re-install the current tag or retry a rejected one. The live port is read
# from the container itself (`docker port`), so a VALHALLA_PORT set only in
# compose's .env is still found.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=${VALHALLA_ROOT:-/srv/valhalla}
repo=${VALHALLA_REPO:-ryanfio15/Crime-Prevention-and-Personal-Safety-Application}
keep_previous=${KEEP_PREVIOUS:-1}
min_free_gb=${MIN_FREE_GB:-3}
force=${FORCE:-0}
max_age_days=45
container=safety_valhalla
candidate=safety_valhalla_candidate
candidate_port=58003
# Used only when safety_valhalla does not exist yet; otherwise the candidate
# runs the exact image the live container runs. Keep in step with
# docker-compose.yml.
default_image=ghcr.io/valhalla/valhalla:3.9.1@sha256:4fd114eb1d26b8fc2f43bcb3df234c1b63a0bf13c277405e5943d5361dc575de
assets=(valhalla_tiles.tar valhalla.json SHA256SUMS)

log() { echo "valhalla-fetch: $*"; }
warn() { echo "valhalla-fetch: WARNING: $*" >&2; }
die() { echo "valhalla-fetch: ERROR: $*" >&2; exit 1; }

for cmd in curl docker python3 sha256sum flock; do
    command -v "$cmd" > /dev/null || die "$cmd is not installed"
done
case $keep_previous in
    '' | *[!0-9]*) die "KEEP_PREVIOUS must be a whole number, not '$keep_previous'" ;;
esac

# Root runs everything below on what this directory holds -- including tiles a
# root-run container serves -- so nobody else may be able to plant files here.
install -d -o root -g root -m 0755 "$root"
[ "$(stat -c %u "$root")" = 0 ] || die "$root is not owned by root; refusing to use it"
[ -z "$(find "$root" -maxdepth 0 -perm /022)" ] ||
    die "$root is group- or world-writable; refusing to use it (chmod 0755 $root)"
exec 9> "$root/.fetch.lock"
if ! flock -n 9; then
    log "another fetch is running; nothing to do"
    exit 0
fi

partial=""
cleanup() {
    local rc=$?
    docker rm -f "$candidate" > /dev/null 2>&1 || true
    if [ -n "$partial" ]; then rm -rf "$partial"; fi
    if [ "$rc" -ne 0 ]; then log "failed (exit $rc); the live build is unchanged unless stated above"; fi
}
trap cleanup EXIT
# Leftovers of a run that was killed: the lock is ours, so none is in use.
rm -rf "$root"/build-*.partial

if [ -e "$root/hold" ]; then
    log "$root/hold exists (a deliberate rollback); not replacing $(readlink "$root/current" || echo 'the current build')"
    exit 0
fi

# `docker compose up` before the first fetch on an older compose file (short
# volume syntax) makes Docker create `current` as an empty directory, which
# `mv -T` cannot replace with the symlink. Empty, it is safe to remove;
# anything else is someone's data. Checked before any download, so a
# problem here costs nothing.
if [ -d "$root/current" ] && [ ! -L "$root/current" ]; then
    rmdir "$root/current" 2> /dev/null ||
        die "$root/current is a non-empty directory, not a symlink; move it aside and re-run"
    log "removed the empty directory $root/current (left by an early docker compose up)"
fi

# --- 1. newest release ---------------------------------------------------------
# Unauthenticated (the repo is public): 60 API calls an hour per IP is plenty
# for a weekly timer. Newest by publication time, not tag name, so a same-day
# re-run tagged valhalla-tiles-YYYYMMDD-N still wins. Only releases the
# workflow created count (author github-actions[bot]): anyone else with push
# access could otherwise publish tiles this root job installs. A release
# missing any of the three files is skipped -- the workflow uploads them as it
# creates it, so a half-made one is still in progress or failed.
releases=$(curl -fsS --max-time 30 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$repo/releases?per_page=30") ||
    die "could not list releases of $repo"
release=$(printf '%s' "$releases" | python3 -I -c '
import datetime, json, re, sys
want = sys.argv[1:]
best = None
for r in json.load(sys.stdin):
    tag = r.get("tag_name") or ""
    if r.get("draft") or r.get("prerelease"):
        continue
    if (r.get("author") or {}).get("login") != "github-actions[bot]":
        continue
    if not re.fullmatch(r"valhalla-tiles-[0-9A-Za-z.-]+", tag):
        continue
    urls = {a.get("name"): a.get("browser_download_url") or "" for a in r.get("assets", [])}
    if not all(urls.get(n, "").startswith("https://github.com/") for n in want):
        continue
    if best is None or (r.get("published_at") or "") > (best[0].get("published_at") or ""):
        best = (r, urls)
if best:
    published = best[0].get("published_at") or ""
    try:
        when = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - when).days
    except ValueError:
        age = -1
    print("\t".join([best[0]["tag_name"], str(age)] + [best[1][n] for n in want]))
' "${assets[@]}")
[ -n "$release" ] || die "no complete valhalla-tiles-* release by github-actions[bot] on $repo"
IFS=$'\t' read -r tag age_days tar_url json_url sums_url <<< "$release"
if [ "$age_days" -gt "$max_age_days" ]; then
    warn "the newest tiles ($tag) are $age_days days old: the monthly valhalla-tiles workflow has stopped publishing (GitHub disables scheduled workflows after 60 days without repo activity; re-enable it in the Actions tab)"
fi
dir=$root/build-$tag
current=$(readlink "$root/current" || true)

if [ "$current" = "build-$tag" ] && [ "$force" != 1 ]; then
    log "$tag is already installed"
    exit 0
fi
if [ "$force" != 1 ] && grep -qxF "$tag" "$root/rejected" 2> /dev/null; then
    log "$tag was rejected by an earlier run (see $root/rejected); still serving ${current:-nothing}"
    log "a newer release will be picked up on its own; FORCE=1 retries this one"
    exit 0
fi
log "newest release $tag (installed: ${current:-none})"

# FORCE=1 on the tag that is already live: a failure must never reject or
# delete the build that is serving (there is nothing to roll back to).
reinstall=""
if [ "$current" = "build-$tag" ]; then reinstall=1; fi
reject() {
    if [ -z "$reinstall" ]; then printf '%s\n' "$tag" >> "$root/rejected"; fi
}

# --- 2-3. download and verify --------------------------------------------------
if [ -d "$dir" ]; then
    # Installed before and rolled away from (or FORCE=1): the files are here.
    src=$dir
    log "reusing $dir"
else
    free_gb=$(df -P --block-size=1G "$root" | awk 'NR==2 {print $4}')
    [ "$free_gb" -ge "$min_free_gb" ] ||
        die "only ${free_gb} GB free on $root, need $min_free_gb; prune old builds or free space"
    partial=$dir.partial
    src=$partial
    install -d -m 0755 "$partial"
    log "downloading $tag"
    curl -fsSL --retry 3 --retry-delay 10 -o "$partial/valhalla_tiles.tar" "$tar_url"
    curl -fsSL --retry 3 -o "$partial/valhalla.json" "$json_url"
    curl -fsSL --retry 3 -o "$partial/SHA256SUMS" "$sums_url"
fi
# Both files must be listed: sha256sum -c only checks what the list names.
for f in valhalla_tiles.tar valhalla.json; do
    grep -qE "^[0-9a-f]{64} [ *]$f\$" "$src/SHA256SUMS" || die "SHA256SUMS does not list $f"
done
(cd "$src" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch in $tag"
# The service runs as nobody (docker-compose.yml), so the tiles must be
# world-readable whatever umask they were downloaded under.
chmod 0755 "$src"
chmod 0644 "$src"/*
log "checksums ok ($(du -sh "$src" | cut -f1))"

# --- 4. candidate smoke test ---------------------------------------------------
image=$(docker inspect -f '{{.Config.Image}}' "$container" 2> /dev/null || true)
image=${image:-$default_image}
docker rm -f "$candidate" > /dev/null 2>&1 || true
# The live container's user, confinement and limits (docker-compose.yml), so a
# build that only works as root, writable, or unconstrained fails here.
docker run -d --name "$candidate" \
    --user 65534:65534 --read-only --tmpfs /tmp:size=64m,mode=1777 \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256 \
    --memory 1536m --cpus 1.5 \
    -p "127.0.0.1:$candidate_port:8002" -v "$src:/data:ro" \
    "$image" valhalla_service /data/valhalla.json 1 > /dev/null
if ! "$here/smoke.sh" "$candidate_port" "$here/regions.tsv"; then
    log "candidate failed its smoke test; restarting it and trying once more"
    if ! docker restart -t 5 "$candidate" > /dev/null ||
        ! "$here/smoke.sh" "$candidate_port" "$here/regions.tsv"; then
        docker logs --tail 40 "$candidate" 2>&1 || true
        reject
        die "candidate $tag failed its smoke test twice; still serving ${current:-nothing}"
    fi
fi
docker rm -f "$candidate" > /dev/null

if [ -n "$partial" ]; then
    mv -T "$partial" "$dir"
    partial=""
fi

# --- 5. swap -------------------------------------------------------------------
# A relative target so the tree can be moved; rename(2) over the old link is
# atomic, so there is never a moment with no `current`.
swap_to() {
    ln -sfn "$1" "$root/.current.new"
    mv -Tf "$root/.current.new" "$root/current"
}
# The port compose published, whatever VALHALLA_PORT it was started with; the
# default if the container does not say.
live_port() {
    local p
    p=$(docker port "$container" 8002/tcp 2> /dev/null | head -n 1 || true)
    p=${p##*:}
    case $p in '' | *[!0-9]*) p=58002 ;; esac
    printf '%s' "$p"
}
# /status reports the tar's mtime; matching it to the file on disk proves the
# restarted container mounted the new build, not a cached path to the old one.
# An unanswered /status, or one without the field, is a failure too: without
# it nothing proves which build is live.
serves() {   # serves <dir> <port>
    local want got
    want=$(stat -c %Y "$1/valhalla_tiles.tar")
    got=$(curl -fsS --max-time 5 "http://127.0.0.1:$2/status" |
        python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("tileset_last_modified", ""))' || true)
    if [ "$got" != "$want" ]; then
        log "live /status reports tileset_last_modified '${got:-none}', expected $want"
        return 1
    fi
}

swap_to "build-$tag"
log "current -> build-$tag"

running=$(docker inspect -f '{{.State.Running}}' "$container" 2> /dev/null || true)
case $running in
    true)
        port=$(live_port)
        if ! docker restart -t 30 "$container" > /dev/null ||
            ! "$here/smoke.sh" "$port" "$here/regions.tsv" || ! serves "$dir" "$port"; then
            if [ -n "$reinstall" ]; then
                # FORCE=1 of the build that was already live: it stays current,
                # is not rejected and is not deleted.
                die "FORCE re-install of the live build $tag failed its live check; it is still current and was not deleted (sudo docker logs --tail 50 $container)"
            fi
            reject
            if [ -n "$current" ] && [ -d "$root/$current" ]; then
                swap_to "$current"
                if ! docker restart -t 30 "$container" > /dev/null; then
                    die "live check failed on $tag; current is back on $current but restarting $container failed too -- start it by hand. build-$tag kept for inspection"
                fi
                # Recorded as rejected, so nothing will look for it again.
                rm -rf "$dir"
                die "live smoke test failed on $tag; rolled back to $current and deleted build-$tag"
            fi
            die "live smoke test failed on $tag and there is no previous build to return to"
        fi
        log "$container is serving $tag on 127.0.0.1:$port"
        ;;
    false)
        log "$container is stopped; not starting it. It will serve $tag when started."
        ;;
    *)
        log "$container does not exist yet. Start it once with (docs/DEPLOYMENT.md, step 12):"
        log "  sudo sh -c 'cd <prod release> && docker compose --profile routing up -d valhalla'"
        ;;
esac

# --- 6. prune ------------------------------------------------------------------
# The build that was live until now is the rollback target, whatever its
# mtime; any further slots go to the newest of the rest. With KEEP_PREVIOUS=0
# it is deleted too.
kept=0 kept_current=""
if [ "$keep_previous" -gt 0 ] && [ -n "$current" ] && [ "$current" != "build-$tag" ] &&
    [ -d "$root/$current" ]; then
    kept=1 kept_current=$current
    log "keeping $current for rollback"
fi
while IFS= read -r old; do
    name=${old##*/}
    [ "$name" = "build-$tag" ] && continue
    [ -n "$kept_current" ] && [ "$name" = "$kept_current" ] && continue
    if [ "$kept" -lt "$keep_previous" ]; then
        kept=$((kept + 1))
        log "keeping $name for rollback"
        continue
    fi
    log "removing $name"
    rm -rf "$old"
done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -name 'build-*' ! -name '*.partial' \
    -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)

log "done: $(du -sh "$root" | cut -f1) under $root, $(df -P -h "$root" | awk 'NR==2 {print $4}') free"
