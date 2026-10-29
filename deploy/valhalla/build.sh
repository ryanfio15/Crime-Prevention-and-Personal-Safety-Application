#!/usr/bin/env bash
# Build the Valhalla routing tiles for every city in regions.tsv:
#
#   deploy/valhalla/build.sh
#
# Writes $OUT_DIR (default ./valhalla-out) with exactly the three files a host
# installs (deploy/valhalla/fetch.sh):
#   valhalla_tiles.tar   every tile in one tar, which valhalla_service mmaps
#   valhalla.json        the service config; its paths are /data/..., the
#                        mount point in docker-compose.yml
#   SHA256SUMS           checksums of the two, verified by fetch.sh
#
# Where it runs: the valhalla-tiles GitHub workflow, monthly. Not on the home
# server -- measured for six cities, osmium's clip peaks at ~4 GB RAM per
# state, valhalla_build_tiles at 3.6 GB, and the work directory at ~4.1 GB
# (~6.5 GB of disk in all), which a 7.6 GB, 87%-full host cannot spare beside
# the database. It also runs on any workstation with Docker, osmium-tool and
# 8+ GB RAM, for a first build or a test:
#   OUT_DIR=/tmp/vt deploy/valhalla/build.sh
#
# Steps:
#   1. For each Geofabrik state, one at a time so only one is ever on disk:
#      download it, check its MD5, clip it to the padded bbox of every city
#      that needs it, delete it. `-s smart -S types=multipolygon,boundary`
#      keeps complete admin polygons -- valhalla_build_admins needs closed
#      boundaries to assign country/state rules (driving side, access).
#   2. osmium merge the clips into one PBF.
#   3. In the pinned image (the same digest docker-compose.yml serves):
#      valhalla_build_config, _timezones, _admins, _tiles, _extract. The
#      container runs as the invoking user so nothing it writes is root-owned.
#   4. Delete everything but the tar and config, serve them on 127.0.0.1:58003
#      and route every city's smoke trip by car, foot and bike (smoke.sh). A
#      build that cannot route all of them is not written to $OUT_DIR.
#
# Settings (environment):
#   OUT_DIR          where the three files go (default ./valhalla-out)
#   WORK_DIR         parent for scratch, ~6.5 GB at peak (default
#                    ./valhalla-work). The build works in its own new
#                    mktemp -d subdirectory and removes only that on exit
#                    (unless KEEP_WORK=1), so it never deletes anything the
#                    caller already had there
#   CITIES_URL       optional: an /api/v1/cities URL; any city whose live bbox
#                    is not inside its regions.tsv bbox is warned about
#   BUILD_MEMORY     container memory cap for the tile build (default 6g;
#                    measured peak 3.6 GB)
#   BUILD_JOBS       valhalla_build_tiles threads (default 2, as measured)
#   SMOKE_PORT       candidate port (default 58003)
#   MIN_FREE_GB      refuse to start below this much free disk (default 8)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
regions=${REGIONS:-$here/regions.tsv}
out=${OUT_DIR:-$PWD/valhalla-out}
work_root=${WORK_DIR:-$PWD/valhalla-work}
# Keep in step with docker-compose.yml's valhalla image: tiles are read by the
# version that wrote them.
image=${VALHALLA_IMAGE:-ghcr.io/valhalla/valhalla:3.9.1@sha256:4fd114eb1d26b8fc2f43bcb3df234c1b63a0bf13c277405e5943d5361dc575de}
geofabrik=${GEOFABRIK_BASE:-https://download.geofabrik.de}
build_memory=${BUILD_MEMORY:-6g}
build_jobs=${BUILD_JOBS:-2}
smoke_port=${SMOKE_PORT:-58003}
min_free_gb=${MIN_FREE_GB:-8}
cities_url=${CITIES_URL:-}
keep_work=${KEEP_WORK:-0}
smoke_name=valhalla-build-smoke-$$
# GitHub's limit per release asset is 2 GiB; the tar was 1.15 GB in Oct 2026.
asset_warn_bytes=$((1800 * 1000 * 1000))
asset_max_bytes=$((2 * 1024 * 1024 * 1024))

log() { echo "valhalla-build: $*"; }
# In GitHub Actions a warning also becomes an annotation on the run page. Some
# warnings carry text from the cities API, so the characters that could end
# the annotation or start another workflow command (`:` and `%`, CR, LF) are
# stripped first.
warn() {
    local msg=$*
    msg=${msg//[$'\r\n']/ }
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::warning title=valhalla-build::${msg//[:%]/}"
    fi
    echo "valhalla-build: WARNING: $msg" >&2
}
die() { echo "valhalla-build: ERROR: $*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
for cmd in osmium docker curl python3 md5sum sha256sum flock; do
    command -v "$cmd" > /dev/null || die "$cmd is not installed (osmium: apt install osmium-tool)"
done
[ -r "$regions" ] || die "no regions file at $regions"

mkdir -p "$work_root"
work_root=$(cd "$work_root" && pwd)
# One build per WORK_DIR at a time: each would fit the free-space check alone
# and not together.
exec 9> "$work_root.lock"
flock -n 9 || die "another build holds $work_root.lock"

free_gb=$(df -P --block-size=1G "$work_root" | awk 'NR==2 {print $4}')
[ "$free_gb" -ge "$min_free_gb" ] ||
    die "only ${free_gb} GB free under $work_root, need $min_free_gb (peak is ~6.5 GB)"

work=$(mktemp -d "$work_root/build.XXXXXX")
cleanup() {
    local rc=$?
    docker rm -f "$smoke_name" > /dev/null 2>&1 || true
    # Only the directory mktemp made for this run, then WORK_DIR itself if
    # that leaves it empty.
    if [ "$keep_work" = 1 ]; then
        log "work directory kept: $work"
    else
        rm -rf "$work"
        rmdir "$work_root" 2> /dev/null || true
    fi
    if [ "$rc" -ne 0 ]; then log "failed (exit $rc)"; fi
}
trap cleanup EXIT

# --- regions -----------------------------------------------------------------
declare -a cities=() states=()
declare -A city_bbox=() city_states=()
while IFS=$'\t' read -r city st bbox smoke; do
    case $city in ''|'#'*) continue ;; esac
    if [ -z "$st" ] || [ -z "$bbox" ] || [ -z "$smoke" ]; then
        die "regions.tsv: incomplete row for $city"
    fi
    cities+=("$city")
    city_bbox[$city]=$bbox
    city_states[$city]=$st
    for s in ${st//,/ }; do
        case " ${states[*]} " in *" $s "*) ;; *) states+=("$s") ;; esac
    done
done < "$regions"
[ "${#cities[@]}" -gt 0 ] || die "regions.tsv lists no cities"
log "${#cities[@]} cities (${cities[*]}) from ${#states[@]} Geofabrik extracts; image $image"

# A city whose data now reaches past its padded box would get routes that stop
# at the clip edge. A warning, not a failure: the build is still better than
# the previous one, and the fix is a regions.tsv edit.
if [ -n "$cities_url" ]; then
    boxes=()
    for c in "${cities[@]}"; do boxes+=("$c=${city_bbox[$c]}"); done
    if cities_json=$(curl -fsS --max-time 30 "$cities_url"); then
        while read -r msg; do
            [ -n "$msg" ] && warn "$msg"
        done < <(printf '%s' "$cities_json" | python3 -I -c '
import json, sys
boxes = dict(arg.split("=", 1) for arg in sys.argv[1:])
for c in json.load(sys.stdin).get("cities", []):
    sid = c.get("source_id")
    # Printed one per line into a warning: keep an odd id on one line.
    shown = "".join(ch if ch.isalnum() or ch in "._-" else "?" for ch in str(sid))
    live = [c.get(k) for k in ("bbox_west", "bbox_south", "bbox_east", "bbox_north")]
    if sid not in boxes:
        print(f"{shown}: served but not in regions.tsv, so it has no routing")
        continue
    if None in live:
        continue
    w, s, e, n = map(float, boxes[sid].split(","))
    if not (w <= live[0] and s <= live[1] and live[2] <= e and live[3] <= n):
        print(f"{shown}: live bbox {live} is not inside regions.tsv {boxes[sid]}")
' "${boxes[@]}")
    else
        warn "could not read $cities_url; bbox check skipped"
    fi
fi

docker pull --quiet "$image" > /dev/null

# --- 1. download and clip, one state at a time -------------------------------
mkdir -p "$work/dl" "$work/clips" "$work/data"
for state in "${states[@]}"; do
    name=${state##*/}
    pbf=$work/dl/$name-latest.osm.pbf
    log "downloading $state"
    curl -fsSL --retry 3 --retry-delay 10 -o "$pbf" "$geofabrik/$state-latest.osm.pbf"
    curl -fsSL --retry 3 -o "$pbf.md5" "$geofabrik/$state-latest.osm.pbf.md5"
    (cd "$work/dl" && md5sum --quiet -c "$name-latest.osm.pbf.md5") ||
        die "$state: MD5 mismatch (Geofabrik may have published mid-download; re-run)"
    for city in "${cities[@]}"; do
        case ",${city_states[$city]}," in *",$state,"*) ;; *) continue ;; esac
        log "clipping $name to $city ${city_bbox[$city]}"
        osmium extract --no-progress --overwrite -b "${city_bbox[$city]}" \
            -s smart -S types=multipolygon,boundary \
            -o "$work/clips/$city-$name.osm.pbf" "$pbf"
    done
    rm -f "$pbf" "$pbf.md5"
done

# --- 2. merge ----------------------------------------------------------------
log "merging $(find "$work/clips" -name '*.osm.pbf' | wc -l) clips"
osmium merge --no-progress --overwrite -o "$work/data/all.osm.pbf" "$work"/clips/*.osm.pbf
rm -rf "$work/clips"
log "merged PBF: $(du -h "$work/data/all.osm.pbf" | cut -f1)"

# --- 3. tiles ----------------------------------------------------------------
# valhalla.json is written with the paths the server will see (/data, the
# compose mount), so the same file is built with and served with. The httpd
# settings are the ones compose relies on: listen on all interfaces inside the
# container (the port is published on loopback only), and drain in-flight
# requests for 28 s, within compose's 30 s stop_grace_period. Elevation is off:
# no elevation tiles are built, and an empty path stops the service looking.
log "building tiles (memory cap $build_memory, $build_jobs threads)"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    --memory "$build_memory" --cpus "$build_jobs" \
    -v "$work/data:/data" -w /data "$image" \
    bash -euo pipefail -c '
        jobs=$1
        valhalla_build_config \
            --mjolnir-tile-dir /data/tiles \
            --mjolnir-tile-extract /data/valhalla_tiles.tar \
            --mjolnir-admin /data/admins.sqlite \
            --mjolnir-timezone /data/timezones.sqlite \
            --mjolnir-concurrency "$jobs" \
            --additional-data-elevation "" \
            --httpd-service-listen "tcp://*:8002" \
            --httpd-service-drain-seconds 28 \
            --httpd-service-shutdown-seconds 1 \
            > valhalla.json
        echo "timezones"; valhalla_build_timezones > timezones.sqlite
        # valhalla_build_timezones does not reliably fail: its download has no
        # curl -f and its own error handling does not stop the script, so an
        # HTML error page or nothing at all can land in the file with exit 0.
        # Tiles built from that silently lose every local-time conversion.
        [ "$(head -c 15 timezones.sqlite)" = "SQLite format 3" ] ||
            { echo "timezones.sqlite is not an SQLite database" >&2; exit 1; }
        if python3 -c "import sqlite3" 2> /dev/null; then
            python3 -I -c "
import sqlite3, sys
n = sqlite3.connect(sys.argv[1]).execute(\"SELECT count(*) FROM tz_world\").fetchone()[0]
print(f\"tz_world: {n} rows\")
sys.exit(0 if n > 0 else 1)
" timezones.sqlite || { echo "timezones.sqlite has no tz_world rows" >&2; exit 1; }
        elif command -v sqlite3 > /dev/null; then
            [ "$(sqlite3 timezones.sqlite "SELECT count(*) FROM tz_world")" -gt 0 ] ||
                { echo "timezones.sqlite has no tz_world rows" >&2; exit 1; }
        fi
        echo "admins";    valhalla_build_admins -c valhalla.json all.osm.pbf
        echo "tiles";     valhalla_build_tiles -c valhalla.json -j "$jobs" all.osm.pbf
        echo "extract";   valhalla_build_extract -c valhalla.json -v
    ' build "$build_jobs"

# Only the tar and the config ship. Deleting the rest before the smoke test
# proves the server needs nothing else: admins and timezones are baked into the
# tiles at build time.
find "$work/data" -mindepth 1 -maxdepth 1 \
    ! -name valhalla_tiles.tar ! -name valhalla.json -exec rm -rf {} +
[ -s "$work/data/valhalla_tiles.tar" ] || die "valhalla_build_extract wrote no tar"
tar_bytes=$(stat -c %s "$work/data/valhalla_tiles.tar")
log "tar: $(du -h "$work/data/valhalla_tiles.tar" | cut -f1) ($tar_bytes bytes)"
# A release asset over 2 GiB cannot be uploaded, so fail here with a reason
# rather than in `gh release create`. The fix is smaller boxes in regions.tsv,
# or splitting the tar.
[ "$tar_bytes" -le "$asset_max_bytes" ] ||
    die "tar is $tar_bytes bytes, over GitHub's 2 GiB release-asset limit"
[ "$tar_bytes" -le "$asset_warn_bytes" ] ||
    warn "tar is $tar_bytes bytes, close to GitHub's 2 GiB release-asset limit"

# World-readable, because the server runs as nobody (docker-compose.yml) and
# mktemp -d made the work directory 0700.
chmod 0755 "$work" "$work/data"
chmod 0644 "$work/data/valhalla_tiles.tar" "$work/data/valhalla.json"

# --- 4. smoke test -----------------------------------------------------------
log "smoke test on 127.0.0.1:$smoke_port"
# The same confinement and limits as the compose service, so a build that only
# works as root or with a writable filesystem fails here.
docker run -d --name "$smoke_name" -p "127.0.0.1:$smoke_port:8002" \
    --user 65534:65534 --read-only --tmpfs /tmp:size=64m,mode=1777 \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256 \
    --memory 1536m --cpus 1.5 \
    -v "$work/data:/data:ro" "$image" valhalla_service /data/valhalla.json 1 > /dev/null
if ! "$here/smoke.sh" "$smoke_port" "$regions"; then
    docker logs --tail 40 "$smoke_name" 2>&1 || true
    die "smoke test failed; nothing written to $out"
fi
docker rm -f "$smoke_name" > /dev/null

# --- output ------------------------------------------------------------------
mkdir -p "$out"
mv -f "$work/data/valhalla_tiles.tar" "$work/data/valhalla.json" "$out/"
(cd "$out" && sha256sum valhalla_tiles.tar valhalla.json > SHA256SUMS)
log "done: $out"
ls -l "$out"
