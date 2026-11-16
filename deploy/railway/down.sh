#!/usr/bin/env bash
# Take the Railway deployment down: delete the project made by up.sh, with
# every service, volume, domain and the data in them. Billing stops with it.
#
#   deploy/railway/down.sh              # asks you to type the project name
#   deploy/railway/down.sh --yes        # no question (scripts)
#   deploy/railway/down.sh --template   # the template's reference project
#                                       # (make-template.sh) instead
#
# Permanent: Railway cannot undo a project delete. The data itself can be
# rebuilt from the cities' portals by running up.sh again (a few hours).

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
state_file=$here/.project-id
yes=0
for arg in "$@"; do
    case $arg in
        --yes) yes=1 ;;
        --template) state_file=$here/.template-project-id ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

die() { printf 'down.sh: %s\n' "$*" >&2; exit 1; }

command -v railway > /dev/null || die "the railway CLI is not installed"
command -v jq > /dev/null || die "jq is not installed"
[[ -z ${RAILWAY_TOKEN:-} ]] || die "unset RAILWAY_TOKEN: a project token cannot delete projects; use \`railway login\`"
railway whoami > /dev/null 2>&1 || die "not logged in to Railway: run \`railway login\` (or export RAILWAY_API_TOKEN)"
[[ -s $state_file ]] || die "no $state_file: nothing deployed from this checkout"

project_id=$(< "$state_file")
# shellcheck disable=SC2016  # $id is GraphQL's, not the shell's
name=$(railway api 'query($id: String!) { project(id: $id) { name } }' \
        --variables "$(jq -nc --arg id "$project_id" '{id: $id}')" --compact 2> /dev/null \
    | jq -r '.data.project.name // empty' 2> /dev/null || true)
[[ -n $name ]] || die "project $project_id was not found (already deleted?). Remove $state_file to start fresh."

if [[ $yes != 1 ]]; then
    echo "This permanently deletes Railway project '$name' ($project_id):"
    echo "the site, the database and all of its data."
    read -r -p "Type the project name to confirm: " answer
    [[ $answer == "$name" ]] || die "not confirmed; nothing deleted"
fi

# Accounts with two-factor auth: Railway asks for a code; pass it through with
#   RAILWAY_2FA_CODE=123456 deploy/railway/down.sh
args=(delete --project "$project_id" --yes)
[[ -n ${RAILWAY_2FA_CODE:-} ]] && args+=(--2fa-code "$RAILWAY_2FA_CODE")
railway "${args[@]}"

rm -f "$state_file"
echo "Deleted '$name'. Run deploy/railway/up.sh to deploy again from scratch."
