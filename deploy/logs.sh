#!/usr/bin/env bash
# Every ETL and ops run for one instance: the durable history from the
# database, then the journal output of the scheduled runs.
#
#   deploy/logs.sh prod                  # everything
#   deploy/logs.sh dev --since 7d        # last week (also 12h, 30m, 2026-10-01)
#   deploy/logs.sh prod --city chi       # one source
#   deploy/logs.sh prod --failed         # failed, blocked or still running
#   deploy/logs.sh prod -f               # history, then follow the journal live
#
# The history (etl.pull_run + etl.ops_run, via `safety.etl.run log`) is the
# complete record: it includes ops runs and anything started by hand, which
# never reach the journal. The journal adds the full process output -- log
# lines and tracebacks -- for the runs systemd started (safety-etl@,
# safety-etl-hourly@ and the post-deploy safety-ops@). The history step reads
# the instance's .env, so it runs
# as the instance's OS user via sudo; the journal is readable by the adm group without it.
set -euo pipefail

case "${1:-}" in
    prod|dev) instance=$1; shift ;;
    *) echo "usage: $0 prod|dev [--since 7d|12h|ISO] [--city ID] [--failed] [-f]" >&2; exit 2 ;;
esac

since='' follow='' history_args=()
while [ $# -gt 0 ]; do
    case $1 in
        --since)  since=$2; history_args+=(--since "$2"); shift 2 ;;
        --city)   history_args+=(--city "$2"); shift 2 ;;
        --failed) history_args+=(--failed); shift ;;
        -f|--follow) follow=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

inst=/srv/safety/Crime-Prevention-and-Personal-Safety-Application/$instance
current=$inst/current

# The instance's OS user (N1): the owner of its .env -- the instance directory
# itself is root's once dev is isolated. Allow-listed like instance_user.
user=$(sudo stat -c %U "$inst/.env")
case "$instance:$user" in
    prod:safety|dev:safety|dev:safety-dev) ;;
    *) echo "unexpected owner of $inst/.env: $user" >&2; exit 1 ;;
esac

echo "=== $instance: run history (etl.pull_run + etl.ops_run) ==="
sudo -u "$user" env -C "$current" PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m safety.etl.run log "${history_args[@]}"

# journalctl understands absolute times but not "7d"; turn the short forms
# into its "-7d" relative syntax.
journal_args=(-u "safety-etl@$instance" -u "safety-etl-hourly@$instance" -u "safety-ops@$instance" --no-pager -o short-iso)
if [ -n "$since" ]; then
    case $since in
        *[0-9][dhm]) journal_args+=(--since "-$since") ;;
        *)           journal_args+=(--since "$since") ;;
    esac
fi

echo
echo "=== $instance: journal (safety-etl@$instance, safety-etl-hourly@$instance, safety-ops@$instance) ==="
if [ -n "$follow" ]; then
    journalctl "${journal_args[@]}" -f
else
    journalctl "${journal_args[@]}"
fi
