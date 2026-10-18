#!/usr/bin/env bash
# Run the dev instance as its own OS user, `safety-dev`, instead of `safety` (N1).
#
#   sudo deploy/os-isolate.sh dev              # isolate (idempotent; safe to re-run)
#   sudo deploy/os-isolate.sh dev --rollback   # put dev back on `safety`
#
# Why: both instances ran as `safety`, so code deployed to dev (any push to
# testing) could read prod's .env -- its database credentials -- and prod's
# /proc/<pid>/environ. After this, dev's units run as safety-dev through
# systemd drop-ins (prod's units are untouched), the deployer runs dev's code as
# safety-dev (it reads /var/lib/safety-deploy/dev.user, root-owned state), and:
#
#   dev/                    root:safety-dev 0750   root owns it, so safety-dev
#                                                  cannot swap releases/, venvs/
#                                                  or current for symlinks root
#                                                  would follow on the next deploy
#   dev/.env, dev/data/**   safety-dev             the instance's own files
#   dev/releases, venvs     root:safety-dev        sealed, read-only to safety-dev
#   prod/                   safety 0700            unchanged
#
# Prod is never touched: this script refuses `prod`. See docs/DEPLOY.md "OS users".
#
# Steps (a failed pre-check exits before anything changes):
#   1. pre-checks: root, committed script, new deployer installed, regular .env,
#      root-owned app directory, no symlink anywhere on the instance path, no
#      file in the instance tree hard-linked from elsewhere (e.g. prod/data);
#   2. the deploy lock, then -- with the dev ETL timers stopped -- the ETL lock;
#   3. creates safety-dev (system user, home /srv/safety-dev) if missing;
#   4. stops safety-api@dev (dev is down until step 7);
#   5. ownership as in the table above (chown -h: never follows a symlink);
#   6. installs the four drop-ins and writes /var/lib/safety-deploy/dev.user;
#   7. starts safety-api@dev and verifies: smoke, process user, isolation both
#      ways, safety-dev cannot create entries in dev/ but can write data/.
#      Any failure rolls back automatically, still under the locks;
#   8. restarts the ETL timers it stopped (also on any exit).
#
# --rollback undoes 4-6 exactly (dev/ back to safety:safety 0700). The
# safety-dev user is kept; `userdel safety-dev` is a manual step.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0 $*" >&2; exit 1; }

instance=${1:-}
mode=isolate
case ${2:-} in
    "") ;;
    --rollback) mode=rollback ;;
    *) echo "usage: $0 dev [--rollback]" >&2; exit 2 ;;
esac
case $instance in
    dev) ;;
    prod) echo "refusing: prod keeps running as safety (N1 isolates dev only)" >&2; exit 2 ;;
    *) echo "usage: $0 dev [--rollback]" >&2; exit 2 ;;
esac

repo=$(cd "$(dirname "$0")/.." && pwd)
app=/srv/safety/Crime-Prevention-and-Personal-Safety-Application
inst=$app/$instance
prodinst=$app/prod
state=/var/lib/safety-deploy
userfile=$state/$instance.user
newuser=safety-dev
smoke=/usr/local/lib/safety-deploy/smoke.sh
dropin_src=$repo/deploy/systemd/instance-user/dev.conf
services=(api etl etl-hourly ops)
timers=("safety-etl@$instance.timer" "safety-etl-hourly@$instance.timer")

log() { echo "os-isolate[$instance]: $*"; }
die() { echo "os-isolate[$instance]: $*" >&2; exit 1; }

# --- 1. pre-checks -------------------------------------------------------------
# What runs on the host is what CI checked: no uncommitted edits.
git -c safe.directory="$repo" -C "$repo" diff --quiet HEAD -- deploy/os-isolate.sh deploy/systemd/instance-user ||
    die "uncommitted changes to this script or deploy/systemd/instance-user in $repo; refusing"
[ -f "$dropin_src" ] || die "$dropin_src is missing"
grep -q '^instance_user()' /usr/local/lib/safety-deploy/release.sh ||
    die "the installed deployer predates N1; run deploy/install-deployer.sh first"
