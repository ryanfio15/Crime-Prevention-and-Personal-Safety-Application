#!/usr/bin/env bash
# Deploy (or update) the site on a Kubernetes cluster.
#
#   deploy/k8s/deploy.sh <overlay> [image]
#
#   overlay   fabric | fabric-small | fabric-minimal | fabric-sharded | aws | azure | local
#             (deploy/k8s/overlays/<overlay>)
#   image     default ghcr.io/ryanfio15/crime-prevention-and-personal-safety-application:horizontal-scaling
#             -- the image workflow also publishes :<full commit sha>, which
#             pins a release and lets the smoke test check the commit.
#
# Options (environment):
#   EGRESS_PROXY=<url>   pods reach the internet through this forward proxy
#                        (FABRIC sites without IPv4; see step 2)
#   SKIP_BOOTSTRAP=1     do not create the first-load Job
#   RERUN_BOOTSTRAP=1    delete the bootstrap Job (completed or running) and run it again
#   FOLLOW_BOOTSTRAP=1   stream the bootstrap's log until it finishes
#   ROUTING_WAIT_SECONDS how long to wait for routing before going on (1800)
#
# In order, each step waiting for the last -- the cluster-side counterpart of
# deploy/lib/install.sh:
#   1. the CloudNativePG operator, if the cluster does not have it yet;
#   2. the overlay (namespace, config, database, API, routing, ETL CronJobs,
#      ingress, policies), with the image substituted;
#   3. the database cluster(s) becoming ready (three with fabric-sharded);
#   4. the migrate Job, run fresh for this release;
#   5. the API rollout (new API pods wait for step 4 by themselves:
#      safety/schemawait.py);
#   6. the bootstrap Job, if it has never completed: enable the cities and
#      load the last year of data. It runs on in the background; the site
#      shows a progress bar with an estimate of the time left
#      (GET /api/v1/progress) and serves each city as soon as its own load is
#      done;
#   7. the Valhalla rollout -- after the bootstrap has started, and not fatal:
#      each new pod downloads ~1.2 GB first, slowly on some networks;
#   8. a smoke test through a port-forward (deploy/lib/smoke.sh).
#
# Idempotent: run it again for every new image.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)

overlay=${1:?usage: $0 <overlay: see deploy/k8s/overlays> [image]}
default_image=ghcr.io/ryanfio15/crime-prevention-and-personal-safety-application:horizontal-scaling
image=${2:-$default_image}
ns=safety
cnpg_version=1.30.1
cnpg_manifest=https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-${cnpg_version}.yaml

[ -d "$here/overlays/$overlay" ] || { echo "no overlay '$overlay' in $here/overlays" >&2; exit 2; }
for cmd in kubectl curl jq openssl; do
    command -v "$cmd" > /dev/null || { echo "deploy.sh needs $cmd" >&2; exit 2; }
done

log() { printf '\n==> %s\n' "$*"; }

# kustomize output with the placeholder image replaced. The base and the jobs
# both resolve `safety-app` to $default_image, so one substitution covers both.
render() {
    kubectl kustomize "$1" | sed "s#image: ${default_image}\$#image: ${image}#"
}

# Wait for a Job to succeed; fail as soon as it has failed for good.
wait_job() {
    local name=$1 timeout=$2 deadline=$((SECONDS + $2)) state
    while :; do
        state=$(kubectl -n "$ns" get job "$name" -o json 2> /dev/null | jq -r '
            if (.status.succeeded // 0) > 0 then "done"
            elif any(.status.conditions[]?; .type == "Failed" and .status == "True") then "failed"
            else "running" end')
        case $state in
            done) return 0 ;;
            failed)
                kubectl -n "$ns" logs "job/$name" --tail=40 || true
                echo "job $name failed" >&2
                return 1 ;;
        esac
        if [ $SECONDS -ge $deadline ]; then
            echo "job $name still not done after ${timeout}s" >&2
            return 1
        fi
        sleep 5
    done
}

# --- 1. operator ------------------------------------------------------------
if ! kubectl get crd clusters.postgresql.cnpg.io > /dev/null 2>&1; then
    log "installing the CloudNativePG operator $cnpg_version"
    kubectl apply --server-side -f "$cnpg_manifest"
fi
kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=300s

# --- 2. the overlay ---------------------------------------------------------
manifests=$(render "$here/overlays/$overlay")
# With the cities split across several databases (overlays/fabric-sharded),
# they share the owner role's credentials: a Secret the operator would
# otherwise generate per cluster. Created once, before the clusters, and never
# replaced -- the databases' role already has this password.
if grep -q 'safety.io/shared-credentials: "true"' <<< "$manifests"; then
    kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
    if ! kubectl -n "$ns" get secret safety-db-app > /dev/null 2>&1; then
        log "creating the shared database credentials (secret safety-db-app)"
        kubectl -n "$ns" create secret generic safety-db-app \
            --type=kubernetes.io/basic-auth \
            --from-literal=username=safety_app \
            --from-literal=password="$(openssl rand -hex 24)" \
            --from-literal=dbname=safety
    fi
