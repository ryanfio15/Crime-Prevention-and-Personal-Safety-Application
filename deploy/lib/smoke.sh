#!/usr/bin/env bash
# Is the API on this loopback port serving, and serving the right commit?
#
#   smoke.sh <port> <expected>
#
# <expected> is one of:
#   <full sha>    /api/v1/health must report exactly this commit
#   ?<full sha>   must match if health reports a commit at all; a release from
#                 before health had the field is accepted (layout migration,
#                 rollback to an old release)
#   -             do not look at the commit (CI, where nothing is deployed)
#
# Checks, all of which must pass in the same attempt: /api/v1/health answers
# 200 (it reads the database), GET / serves the web UI, and GET /api/v1/cities
# answers 200 (a real read endpoint). Retries once a second for
# SMOKE_TIMEOUT seconds (default 60), since uvicorn and the pool take a moment.
#
# One script for every gate so they cannot drift apart: CI runs this repository
# copy; install.sh and migrate-layout.sh run the root-owned copy at
# /usr/local/lib/safety-deploy/smoke.sh.
set -euo pipefail

port=${1:?usage: $0 <port> <sha|?sha|->}
expected=${2:?usage: $0 <port> <sha|?sha|->}
timeout=${SMOKE_TIMEOUT:-60}
base=http://127.0.0.1:$port

case $expected in
    -) mode=skip ;;
    \?*) mode=if-present; expected=${expected#\?} ;;
    *) mode=exact ;;
esac

status() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || true; }

attempt() {
    local body commit code
    body=$(curl -fsS --max-time 10 "$base/api/v1/health" 2>/dev/null) ||
        { why="/api/v1/health did not answer 200"; return 1; }
    commit=$(jq -r '.commit // empty' <<<"$body" 2>/dev/null) ||
        { why="/api/v1/health did not return JSON"; return 1; }
    case $mode in
        exact)
            [ "$commit" = "$expected" ] ||
                { why="health reports commit '${commit:-none}', want $expected"; return 1; } ;;
        if-present)
            [ -z "$commit" ] || [ "$commit" = "$expected" ] ||
                { why="health reports commit '$commit', want $expected"; return 1; } ;;
    esac
    code=$(status "$base/")
    [ "$code" = 200 ] || { why="GET / answered $code"; return 1; }
    code=$(status "$base/api/v1/cities")
    [ "$code" = 200 ] || { why="GET /api/v1/cities answered $code"; return 1; }
}

why="no attempt made"
deadline=$((SECONDS + timeout))
while :; do
    if attempt; then
        echo "smoke ok on :$port"
        exit 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep 1
done
echo "smoke failed on :$port after ${timeout}s: $why" >&2
exit 1
