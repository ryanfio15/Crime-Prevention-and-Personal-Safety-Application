#!/usr/bin/env bash
# Logical backups of both instance databases from the shared safety_db container.
#   backup.sh dump     nightly: pg_dump -Fc each DB, check the archive, record a sidecar, prune
#   backup.sh verify   monthly: restore the newest prod dump into a scratch DB and compare
# Runs as root from /usr/local/lib/safety-deploy (deploy/install-deployer.sh), via
# safety-backup.service / safety-backup-verify.service. Uses the container's local
# socket as the bootstrap superuser (trust auth inside the official image), so it
# keeps working after F2 moves the instances to their own roles and after the
# superuser's TCP password is rotated.
#
# Nice=/IOSchedulingClass= on the units only affect the docker CLI: the dump and the
# restore run inside the postgres backend at normal priority.
#
# Where the files go: $SAFETY_BACKUP_DIR (default /var/backups/safety), root 0700,
# files 0600. Retention is per database (keep_days) and only pruned after a
# successful dump of that database, so a run of failures never deletes the last
# good copy. Everything is on the same disk as the database: this protects against
# a bad migration or corruption, not against losing the disk (no off-host copy yet).
set -euo pipefail

dest=${SAFETY_BACKUP_DIR:-/var/backups/safety}
container=safety_db
# The restore check's scratch database. Global, not local to verify(): the EXIT
# trap that drops it runs after verify() has returned.
scratch=safety_restore_check
# prod's database is `safety`, dev's is `safety_dev`.
declare -A keep_days=([safety]=7 [safety_dev]=2)
# Floor of free space left after a dump, so a backup never fills / under the
# database it is protecting.
min_free_gb=${SAFETY_BACKUP_MIN_FREE_GB:-15}
umask 077
install -d -o root -g root -m 0700 "$dest"

pg() { docker exec -i "$container" "$@"; }

free_gb() { df -P --block-size=1G "$1" | awk 'NR==2 {print $4}'; }

need_space() {   # need_space <path> <GB>
    local have
    have=$(free_gb "$1")
    if [ "$have" -lt "$2" ]; then
        echo "backup: only ${have} GB free on $1, need $2; refusing" >&2
        exit 1
    fi
}

# Newest dump of a database, or nothing. "safety-2…" never matches "safety_dev-…".
newest() {
    local db=$1 f newest_f=""
    for f in "$dest/$db"-2*.dump; do
        [ -e "$f" ] || continue
        if [ -z "$newest_f" ] || [ "$f" -nt "$newest_f" ]; then newest_f=$f; fi
    done
    printf '%s' "$newest_f"
}

dump_one() {
    local db=$1 ts out last need started
    # Room for at least twice the previous dump of this DB, and never below the floor.
    last=$(newest "$db")
    need=$min_free_gb
    if [ -n "$last" ]; then need=$(( $(stat -c %s "$last") * 2 / 1024**3 + min_free_gb )); fi
    need_space "$dest" "$need"

    ts=$(date -u +%Y%m%dT%H%M%SZ)
    out=$dest/$db-$ts.dump
    started=$SECONDS
    pg pg_dump -U safety -Fc -d "$db" > "$out.partial"
    # The archive's table of contents must be readable, or it is not a backup.
    pg pg_restore -l < "$out.partial" > /dev/null
    # Sidecar: what the restore check compares against (the live DB may have
    # migrated since the dump was taken).
    pg psql -U safety -d "$db" -X -Atc "SELECT count(*) FROM public.schema_migration" > "$out.meta.partial"
    mv "$out.meta.partial" "$out.meta"
    mv "$out.partial" "$out"
    echo "dumped $db to $out ($(stat -c %s "$out") bytes, $(( SECONDS - started ))s)"

    find "$dest" -maxdepth 1 \( -name "$db-2*.dump" -o -name "$db-2*.dump.meta" \) \
         -mtime +"${keep_days[$db]}" -print -delete
}

verify() {
    local latest restored expected size started
    latest=$(newest safety)
    if [ -z "$latest" ] || [ ! -s "$latest.meta" ]; then
        echo "restore check: no prod dump with a .meta sidecar in $dest" >&2
        exit 1
    fi
    expected=$(cat "$latest.meta")
    # The restore lands inside the shared cluster volume (on /): a full prod copy.
    # -Fc compresses roughly 4-6x, so budget six times the archive plus the floor.
    size=$(( $(stat -c %s "$latest") * 6 / 1024**3 + min_free_gb ))
    need_space / "$size"

    trap 'pg dropdb -U safety --if-exists "$scratch" >/dev/null 2>&1 || true' EXIT
    started=$SECONDS
    pg dropdb -U safety --if-exists "$scratch"
    pg createdb -U safety "$scratch"
    # A new database grants CONNECT/TEMP to PUBLIC; this one holds a full copy of
    # prod, so close it to the instance roles before restoring into it (N3). The
    # superuser (container socket) still restores, checks and drops it.
    pg psql -U safety -d postgres -X -q -v ON_ERROR_STOP=1 -c "REVOKE CONNECT, TEMPORARY ON DATABASE $scratch FROM PUBLIC"
    pg pg_restore -U safety -d "$scratch" --no-owner --exit-on-error < "$latest"
    restored=$(pg psql -U safety -d "$scratch" -X -Atc "SELECT count(*) FROM public.schema_migration")
    if [ "$restored" != "$expected" ]; then
        echo "restore check: $restored migrations, dump recorded $expected" >&2
        exit 1
    fi
    [ "$(pg psql -U safety -d "$scratch" -X -Atc 'SELECT count(*) > 0 FROM silver.incident')" = t ] ||
        { echo "restore check: silver.incident is empty" >&2; exit 1; }
    [ "$(pg psql -U safety -d "$scratch" -X -Atc 'SELECT count(*) > 0 FROM gold.city_snapshot')" = t ] ||
        { echo "restore check: gold.city_snapshot is empty" >&2; exit 1; }
    echo "restore check ok: $latest ($expected migrations, $(( SECONDS - started ))s)"
}

case ${1:-} in
    dump)
        # Leftovers of a run that died mid-dump.
        find "$dest" -maxdepth 1 -name '*.partial' -mmin +720 -delete
        dump_one safety
        dump_one safety_dev
        ;;
    verify) verify ;;
    *) echo "usage: $0 dump|verify" >&2; exit 2 ;;
esac
