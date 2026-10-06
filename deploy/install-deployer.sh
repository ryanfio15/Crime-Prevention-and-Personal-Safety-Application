#!/usr/bin/env bash
# Install or update the root-owned half of continuous deployment.
#
#   sudo deploy/install-deployer.sh
#
# Copies, from this working copy:
#   deploy/autodeploy.sh     -> /usr/local/sbin/safety-autodeploy
#   deploy/lib/install.sh    -> /usr/local/lib/safety-deploy/install.sh
#   deploy/systemd/*.service, *.timer -> /etc/systemd/system/
# and creates the state directory /var/lib/safety-deploy.
#
# Deploys never update these: root runs only what a human installed here, never
# a file a push could change. So after changing any of the files above, merge
# it, then re-run this from an up-to-date checkout. The *.timer.d drop-ins
# (dev's ETL offsets) are left as they are.
#
# It does not enable or start anything; see docs/DEPLOY.md, "Continuous
# deployment (home server)".
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }
src=$(cd "$(dirname "$0")" && pwd)

# Here and not only via the unit's StateDirectory=: a manual deploy.sh takes the
# lock in it before the timer has ever run.
install -d -o root -g root -m 0700 /var/lib/safety-deploy
install -o root -g root -m 0755 "$src/autodeploy.sh" /usr/local/sbin/safety-autodeploy
install -D -o root -g root -m 0755 "$src/lib/install.sh" /usr/local/lib/safety-deploy/install.sh
install -o root -g root -m 0644 "$src"/systemd/*.service "$src"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
echo "installed; enable with: systemctl enable --now safety-autodeploy@<prod|dev>.timer"