[ -x "$smoke" ] || die "$smoke is missing; run deploy/install-deployer.sh"
# The app directory must be root's (U32b): otherwise `safety` could have
# replaced dev/ with something of its own before we chown it.
{ [ "$(stat -c '%U:%G %a' "$app")" = "root:root 755" ] && [ ! -L "$app" ]; } ||
    die "$app is not root:root 755; refusing"
# No symlink anywhere on the instance path, nor at the directories we recurse into.
{ [ ! -L "$inst" ] && [ "$(readlink -f "$inst")" = "$inst" ]; } || die "$inst is or passes through a symlink; refusing"
for d in data releases venvs; do
    { [ -d "$inst/$d" ] && [ ! -L "$inst/$d" ]; } || die "$inst/$d is not a real directory; refusing"
done
{ [ -f "$inst/.env" ] && [ ! -L "$inst/.env" ]; } || die "$inst/.env is not a regular file; refusing"
# chown changes an inode, not a name: a file in dev/ hard-linked from elsewhere
# (dev's data seeded from prod's with cp -al, say) would be re-owned there too,
# handing safety-dev write access to prod's copy. Refuse; docs/DEPLOY.md "OS
# users" has the command that gives dev its own copies.
shared=$(find "$inst" -xdev ! -type d -links +1 -print -quit)
[ -z "$shared" ] ||
    die "$shared (and maybe more) has other hard links, possibly into prod/; break them first (docs/DEPLOY.md \"OS users\"); refusing"

# Who owns the instance's files right now (safety, or safety-dev on a re-run).
cur=$(stat -c %U "$inst/.env")
case $cur in
    safety|safety-dev) ;;
    *) die "unexpected owner of $inst/.env: $cur" ;;
esac

# --- 2. locks, held until exit ---------------------------------------------------
exec 8>"$state/deploy.lock"
log "waiting for the deploy lock"
flock -w 900 8 || die "the deploy lock has been held for 15 minutes; try again later"

# Oneshot units report "activating" while they run, which `is-active --quiet`
# does not count as running (see etl_running in deploy/lib/release.sh).
for unit in "safety-etl@$instance" "safety-etl-hourly@$instance" "safety-ops@$instance"; do
    case $(systemctl is-active "$unit" 2>/dev/null) in
        active | activating | deactivating | reloading)
            echo "os-isolate[$instance]: ETL running on $instance; try again later" >&2
            exit 75 ;;
    esac
done
# A unit queued behind the ETL lock would already have exec'd as the old uid,
# so stop the timers first; the ones that were active are restarted on exit.
stopped=()
for t in "${timers[@]}"; do
    if systemctl is-active --quiet "$t"; then
        systemctl stop "$t"
        stopped+=("$t")
    fi
done
restart_timers() {
    local t
    for t in "${stopped[@]}"; do systemctl start "$t" || echo "os-isolate[$instance]: could not restart $t" >&2; done
}
trap restart_timers EXIT

# Root never creates or opens for writing a path in the instance user's data/
# (a planted symlink would be followed): a missing lock is created by its
# owner, an existing one must be a regular file and is opened read-only.
lockf=$inst/data/.etl.lock
if [ ! -e "$lockf" ] && [ ! -L "$lockf" ]; then
    # shellcheck disable=SC2016  # $1 expands in the inner sh
    runuser -u "$cur" -- sh -c 'umask 022; : >> "$1"' sh "$lockf"
fi
{ [ -f "$lockf" ] && [ ! -L "$lockf" ]; } || die "$lockf is not a regular file; refusing"
exec 9<"$lockf"
log "waiting for the ETL lock"
flock -w 3600 9 || die "an ETL run has held $lockf for an hour; try again later"

# chown the instance's top-level entries for user $1. releases/venvs keep their
# root owner (seal) and only change group; current is root's symlink.
own_entries() {
    local who=$1 p
    for p in "$inst"/* "$inst"/.[!.]*; do
        [ -e "$p" ] || [ -L "$p" ] || continue
        case ${p##*/} in
            current) ;;
            releases|venvs) chgrp -hR "$who" "$p" ;;
            *) chown -hR "$who:$who" "$p" ;;
        esac
    done
}

