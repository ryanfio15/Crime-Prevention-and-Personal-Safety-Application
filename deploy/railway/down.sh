#!/usr/bin/env bash
# Take the Railway deployment down: delete the project made by up.sh, with
# every service, volume, domain and the data in them. Billing stops with it.
#
#   deploy/railway/down.sh            # asks you to type the project name
#   deploy/railway/down.sh --yes      # no question (scripts)
#
# Permanent: Railway cannot undo a project delete. The data itself can be
# rebuilt from the cities' portals by running up.sh again (a few hours).

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
state_file=$here/.project-id
yes=0
[[ ${1:-} == --yes ]] && yes=1

die() { printf 'down.sh: %s\n' "$*" >&2; exit 1; }

command -v railway > /dev/null || die "the railway CLI is not installed"
command -v jq > /dev/null || die "jq is not installed"
[[ -n ${RAILWAY_API_TOKEN:-} ]] || die "set RAILWAY_API_TOKEN (railway.com/account/tokens)"
[[ -z ${RAILWAY_TOKEN:-} ]] || die "unset RAILWAY_TOKEN: this needs the account token only"
[[ -s $state_file ]] || die "no $state_file: nothing deployed from this checkout"

project_id=$(< "$state_file")
name=$(curl -sS -X POST https://backboard.railway.com/graphql/v2 \
        -H "Authorization: Bearer $RAILWAY_API_TOKEN" -H 'Content-Type: application/json' \
        --data "$(jq -nc --arg id "$project_id" '{query: "query($id: String!) { project(id: $id) { name } }", variables: {id: $id}}')" \
    | jq -r '.data.project.name // empty')
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