fi
# EGRESS_PROXY=http://host:port sends the pods' outbound HTTP(S) through a
# forward proxy: on a FABRIC site whose VMs have no IPv4, the nodes reach
# IPv4-only hosts by FABRIC's NAT64 but the IPv4-only pod network cannot, so
# create_slice.py runs a proxy on the control-plane node and sets this. Kept
# in the `safety-egress` ConfigMap, which every app container loads when it
# exists; leaving EGRESS_PROXY unset on a later deploy leaves it in place.
# (Running pods pick a change up on their next restart.) Two more settings
# ride along, because that path is the flaky one: OPENSSL_CONF drops the
# post-quantum key share whose oversized TLS greeting the NAT64 path to the
# Socrata and Esri portals drops (openssl-classic-groups.cnf says how that was
# measured), and the adapters retry six times rather than four.
if [ -n "${EGRESS_PROXY:-}" ]; then
    kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
    log "outbound traffic goes through $EGRESS_PROXY (configmap safety-egress)"
    no_proxy="localhost,127.0.0.1,.svc,.cluster.local,valhalla,10.0.0.0/8"
    kubectl -n "$ns" create configmap safety-egress \
        --from-literal=HTTPS_PROXY="$EGRESS_PROXY" \
        --from-literal=https_proxy="$EGRESS_PROXY" \
        --from-literal=HTTP_PROXY="$EGRESS_PROXY" \
        --from-literal=http_proxy="$EGRESS_PROXY" \
        --from-literal=NO_PROXY="$no_proxy" \
        --from-literal=no_proxy="$no_proxy" \
        --from-literal=ROUTING_HTTPS_PROXY="$EGRESS_PROXY" \
        --from-literal=OPENSSL_CONF=/app/deploy/k8s/openssl-classic-groups.cnf \
        --from-literal=HTTP_MAX_RETRIES=6 \
        --dry-run=client -o yaml | kubectl apply -f -
fi

log "applying overlay '$overlay' with image $image"
printf '%s\n' "$manifests" | kubectl apply -f -

# --- 3. database ------------------------------------------------------------
log "waiting for the database cluster(s)"
# The Cluster objects exist at once, their Ready condition only once the
# primary accepts connections; the first initdb takes a minute or two.
kubectl -n "$ns" wait clusters.postgresql.cnpg.io --all --for=condition=Ready --timeout=1200s

# --- 4. migrate -------------------------------------------------------------
log "migrating"
jobs=$(render "$here/jobs")
kubectl -n "$ns" delete job safety-migrate --ignore-not-found --wait=true
printf '%s\n' "$jobs" | kubectl apply -f - -l app.kubernetes.io/name=safety-migrate
wait_job safety-migrate 1800
kubectl -n "$ns" logs job/safety-migrate --tail=5 || true

# --- 5. the API -------------------------------------------------------------
log "rolling out the API"
kubectl -n "$ns" rollout status deploy/safety-api --timeout=900s

# --- 6. bootstrap -----------------------------------------------------------
if [ "${SKIP_BOOTSTRAP:-0}" != 1 ]; then
    state=$(kubectl -n "$ns" get job safety-bootstrap -o json 2> /dev/null | jq -r '
        if (.status.succeeded // 0) > 0 then "done"
        elif any(.status.conditions[]?; .type == "Failed" and .status == "True") then "failed"
        else "running" end' || true)
    # RERUN_BOOTSTRAP=1 replaces a completed or a still-running bootstrap:
    # a Job's pods keep the image and settings it was created with, so this
    # is how a fix reaches a load that is failing.
    if [ "$state" != "" ] && [ "${RERUN_BOOTSTRAP:-0}" = 1 ]; then
        state=failed
    fi
    case $state in
        done) log "bootstrap already completed; the CronJobs keep the data current" ;;
        running) log "bootstrap is already running: kubectl -n $ns logs -f job/safety-bootstrap" ;;
        *)
            [ -z "$state" ] || kubectl -n "$ns" delete job safety-bootstrap --wait=true
            log "starting the first load (all enabled cities, the last year of each)"
            printf '%s\n' "$jobs" | kubectl apply -f - -l app.kubernetes.io/name=safety-bootstrap
            echo "follow it with: kubectl -n $ns logs -f job/safety-bootstrap"
            echo "or on the site itself: a progress bar shows how much longer it will take"
            ;;
    esac
    if [ "${FOLLOW_BOOTSTRAP:-0}" = 1 ]; then
        kubectl -n "$ns" wait --for=condition=Ready pod -l app.kubernetes.io/name=safety-bootstrap --timeout=600s || true
        kubectl -n "$ns" logs -f job/safety-bootstrap || true
        wait_job safety-bootstrap 64800
    fi
fi

# --- 7. routing --------------------------------------------------------------
# After the bootstrap has started, and not fatal: the first Valhalla pods
# download ~1.2 GB of tiles before they report ready, which on a slow path
# (FABRIC's NAT64) can take a long while, and the map does not need them --
# until they are up, drive/walk/bike directions report unavailable.
log "rolling out routing (each new pod downloads ~1.2 GB of tiles first)"
if ! kubectl -n "$ns" rollout status deploy/valhalla --timeout="${ROUTING_WAIT_SECONDS:-1800}s"; then
    echo "routing is not ready yet; the site is deployed without it for now." >&2
    kubectl -n "$ns" get pods -l app.kubernetes.io/name=valhalla -o wide || true
    kubectl -n "$ns" logs -l app.kubernetes.io/name=valhalla -c fetch-tiles --prefix --tail=4 || true
    echo "follow it: kubectl -n $ns logs -f -l app.kubernetes.io/name=valhalla -c fetch-tiles --prefix" >&2
fi

# --- 8. smoke ---------------------------------------------------------------
log "smoke test"
port=18080
kubectl -n "$ns" port-forward svc/safety-api "$port:80" > /dev/null 2>&1 &
pf=$!
trap 'kill $pf 2> /dev/null || true' EXIT
tag=${image##*:}
if [[ $image != *@* && $tag =~ ^[0-9a-f]{40}$ ]]; then
    expect=$tag
else
    expect=-
fi
SMOKE_TIMEOUT=90 "$repo/deploy/lib/smoke.sh" "$port" "$expect"

log "deployed. kubectl -n $ns get pods -o wide"
kubectl -n "$ns" get pods -o wide
