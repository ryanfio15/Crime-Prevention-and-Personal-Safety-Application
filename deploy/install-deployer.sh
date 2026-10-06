#!/usr/bin/env bash
# Install or update the root-owned half of continuous deployment.
#
#   sudo deploy/install-deployer.sh            # scripts + autodeploy units
#   sudo deploy/install-deployer.sh --units    # also the api/etl unit templates
#
# Copies, from this working copy:
#   deploy/autodeploy.sh     -> /usr/local/sbin/safety-autodeploy
#   deploy/lib/*.sh          -> /usr/local/lib/safety-deploy/
#   deploy/systemd/safety-autodeploy@.*  -> /etc/systemd/system/
#   with --units, every other deploy/systemd/*.service and *.timer too
# and creates the state directory /var/lib/safety-deploy.
#
# Deploys never update these: root runs only what a human installed here, never
# a file a push could change. So after changing any of the files above, merge
# it, then re-run this from an up-to-date checkout.
#
# The api/etl templates are behind --units because both instances share them:
# installing a template that names I/current before an instance has one breaks
# that instance on its next restart (docs/DEPLOY.md, "Moving an instance to
# the release layout"). Each template it replaces with different content is
# copied to /var/lib/safety-deploy/units.prev/ first, to put back by hand if a
# restart under the new one fails. The *.timer.d drop-ins (dev's ETL offsets)
# are left as they are.
#
# It does not enable, start or restart anything.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0 $*" >&2; exit 1; }
src=$(cd "$(dirname "$0")" && pwd)
units=/etc/systemd/system

# Here and not only via the unit's StateDirectory=: a manual deploy.sh takes the
# lock in it before the timer has ever run.
install -d -o root -g root -m 0700 /var/lib/safety-deploy
install -o root -g root -m 0755 "$src/autodeploy.sh" /usr/local/sbin/safety-autodeploy
install -d -o root -g root -m 0755 /usr/local/lib/safety-deploy
install -o root -g root -m 0755 "$src"/lib/*.sh /usr/local/lib/safety-deploy/
install -o root -g root -m 0644 "$src"/systemd/safety-autodeploy@.* "$units/"

if [ "${1:-}" = --units ]; then
    install -d -o root -g root -m 0700 /var/lib/safety-deploy/units.prev
    for f in "$src"/systemd/*.service "$src"/systemd/*.timer; do
        name=${f##*/}
        case $name in safety-autodeploy@.*) continue ;; esac
        if [ -f "$units/$name" ] && ! cmp -s "$f" "$units/$name"; then
            cp -p "$units/$name" "/var/lib/safety-deploy/units.prev/$name"
        fi
        install -o root -g root -m 0644 "$f" "$units/$name"
    done
fi

systemctl daemon-reload
echo "installed; nothing was restarted or enabled"
