#!/usr/bin/env bash
# Load-test the API tier from inside the cluster.
#
#   deploy/k8s/loadtest/run.sh [vus]        # default 3000 virtual users
#
# The app's per-client rate limiter would count every virtual user as one
# client (they all come from the k6 pod), so it is switched off for the run
# and back on afterwards -- the use .env.example names for ENABLE_RATE_LIMIT
# =false. That restarts the API pods twice; each warms its cache on start.
# Watch the autoscaler meanwhile with:  kubectl -n safety get hpa -w
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ns=safety
vus=${1:-3000}

restore() {
    echo "==> re-enabling the API rate limiter"
    kubectl -n "$ns" set env deploy/safety-api ENABLE_RATE_LIMIT- > /dev/null
    kubectl -n "$ns" rollout status deploy/safety-api --timeout=600s
}

echo "==> disabling the API rate limiter for the run"
kubectl -n "$ns" set env deploy/safety-api ENABLE_RATE_LIMIT=false > /dev/null
trap restore EXIT
kubectl -n "$ns" rollout status deploy/safety-api --timeout=600s

kubectl -n "$ns" create configmap safety-loadtest-script \
    --from-file=browse.js="$here/browse.js" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$ns" delete job safety-loadtest --ignore-not-found --wait=true
sed "s/value: \"3000\"/value: \"$vus\"/" "$here/k6-job.yaml" | kubectl apply -f -

echo "==> running k6 with $vus virtual users (about 9 minutes)"
kubectl -n "$ns" wait --for=condition=Ready pod -l app.kubernetes.io/name=safety-loadtest --timeout=300s
kubectl -n "$ns" logs -f job/safety-loadtest || true
if kubectl -n "$ns" wait --for=condition=Complete job/safety-loadtest --timeout=120s; then
    echo "==> thresholds met"
else
    echo "==> k6 reported failed thresholds (see summary above)"
    exit 1
fi
