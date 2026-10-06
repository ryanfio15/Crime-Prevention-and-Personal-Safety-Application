#!/usr/bin/env bash
# Release and venv helpers, sourced by install.sh and migrate-layout.sh from the
# root-owned copy /usr/local/lib/safety-deploy/release.sh. Root only.
#
# Layout of an instance directory I (docs/DEPLOY.md, "How a deploy works"):
#   I/.env, I/data/            the instance's own, owned by safety, never replaced
#   I/releases/<full sha>/     one commit, root:safety and read-only to safety
#   I/venvs/<hash>/            one venv per requirements.txt + interpreter, same
#   I/current -> releases/<sha>  the only path systemd units name
#
# Every release links .venv -> ../../venvs/<hash>, .env -> ../../.env and
# data -> ../../data, so the code finds its environment exactly as it did in a
# flat checkout. Nothing here executes repository code as root: compileall only
# compiles, and the venv is built by `safety`.

python=/usr/bin/python3.12

# A venv is reusable exactly when the requirements and the interpreter (patch
# level and build included) are the same.
venv_hash() {
    { cat "$1"; "$python" -VV; } | sha256sum | cut -c1-16
}

# Read-only to safety: directories root:safety 0750, files 0640, executables
# 0750. chown -h and chmod -R never follow the release's symlinks.
seal() {
    chown -hR root:safety "$1"
    chmod -R u=rwX,g=rX,o= "$1"
}

# Turn an extracted tree into I/releases/<sha>, unless a complete one is already
# there. Built under .tmp and renamed, with the marker written last, so a
# release that exists is a whole one.
build_release() {
    local inst=$1 sha=$2 stage=$3
    local rel=$inst/releases/$sha tmp=$inst/releases/$sha.tmp hash
    [ -f "$rel/.release-complete" ] && return 0
    install -d -o root -g safety -m 0750 "$inst/releases" "$inst/venvs"
    rm -rf --one-file-system "$rel" "$tmp"
    mkdir "$tmp"
    cp -a "$stage/." "$tmp/"
    hash=$(venv_hash "$tmp/requirements.txt")
    rm -rf "$tmp/.venv" "$tmp/.env" "$tmp/data"
    ln -s "../../venvs/$hash" "$tmp/.venv"
    ln -s ../../.env "$tmp/.env"
    ln -s ../../data "$tmp/data"
    echo "$sha" > "$tmp/DEPLOYED_COMMIT"
    # Bytecode now, as root, because the release is read-only to the process
    # that imports it (units set PYTHONDONTWRITEBYTECODE=1). -s/-p record the
    # final path, not .tmp, in tracebacks.
    "$python" -m compileall -q -s "$tmp" -p "$rel" "$tmp/safety"
    touch "$tmp/.release-complete"
    seal "$tmp"
    mv -T "$tmp" "$rel"
}

# Build I/venvs/<hash> from a requirements file unless it exists. `safety` runs
# venv and pip into a .tmp it owns; root then seals and renames it. Never
# modified afterwards: a changed requirements.txt is a new hash, a new venv.
ensure_venv() {
    local inst=$1 hash=$2 req=$3
    local venv=$inst/venvs/$hash tmp=$inst/venvs/$hash.tmp
    [ -f "$venv/.venv-complete" ] && return 0
    rm -rf --one-file-system "$venv" "$tmp"
    install -d -o safety -g safety -m 0700 "$tmp"
    runuser -u safety -- env HOME=/srv/safety "$python" -m venv "$tmp"
    runuser -u safety -- env HOME=/srv/safety "$tmp/bin/python" -m pip install \
        --quiet --disable-pip-version-check -r "$req"
    touch "$tmp/.venv-complete"
    seal "$tmp"
    mv -T "$tmp" "$venv"
}

# Point I/current at a release in one rename, so systemd never sees it missing.
switch_current() {
    ln -sfn "releases/$2" "$1/current.new"
    mv -T "$1/current.new" "$1/current"
}

# The sha I/current points at, or nothing.
current_sha() {
    local link
    link=$(readlink "$1/current" 2>/dev/null) || return 0
    echo "${link#releases/}"
}

# Keep the three newest complete releases plus the shas given (current,
# previous, the one being installed); remove the rest, any leftovers from an
# interrupted build, and every venv no remaining release links to.
prune_releases() {
    local inst=$1 n=0 rel name
    shift
    local -A keep=() used=()
    for name in "$@"; do keep[$name]=1; done
    while read -r _ rel; do
        name=${rel##*/}
        n=$((n + 1))
        if [ "$n" -gt 3 ] && [ -z "${keep[$name]:-}" ]; then
            echo "pruning release $name"
            rm -rf --one-file-system "$rel"
        fi
    done < <(for rel in "$inst"/releases/*; do
        [ -f "$rel/.release-complete" ] && echo "$(stat -c %Y "$rel/.release-complete") $rel"
    done | sort -rn)
    for rel in "$inst"/releases/*; do
        [ -e "$rel" ] || continue
        name=${rel##*/}
        if [ ! -f "$rel/.release-complete" ] && [ -z "${keep[$name]:-}" ]; then
            rm -rf --one-file-system "$rel"
        elif [ -L "$rel/.venv" ]; then
            name=$(readlink "$rel/.venv")
            used[${name##*/}]=1
        fi
    done
    for rel in "$inst"/venvs/*; do
        [ -e "$rel" ] || continue
        name=${rel##*/}
        if [ -z "${used[$name]:-}" ]; then
            echo "pruning venv $name"
            rm -rf --one-file-system "$rel"
        fi
    done
}

# An ETL run imports the release under it and migrate can block on its locks.
etl_running() {
    systemctl is-active --quiet "safety-etl@$1" "safety-etl-hourly@$1"
}