start_and_smoke() {
    local deployed expected=-
    deployed=$(cat "$state/$instance.deployed" 2>/dev/null || true)
    if [ -n "$deployed" ]; then expected="?$deployed"; fi
    systemctl start "safety-api@$instance"
    "$smoke" 8001 "$expected"
}

rollback() {
    log "rolling back to safety"
    systemctl stop "safety-api@$instance" || true
    local s
    for s in "${services[@]}"; do
        rm -f "/etc/systemd/system/safety-$s@$instance.service.d/10-instance-user.conf"
        rmdir "/etc/systemd/system/safety-$s@$instance.service.d" 2>/dev/null || true
    done
    rm -f "$userfile"
    own_entries safety
    chown -h safety:safety "$inst"
    chmod 0700 "$inst"
    systemctl daemon-reload
    start_and_smoke || die "rollback smoke FAILED: safety-api@$instance is not serving -- investigate now"
    [ "$(ps -o user= -p "$(systemctl show -p MainPID --value "safety-api@$instance")")" = safety ] ||
        die "after rollback safety-api@$instance does not run as safety"
    log "rolled back: $instance runs as safety"
}

if [ "$mode" = rollback ]; then
    rollback
    exit 0
fi

# --- 3. user ------------------------------------------------------------------------
if ! getent passwd "$newuser" >/dev/null; then
    useradd --system --user-group --home-dir "/srv/$newuser" --no-create-home \
        --shell /usr/sbin/nologin "$newuser"
    log "created user $newuser"
fi
install -d -o "$newuser" -g "$newuser" -m 0700 "/srv/$newuser" "/srv/$newuser/.cache"

# --- 4./5. stop, then ownership ------------------------------------------------------
systemctl stop "safety-api@$instance"
chown -h "root:$newuser" "$inst"
chmod 0750 "$inst"
own_entries "$newuser"

# --- 6. drop-ins and state ------------------------------------------------------------
for s in "${services[@]}"; do
    install -d -o root -g root -m 0755 "/etc/systemd/system/safety-$s@$instance.service.d"
    install -o root -g root -m 0644 "$dropin_src" "/etc/systemd/system/safety-$s@$instance.service.d/10-instance-user.conf"
done
tmp=$(mktemp "$state/.$instance.user.XXXXXX")
printf '%s\n' "$newuser" > "$tmp"
install -o root -g root -m 0600 "$tmp" "$userfile"
rm -f "$tmp"
systemctl daemon-reload

# --- 7. start and verify ---------------------------------------------------------------
verify() {
    start_and_smoke || { echo "smoke failed" >&2; return 1; }
    [ "$(ps -o user= -p "$(systemctl show -p MainPID --value "safety-api@$instance")")" = "$newuser" ] ||
        { echo "safety-api@$instance does not run as $newuser" >&2; return 1; }
    runuser -u "$newuser" -- test ! -r "$prodinst/.env" || { echo "$newuser can read prod's .env" >&2; return 1; }
    runuser -u safety -- test ! -r "$inst/.env" || { echo "safety can read dev's .env" >&2; return 1; }
    # shellcheck disable=SC2016  # $1 expands in the inner sh
    if runuser -u "$newuser" -- sh -c 'ln -s /tmp "$1/x"' sh "$inst" 2>/dev/null || [ -e "$inst/x" ] || [ -L "$inst/x" ]; then
        rm -f "$inst/x"
        echo "$newuser can create entries in $inst" >&2
        return 1
    fi
    runuser -u "$newuser" -- test -w "$inst/data" || { echo "$newuser cannot write $inst/data" >&2; return 1; }
}
if ! verify; then
    echo "os-isolate[$instance]: verification FAILED; rolling back" >&2
    rollback
    exit 1
fi
log "isolated: $instance runs as $newuser"
