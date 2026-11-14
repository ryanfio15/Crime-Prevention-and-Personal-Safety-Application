#!/usr/bin/env bash
# Deploy the whole site to Railway with one command, or update a deployment.
#
#   export RAILWAY_API_TOKEN=...          # railway.com/account/tokens
#   deploy/railway/up.sh                  # create (first run) or update
#   deploy/railway/up.sh --name my-site --workspace "My Team"
#   deploy/railway/up.sh --no-routing     # no Valhalla (drive/walk/bike off)
#   deploy/railway/up.sh --config-only    # settings and variables, no deploys
#
# What it builds (docs/RAILWAY.md):
#
#   db        PostGIS 17-3.5 (deploy/railway/db.Dockerfile), volume, private
#   api       the site and API, ONE replica, public https://*.up.railway.app
#   valhalla  street routing, tiles baked in (deploy/railway/valhalla.Dockerfile)
#   etl       hourly cron (deploy/railway/etl_tick.py), volume for bronze
#
# The first run creates the project and remembers its id in
# deploy/railway/.project-id (git-ignored). Every later run updates that same
# project in place: settings and variables re-applied, every service rebuilt
# from the working tree, generated passwords kept. Data is never touched.
#
# Why two tools: the Railway CLI creates the project, uploads the code
# (`railway up`), makes the domain and triggers cron runs; everything it has no
# command for -- cron schedule, start command, health check, restart policy,
# Dockerfile path, volumes -- goes through Railway's public GraphQL API, which
# is also what keeps reruns idempotent. Railway's config-as-code files
# (railway.toml) are deprecated as of 2026-12-01, and its IaC SDK has no cron
# field, so neither is used.
#
# Requires: railway (CLI), curl, jq, openssl.

# shellcheck disable=SC2016  # GraphQL queries: the $vars are GraphQL's, not the shell's.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
cd "$repo"

api_url=https://backboard.railway.com/graphql/v2
state_file=$here/.project-id

name=safety-map
workspace=
routing=1
deploy=1
cities=${BOOTSTRAP_CITIES:-phl,chi,sea,lax,dc,aus}
etl_cron="20 * * * *"

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
    case $1 in
        --name) name=$2; shift 2 ;;
        --workspace) workspace=$2; shift 2 ;;
        --no-routing) routing=0; shift ;;
        --config-only) deploy=0; shift ;;
        --cities) cities=$2; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
done

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'up.sh: %s\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

for cmd in railway curl jq openssl; do
    command -v "$cmd" > /dev/null || die "$cmd is not installed$( [[ $cmd == railway ]] && printf '%s' \
        ' (npm i -g @railway/cli, or see https://docs.railway.com/cli)')"
done
[[ -n ${RAILWAY_API_TOKEN:-} ]] || die "set RAILWAY_API_TOKEN to an account token from https://railway.com/account/tokens"
# The CLI refuses to run with both set; a project token cannot create projects.
[[ -z ${RAILWAY_TOKEN:-} ]] || die "unset RAILWAY_TOKEN: this script needs the account token (RAILWAY_API_TOKEN) only"
export RAILWAY_API_TOKEN

# --- GraphQL -----------------------------------------------------------------

# gql QUERY [VARIABLES_JSON] -> prints .data; dies on any error.
gql() {
    local out
    out=$(gql_try "$@") || die "Railway API: $out"
    printf '%s\n' "$out"
}

# gql_try QUERY [VARIABLES_JSON] -> prints .data, or the error text and fails.
gql_try() {
    local body resp
    body=$(jq -nc --arg q "$1" --argjson v "${2:-"{}"}" '{query: $q, variables: $v}')
    if ! resp=$(curl -sS -X POST "$api_url" \
            -H "Authorization: Bearer $RAILWAY_API_TOKEN" \
            -H 'Content-Type: application/json' --data "$body"); then
        printf 'request failed\n'
        return 1
    fi
    if ! jq -e . > /dev/null 2>&1 <<< "$resp"; then
        printf '%s\n' "$resp"
        return 1
    fi
    if jq -e '(.errors // []) | length > 0' > /dev/null <<< "$resp"; then
        jq -r '[.errors[].message] | join("; ")' <<< "$resp"
        return 1
    fi
    jq -c '.data' <<< "$resp"
}

