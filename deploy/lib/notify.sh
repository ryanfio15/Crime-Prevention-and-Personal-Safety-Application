#!/usr/bin/env bash
# Alert hook for a failed safety unit: safety-notify@<unit>.service runs
#   notify.sh <unit>
# from the OnFailure= line every ETL, ops, deploy, API and backup unit carries.
#
# Journal only, by decision (2026-10-07): one err-priority line under the tag
# safety-notify, read with
#   journalctl -t safety-notify -p err
# plus the same line appended to $STATE_DIRECTORY/alerts.log
# (/var/lib/safety-notify/alerts.log via DynamicUser's StateDirectory), a durable
# list that outlives journal rotation. To add an external channel later, add one
# command after `logger` below (a webhook curl, mail, ...) -- and keep it
# best-effort, like everything here.
#
# Never fails: an alert hook that errors would only bury the failure it reports.
# No `set -e` for that reason.
set -u

unit=${1:-unknown}
line="$unit failed on $(hostname) at $(date -u +%FT%TZ); see journalctl -u $unit"

logger -p daemon.err -t safety-notify -- "$line" 2>/dev/null || true
if [ -n "${STATE_DIRECTORY:-}" ]; then
    printf '%s\n' "$line" >> "$STATE_DIRECTORY/alerts.log" 2>/dev/null || true
fi
exit 0
