#!/usr/bin/env bash
# Install or update the root-owned half of continuous deployment.
#
#   sudo deploy/install-deployer.sh            # scripts + autodeploy units
#   sudo deploy/install-deployer.sh --units    # also the api/etl unit templates
#   sudo deploy/install-deployer.sh --nginx    # also the nginx sites (any order)
#
# Copies, from this working copy:
#   deploy/autodeploy.sh     -> /usr/local/sbin/safety-autodeploy
#   deploy/lib/*.sh          -> /usr/local/lib/safety-deploy/
#   deploy/systemd/safety-autodeploy@.*  -> /etc/systemd/system/
#   with --units, every other deploy/systemd/*.service and *.timer too
#   with --nginx, deploy/nginx/<name>.conf -> /etc/nginx/sites-available/<name>
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
# The nginx sites are kept in the repo exactly as installed, certbot-managed
# lines included (F18): `certbot --nginx` edits the live file in place, so a
# repo copy without them would silently drop TLS. --nginx therefore refuses to
# overwrite a live site that differs both from the repo and from the copy it
# last installed (/var/lib/safety-deploy/nginx.installed/): commit the live file
# first. It reloads nginx only if a site changed and `nginx -t` passes; on a
# failed test it restores the replaced sites and removes new ones. It never
# creates or removes sites-enabled links.
#
# It does not enable, start or restart anything (a changed nginx site is reloaded).
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0 $*" >&2; exit 1; }
src=$(cd "$(dirname "$0")" && pwd)
units=/etc/systemd/system

want_units="" want_nginx=""
for arg in "$@"; do
    case $arg in
        --units) want_units=1 ;;
        --nginx) want_nginx=1 ;;
        *) echo "usage: $0 [--units] [--nginx]" >&2; exit 2 ;;
    esac
done

# Here and not only via the unit's StateDirectory=: a manual deploy.sh takes the
# lock in it before the timer has ever run.
install -d -o root -g root -m 0700 /var/lib/safety-deploy
install -o root -g root -m 0755 "$src/autodeploy.sh" /usr/local/sbin/safety-autodeploy
install -d -o root -g root -m 0755 /usr/local/lib/safety-deploy
install -o root -g root -m 0755 "$src"/lib/*.sh /usr/local/lib/safety-deploy/
install -o root -g root -m 0644 "$src"/systemd/safety-autodeploy@.* "$units/"

if [ -n "$want_units" ]; then
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

install_nginx() {
    local prev=/var/lib/safety-deploy/nginx.prev inst=/var/lib/safety-deploy/nginx.installed
    local f name dst
    local -a changed=() added=()
    install -d -o root -g root -m 0700 "$prev" "$inst"
    for f in "$src"/nginx/*.conf; do
        name=$(basename "$f" .conf)
        dst=/etc/nginx/sites-available/$name
        if cmp -s "$f" "$dst"; then
            install -o root -g root -m 0600 "$f" "$inst/$name"
            continue
        fi
        # The live file was changed outside the repo (certbot --nginx edits in
        # place): refuse rather than silently revert it.
        if [ -f "$dst" ] && { [ ! -f "$inst/$name" ] || ! cmp -s "$dst" "$inst/$name"; }; then
            diff -u "$f" "$dst" >&2 || true
            echo "nginx: $dst differs from the repo and from the last copy installed from it; commit the live file first" >&2
            exit 1
        fi
        if [ -f "$dst" ]; then
            cp -p "$dst" "$prev/$name"
            changed+=("$name")
        else
            added+=("$name")
        fi
        install -o root -g root -m 0644 "$f" "$dst"
    done
    if [ $((${#changed[@]} + ${#added[@]})) -eq 0 ]; then
        echo "nginx: sites already match the repo"
        return 0
    fi
    if nginx -t; then
        systemctl reload nginx
        for name in "${changed[@]}" "${added[@]}"; do
            install -o root -g root -m 0600 "/etc/nginx/sites-available/$name" "$inst/$name"
        done
        echo "nginx: installed ${changed[*]} ${added[*]} and reloaded"
    else
        for name in "${changed[@]}"; do cp -p "$prev/$name" "/etc/nginx/sites-available/$name"; done
        for name in "${added[@]}"; do rm -f "/etc/nginx/sites-available/$name"; done
        nginx -t || true
        echo "nginx -t failed; restored ${changed[*]}, removed ${added[*]}" >&2
        exit 1
    fi
}
if [ -n "$want_nginx" ]; then install_nginx; fi

echo "installed; nothing was restarted or enabled"