project_query='query($id: String!) {
  project(id: $id) {
    id name
    environments { edges { node { id name } } }
    services { edges { node { id name } } }
  }
}'

# --- project -----------------------------------------------------------------

if [[ -s $state_file ]]; then
    project_id=$(< "$state_file")
    log "updating Railway project $project_id"
    project=$(gql "$project_query" "$(jq -nc --arg id "$project_id" '{id: $id}')")
else
    log "creating Railway project '$name'"
    init_args=(init --name "$name" --json)
    [[ -n $workspace ]] && init_args+=(--workspace "$workspace")
    init_out=$(railway "${init_args[@]}") || die "railway init failed: $init_out"
    project_id=$(jq -r '.id // .projectId // .project.id // empty' <<< "$init_out" 2> /dev/null || true)
    if [[ -z $project_id ]]; then
        project_id=$(railway status --json | jq -r '.id // .project.id // empty')
    fi
    [[ -n $project_id ]] || die "could not read the new project's id from: $init_out"
    printf '%s\n' "$project_id" > "$state_file"
    project=$(gql "$project_query" "$(jq -nc --arg id "$project_id" '{id: $id}')")
fi

env_id=$(jq -r '[.project.environments.edges[].node] | (map(select(.name == "production")) + .)[0].id' <<< "$project")
[[ -n $env_id && $env_id != null ]] || die "project $project_id has no environment"
railway link --project "$project_id" --environment "$env_id" > /dev/null

# --- services ----------------------------------------------------------------

service_id() {
    jq -r --arg n "$1" '.project.services.edges[].node | select(.name == $n) | .id' <<< "$project"
}

ensure_service() {
    local svc=$1 id
    id=$(service_id "$svc")
    if [[ -z $id ]]; then
        id=$(gql 'mutation($input: ServiceCreateInput!) { serviceCreate(input: $input) { id name } }' \
            "$(jq -nc --arg p "$project_id" --arg n "$svc" '{input: {projectId: $p, name: $n}}')" \
            | jq -r '.serviceCreate.id')
        echo "    created service $svc ($id)" >&2
    fi
    printf '%s\n' "$id"
}

# Attach a volume unless the service already has one. A volume belongs to
# exactly one service; deleting it deletes the data, so this never does.
ensure_volume() {
    local svc=$1 sid=$2 mount=$3 existing
    if existing=$(gql_try 'query($id: String!) { project(id: $id) { volumes { edges { node {
              name volumeInstances { edges { node { serviceId mountPath } } } } } } } }' \
            "$(jq -nc --arg id "$project_id" '{id: $id}')"); then
        if jq -e --arg s "$sid" '[.project.volumes.edges[].node.volumeInstances.edges[].node
                | select(.serviceId == $s)] | length > 0' > /dev/null <<< "$existing"; then
            return 0
        fi
    else
        echo "    (could not list volumes: $existing; creating one for $svc)" >&2
    fi
    gql 'mutation($input: VolumeCreateInput!) { volumeCreate(input: $input) { id name } }' \
        "$(jq -nc --arg p "$project_id" --arg e "$env_id" --arg s "$sid" --arg m "$mount" \
            '{input: {projectId: $p, environmentId: $e, serviceId: $s, mountPath: $m}}')" > /dev/null
    echo "    attached a volume to $svc at $mount" >&2
}

# set_vars SERVICE_ID JSON_OBJECT: merge, never replace, and do not deploy yet.
set_vars() {
    gql 'mutation($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }' \
        "$(jq -nc --arg p "$project_id" --arg e "$env_id" --arg s "$1" --argjson v "$2" \
            '{input: {projectId: $p, environmentId: $e, serviceId: $s, variables: $v, skipDeploys: true}}')" \
        > /dev/null
}

get_vars() {
    gql 'query($p: String!, $e: String!, $s: String!) {
           variables(projectId: $p, environmentId: $e, serviceId: $s) }' \
        "$(jq -nc --arg p "$project_id" --arg e "$env_id" --arg s "$1" '{p: $p, e: $e, s: $s}')" \
        | jq -c '.variables // {}'
}

# configure SERVICE_ID JSON_OBJECT: the service's deploy and build settings.
configure() {
    gql 'mutation($s: String!, $e: String!, $input: ServiceInstanceUpdateInput!) {
           serviceInstanceUpdate(serviceId: $s, environmentId: $e, input: $input) }' \
        "$(jq -nc --arg s "$1" --arg e "$env_id" --argjson i "$2" '{s: $s, e: $e, input: $i}')" > /dev/null
}

