#!/usr/bin/env bash
# Build the one-click "Deploy on Railway" template (maintainers, not deployers).
#
#   railway login                                   # once per machine
#   deploy/railway/make-template.sh                 # branch railway-deployment
#   deploy/railway/make-template.sh --branch main
#
# A Railway template can only be written in the dashboard's composer or
# generated from an existing project; there is no file format for one. So this
# builds the project and generates it:
#
#   1. up.sh --github BRANCH: a reference project whose four services build from
#      this repository on GitHub, configured only through what a template
#      carries (variables, start command, health check, volume, domain);
#   2. Railway's templateGenerate: a template DRAFT copied from that project;
#   3. a check of the draft against what a deployer needs, printing anything
#      the copy lost, to fix in the composer before publishing.
#
# It does not publish: a published template is public on Railway's
# marketplace. The last lines print the command (or dashboard button) that
# does, and the Deploy-on-Railway button for the README.
#
# Rerun after changing the service layout or variables. Code changes need no
# new template: deployed copies build from the branch.

# shellcheck disable=SC2016  # GraphQL queries: the $vars are GraphQL's, not the shell's.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
cd "$repo"

branch=railway-deployment
name=safety-map-template
passthrough=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --branch) branch=$2; shift 2 ;;
        --name) name=$2; shift 2 ;;
        --cities|--workspace) passthrough+=("$1" "$2"); shift 2 ;;
        --no-routing) passthrough+=("$1"); shift ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

