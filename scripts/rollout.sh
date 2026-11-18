#!/usr/bin/env bash
# Three changes to a running deployment, each with steady traffic going through the Service:
#   1. a normal release (new version rolls out pod by pod)
#   2. a pod is deleted, as if its node went away
#   3. a bad release that crashes on start, then a rollback
# The traffic generator runs inside the cluster and fails the step if any request fails.
set -euo pipefail
NS=${NS:-ledger-dev}
IMAGE=${IMAGE:-ghcr.io/jainam004747/lease-ledger-api:ci}
k() { kubectl -n "$NS" "$@"; }

start_traffic() {   # start_traffic NAME SECONDS
  k delete pod "$1" --ignore-not-found >/dev/null
  k run "$1" --image="$IMAGE" --image-pull-policy=IfNotPresent --restart=Never --quiet \
    -- python -m app.hammer --url http://ledger-api/version --seconds "$2" --rate 20 >/dev/null
  k wait --for=condition=Ready "pod/$1" --timeout=60s >/dev/null
}

finish_traffic() {  # finish_traffic NAME -> prints the result, returns its exit code
  for _ in $(seq 120); do
    phase=$(k get pod "$1" -o jsonpath='{.status.phase}')
    [ "$phase" = Succeeded ] || [ "$phase" = Failed ] && break
    sleep 2
  done
  k logs "$1"
  [ "$phase" = Succeeded ]
}

echo "### 1. Release 1.1.0 while traffic is flowing"
start_traffic traffic-release 45
sleep 5
k set env deployment/ledger-api -c api APP_VERSION=1.1.0
k rollout status deployment/ledger-api --timeout=120s
finish_traffic traffic-release

echo
echo "### 2. A pod disappears"
start_traffic traffic-pod-loss 30
sleep 5
victim=$(k get pods -l app.kubernetes.io/name=ledger-api -o jsonpath='{.items[0].metadata.name}')
k delete pod "$victim" --wait=false
k rollout status deployment/ledger-api --timeout=120s
finish_traffic traffic-pod-loss
k get pods -l app.kubernetes.io/name=ledger-api -o wide

echo
echo "### 3. A bad release: DB_POOL_SIZE=0 makes the new pods crash on start"
start_traffic traffic-bad-release 150
sleep 5
k set env deployment/ledger-api -c api DB_POOL_SIZE=0 APP_VERSION=1.2.0
if k rollout status deployment/ledger-api --timeout=150s; then
  echo "the bad release should not have become ready"; exit 1
fi
k get pods -l app.kubernetes.io/name=ledger-api
bad=$(k get pods -l app.kubernetes.io/name=ledger-api --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
echo "--- last log lines of $bad"
k logs "$bad" -c api --tail=3 || true
echo "--- rolling back"
k rollout undo deployment/ledger-api
k rollout status deployment/ledger-api --timeout=120s
finish_traffic traffic-bad-release
k rollout history deployment/ledger-api