log "services"
db_id=$(ensure_service db)
api_id=$(ensure_service api)
etl_id=$(ensure_service etl)
valhalla_id=
[[ $routing == 1 ]] && valhalla_id=$(ensure_service valhalla)

ensure_volume db "$db_id" /var/lib/postgresql/data
ensure_volume etl "$etl_id" /data

# --- variables ---------------------------------------------------------------

log "variables"

# Passwords are generated once and then kept: the database volume remembers
# the first ones, so a new value on a rerun would lock the app out. Hex only,
# because the password ends up inside a connection URL (safety/config.py).
db_vars=$(get_vars "$db_id")
superuser_pw=$(jq -r '.POSTGRES_PASSWORD // empty' <<< "$db_vars")
app_pw=$(jq -r '.SAFETY_APP_PASSWORD // empty' <<< "$db_vars")
[[ -n $superuser_pw ]] || superuser_pw=$(openssl rand -hex 24)
[[ -n $app_pw ]] || app_pw=$(openssl rand -hex 24)

set_vars "$db_id" "$(jq -nc --arg su "$superuser_pw" --arg app "$app_pw" '{
    POSTGRES_USER: "postgres", POSTGRES_PASSWORD: $su, POSTGRES_DB: "safety",
    SAFETY_APP_PASSWORD: $app }')"

# Settings both app services read (safety/config.py), as in the Kubernetes
# ConfigMap (deploy/k8s/base/config.yaml) with the cluster-only ones dropped.
# ${{...}} are Railway references, resolved by Railway at deploy time.
common=$(jq -nc --arg cities "$cities" --arg socrata "${SOCRATA_APP_TOKEN:-}" --arg routing "$routing" '{
    POSTGRES_HOST: "${{db.RAILWAY_PRIVATE_DOMAIN}}",
    POSTGRES_PORT: "5432",
    POSTGRES_DB: "safety",
    POSTGRES_USER: "safety_app",
    POSTGRES_PASSWORD: "${{db.SAFETY_APP_PASSWORD}}",
    RETENTION_DAYS: "365",
    BACKFILL_MONTHS: "12",
    BOOTSTRAP_CITIES: $cities,
    DIRECTIONS_ENABLED: "true",
    ROUTING_BUDGET_BACKEND: "db",
    TRANSIT_DAILY_BUDGET: "100",
    GEOCODE_DAILY_BUDGET: "2000",
    ROUTING_TIMEOUT_SECONDS: "8"
  }
  + (if $routing == "1" then {VALHALLA_URL: "http://${{valhalla.RAILWAY_PRIVATE_DOMAIN}}:8002"} else {} end)
  + (if $socrata != "" then {SOCRATA_APP_TOKEN: $socrata} else {} end)')

set_vars "$api_id" "$(jq -c '. + {
    PORT: "8080",
    ENABLE_DOCS: "true",
    ENABLE_RATE_LIMIT: "true",
    CACHE_MAX_ENTRIES: "192",
    CACHE_MAX_BYTES: "268435456",
    CACHE_STAMP_TTL_SECONDS: "5",
    CACHE_WARM_ON_START: "true",
    API_POOL_MAX_SIZE: "8",
    API_STATEMENT_TIMEOUT_MS: "15000",
    API_POOL_TIMEOUT_SECONDS: "10",
    HSTS_MAX_AGE_SECONDS: "86400"
  }' <<< "$common")"

set_vars "$etl_id" "$(jq -c '. + {
    BRONZE_ROOT: "/data/bronze",
    WITHDRAWN_RECONCILE: "report",
    OPS_RETRY_COOLDOWN_HOURS: "6",
    JOBLOCK_WAIT_SECONDS: "600"
  }' <<< "$common")"

