#!/usr/bin/env bash
# Smoke test a Valhalla service before its tiles are trusted:
#
#   smoke.sh <port> [regions.tsv]
#
# Waits for http://127.0.0.1:<port>/status, then routes every city's `smoke`
# trip from regions.tsv (default: the copy next to this script) by auto,
# pedestrian and bicycle, and requires trip.status 0 for each -- 18 routes for
# six cities. One mode failing in one city fails the whole test: a tileset that
# routes cars but not bikes in Austin is a broken tileset, not a partial one.
#
# Used by build.sh (before a build is published) and by fetch.sh (before a
# downloaded build is swapped in, and on the live port after the restart).
# Python is only the JSON reader; any python3 will do.
set -euo pipefail

port=${1:?usage: smoke.sh <port> [regions.tsv]}
regions=${2:-$(cd "$(dirname "$0")" && pwd)/regions.tsv}
base=http://127.0.0.1:$port
# Long enough for a cold start that maps a 1+ GB tar from a slow disk.
wait_s=${VALHALLA_SMOKE_WAIT:-120}

log() { echo "valhalla-smoke[:$port]: $*"; }

deadline=$((SECONDS + wait_s))
until curl -fs -o /dev/null --max-time 5 "$base/status"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        log "no /status after ${wait_s}s"
        exit 1
    fi
    sleep 2
done

fail=0 ok=0
while IFS=$'\t' read -r city _states _bbox smoke; do
    case $city in ''|'#'*) continue ;; esac
    from=${smoke%%;*} to=${smoke##*;}
    for mode in auto pedestrian bicycle; do
        body=$(printf '{"locations":[{"lat":%s,"lon":%s},{"lat":%s,"lon":%s}],"costing":"%s","units":"kilometers","directions_type":"none"}' \
            "${from%,*}" "${from#*,}" "${to%,*}" "${to#*,}" "$mode")
        # Valhalla answers every routing error with HTTP 400 and a JSON body,
        # so the body is read either way and the verdict comes from it.
        resp=$(curl -sS --max-time 30 -H 'Content-Type: application/json' \
            --data "$body" "$base/route" || true)
        verdict=$(printf '%s' "$resp" | python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("no JSON"); sys.exit()
t = d.get("trip") or {}
if t.get("status") == 0:
    print("ok %.2f km" % (t.get("summary") or {}).get("length", 0))
else:
    print("error %s: %s" % (d.get("error_code"), d.get("error", "")))
')
        case $verdict in
            ok*) ok=$((ok + 1)) ;;
            *) fail=$((fail + 1)); log "$city $mode: $verdict" ;;
        esac
    done
done < "$regions"

if [ "$ok" -eq 0 ] || [ "$fail" -ne 0 ]; then
    log "FAILED: $ok routed, $fail failed"
    exit 1
fi
log "ok: $ok routes"