die() { printf 'make-template.sh: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

command -v railway > /dev/null || die "the railway CLI is not installed (npm i -g @railway/cli)"
command -v jq > /dev/null || die "jq is not installed"
railway whoami > /dev/null 2>&1 || die "not logged in to Railway: run \`railway login\` first"

# The template builds from GitHub, not from this checkout: make sure what is
# there is what you mean.
remote_sha=$(git ls-remote origin "refs/heads/$branch" | cut -f1)
[[ -n $remote_sha ]] || die "branch $branch is not on origin; push it first"
if [[ $(git rev-parse HEAD) != "$remote_sha" ]]; then
    echo "note: origin/$branch is ${remote_sha:0:7}, this checkout is $(git rev-parse --short HEAD);"
    echo "      the template and the reference project build origin/$branch."
fi

gql() {
    local resp
    resp=$(railway api "$1" --variables "${2:-"{}"}" --compact 2>&1) \
        || die "Railway API: $(jq -r '[.errors[]?.message] | join("; ")' <<< "$resp" 2> /dev/null || echo "$resp")"
    jq -c '.data' <<< "$resp"
}

# --- 1. the reference project ------------------------------------------------

log "reference project (built from github, branch $branch)"
"$here/up.sh" --github "$branch" --name "$name" "${passthrough[@]}"
project_id=$(< "$here/.template-project-id")
env_id=$(gql 'query($id: String!) { project(id: $id) { environments { edges { node { id name } } } } }' \
        "$(jq -nc --arg id "$project_id" '{id: $id}')" \
    | jq -r '[.project.environments.edges[].node] | (map(select(.name == "production")) + .)[0].id')

# --- 2. the template draft ---------------------------------------------------

log "generating the template draft"
template=$(gql 'mutation($input: TemplateGenerateInput!) { templateGenerate(input: $input) { id code name } }' \
    "$(jq -nc --arg p "$project_id" --arg e "$env_id" '{input: {projectId: $p, environmentId: $e}}')")
template_id=$(jq -r '.templateGenerate.id' <<< "$template")
code=$(jq -r '.templateGenerate.code' <<< "$template")
echo "    draft $code ($template_id)"

# --- 3. check the draft ------------------------------------------------------

log "checking the draft"
config=$(gql 'query($id: String!) { template(id: $id) { serializedConfig } }' \
    "$(jq -nc --arg id "$template_id" '{id: $id}')" | jq -c '.template.serializedConfig')
printf '%s\n' "$config" > "$here/.template-draft.json"

# One line per requirement: "ok" or "FIX", and what to do in the composer.
jq -r --arg branch "$branch" '
  [.services[]] as $s
  | def svc($n): ($s | map(select(.name == $n)) | first) // {};
    def var($n; $k): ((svc($n).variables // {})[$k]) as $v
        | if ($v | type) == "object" then $v.defaultValue else $v end;
    def check($ok; $what; $fix): if $ok then "  ok   \($what)" else "  FIX  \($what)\n         -> \($fix)" end;
  ( ["db", "api", "etl", "valhalla"] | map(. as $n | check((svc($n) | length) > 0; "service \($n) exists";
        "the draft has no \($n) service: rerun make-template.sh")) ),
  ( ["db", "api", "etl", "valhalla"] | map(. as $n | check(
        ((svc($n).source.repo // "") | test("Crime-Prevention")) and
        (((svc($n).source.repo // "") + " " + (svc($n).source.branch // "")) | test($branch));
        "\($n) builds from the repo, branch \($branch) (\(svc($n).source.repo // "no repo") \(svc($n).source.branch // ""))";
        "Source Repo: https://github.com/ryanfio15/Crime-Prevention-and-Personal-Safety-Application/tree/\($branch)")) ),
  check(var("db"; "RAILWAY_DOCKERFILE_PATH") == "deploy/railway/db.Dockerfile"; "db Dockerfile path";
        "variable RAILWAY_DOCKERFILE_PATH = deploy/railway/db.Dockerfile"),
  check(var("valhalla"; "RAILWAY_DOCKERFILE_PATH") == "deploy/railway/valhalla.Dockerfile"; "valhalla Dockerfile path";
        "variable RAILWAY_DOCKERFILE_PATH = deploy/railway/valhalla.Dockerfile"),
  ( [["db", "POSTGRES_PASSWORD"], ["db", "SAFETY_APP_PASSWORD"]] | map(.[0] as $sv | .[1] as $key | check(
        ((var($sv; $key) // "") | tostring | test("secret\\("));
        "\($sv) \($key) is generated per deployment";
        "set it to ${{secret(48, \"abcdef0123456789\")}} -- otherwise every deployment shares this project'"'"'s password")) ),
  check((var("api"; "POSTGRES_PASSWORD") // "") == "${{db.SAFETY_APP_PASSWORD}}"; "api reads the db password by reference";
        "api variable POSTGRES_PASSWORD = ${{db.SAFETY_APP_PASSWORD}}"),
  check((svc("api").deploy.startCommand // "") | test("start-api.sh"); "api start command";
        "/usr/local/bin/docker-entrypoint.sh sh deploy/railway/start-api.sh"),
  check((svc("api").deploy.healthcheckPath // "") == "/api/v1/health"; "api health check"; "Healthcheck Path: /api/v1/health"),
  check((svc("api").networking.serviceDomains // {}) | length > 0; "api has a public domain";
        "Settings -> Public Networking -> HTTP, port 8080"),
  check((svc("etl").deploy.startCommand // "") | test("etl_tick.py --loop"); "etl start command";
        "/usr/local/bin/docker-entrypoint.sh python deploy/railway/etl_tick.py --loop"),
  check([svc("db").volumeMounts // {} | .[] | .mountPath] | index("/var/lib/postgresql/data") != null; "db volume";
        "attach a volume at /var/lib/postgresql/data"),
  check([svc("etl").volumeMounts // {} | .[] | .mountPath] | index("/data") != null; "etl volume";
        "attach a volume at /data")
  | if type == "array" then .[] else . end
' <<< "$config"

cat << EOF

The draft is saved for reference in deploy/railway/.template-draft.json.

Next, in the Railway dashboard (workspace -> Templates -> $name):
  1. Fix every FIX line above in the composer, if any.
  2. Optional: give the variables descriptions (BOOTSTRAP_CITIES, SOCRATA_APP_TOKEN).
  3. Publish it -- this makes it PUBLIC on Railway's marketplace -- with the
     Publish button, or:
       railway templates publish $code --category Other \\
         --description "Map of relative reported-crime activity for six US cities" \\
         --readme-file deploy/railway/TEMPLATE.md

Then the one-click button for the README:

  [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new/template/$code?utm_medium=integration&utm_source=button&utm_campaign=safety-map)

The reference project ($name) can be kept as the template's live demo, or
deleted: deploy/railway/down.sh --template
EOF