if [[ $routing == 1 ]]; then
    # The newest tiles release, so a rerun after the monthly tiles build picks
    # it up (and busts the image's build cache). `latest` if GitHub is unreachable.
    tiles_repo=${VALHALLA_TILES_REPO:-ryanfio15/Crime-Prevention-and-Personal-Safety-Application}
    tiles_tag=$(curl -fsS "https://api.github.com/repos/$tiles_repo/releases?per_page=30" 2> /dev/null | jq -r '
        [.[] | select((.tag_name | startswith("valhalla-tiles-")) and (.draft | not)
                      and (.prerelease | not) and .author.login == "github-actions[bot]")]
        | sort_by(.published_at) | last | .tag_name // empty' 2> /dev/null || true)
    tiles_tag=${tiles_tag:-latest}
    echo "    routing tiles: $tiles_tag"
    set_vars "$valhalla_id" "$(jq -nc --arg t "$tiles_tag" '{VALHALLA_TILES_TAG: $t}')"
fi

# --- service settings --------------------------------------------------------

log "service settings"

configure "$db_id" '{
  "dockerfilePath": "deploy/railway/db.Dockerfile",
  "restartPolicyType": "ALWAYS",
  "numReplicas": 1
}'

# One replica, by design. The health check runs a query through the pool; its
# long timeout covers a first deploy waiting out the database's initdb.
configure "$api_id" '{
  "dockerfilePath": "Dockerfile",
  "startCommand": "/usr/local/bin/docker-entrypoint.sh sh deploy/railway/start-api.sh",
  "healthcheckPath": "/api/v1/health",
  "healthcheckTimeout": 1500,
  "restartPolicyType": "ALWAYS",
  "numReplicas": 1
}'

# A cron job must exit; restart policy NEVER, or Railway treats a finished run
# as a crash and starts it again.
configure "$etl_id" "$(jq -nc --arg cron "$etl_cron" '{
  dockerfilePath: "Dockerfile",
  startCommand: "/usr/local/bin/docker-entrypoint.sh python deploy/railway/etl_tick.py",
  cronSchedule: $cron,
  restartPolicyType: "NEVER",
  numReplicas: 1
}')"

if [[ $routing == 1 ]]; then
    configure "$valhalla_id" '{
      "dockerfilePath": "deploy/railway/valhalla.Dockerfile",
      "restartPolicyType": "ALWAYS",
      "numReplicas": 1
    }'
fi

# --- deploy ------------------------------------------------------------------

if [[ $deploy == 1 ]]; then
    # The database first: the API waits for it, but a database still building
    # when the API starts only makes the first deploy's logs harder to read.
    log "building and deploying db (the first start runs initdb: several minutes)"
    railway up --service db --ci

    if [[ $routing == 1 ]]; then
        log "building and deploying valhalla (downloads ~1.2 GB of tiles into the image)"
        railway up --service valhalla --ci
    fi

    log "building and deploying api (one replica)"
    railway up --service api --ci

    log "building and deploying etl (hourly cron at minute 20)"
    railway up --service etl --ci
fi

# --- public URL --------------------------------------------------------------

log "public URL"
domain=$(gql_try 'query($p: String!, $e: String!, $s: String!) {
           domains(projectId: $p, environmentId: $e, serviceId: $s) { serviceDomains { domain } } }' \
    "$(jq -nc --arg p "$project_id" --arg e "$env_id" --arg s "$api_id" '{p: $p, e: $e, s: $s}')" \
    | jq -r '.domains.serviceDomains[0].domain // empty' 2> /dev/null || true)
if [[ -z $domain ]]; then
    domain=$(gql 'mutation($input: ServiceDomainCreateInput!) { serviceDomainCreate(input: $input) { domain } }' \
        "$(jq -nc --arg e "$env_id" --arg s "$api_id" '{input: {environmentId: $e, serviceId: $s, targetPort: 8080}}')" \
        | jq -r '.serviceDomainCreate.domain')
fi

# --- first data load ---------------------------------------------------------

if [[ $deploy == 1 ]]; then
    # Start the first tick now rather than at the next :20. On a new project it
    # enables the cities and begins the one-year load; on an update it is an
    # ordinary tick (Railway skips it if one is already running).
    log "starting an etl tick now"
    railway cron run --service etl --yes \
        || echo "    could not trigger it; the schedule runs it at the next minute 20"
fi

cat << EOF

Done.

  Site:     https://$domain
  Project:  https://railway.com/project/$project_id

The first load of a new project takes one to a few hours, mostly waiting on
the city portals. The site shows a progress bar meanwhile, and each city
appears on the map as soon as its own load finishes.

  Data job logs:   railway logs --service etl
  Site logs:       railway logs --service api
  Run a tick now:  railway cron run --service etl --yes
  Take it down:    deploy/railway/down.sh
EOF
