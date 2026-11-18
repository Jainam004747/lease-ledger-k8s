#!/usr/bin/env bash
# Calls the API through its Service and checks each answer. Fails on the first surprise.
set -euo pipefail
NS=${NS:-ledger-dev}

kubectl -n "$NS" port-forward svc/ledger-api 8080:80 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
for _ in $(seq 30); do curl -sf localhost:8080/healthz >/dev/null && break; sleep 1; done

call() {   # call EXPECTED_STATUS METHOD PATH [JSON]
  local want=$1 method=$2 path=$3 data=${4:-}
  local args=(-sS -o /tmp/body -w '%{http_code}' -X "$method" "localhost:8080$path")
  [ -n "$data" ] && args+=(-H 'content-type: application/json' -d "$data")
  local got
  got=$(curl "${args[@]}")
  echo "$method $path  ->  HTTP $got"
  jq -c . /tmp/body | cut -c1-400
  if [ "$got" != "$want" ]; then echo "expected HTTP $want"; exit 1; fi
  echo
}

call 200 GET /version
call 200 GET /readyz
call 200 GET "/leases/L-0043/statement?last=2"
call 201 POST /payments '{"lease_id": "L-0043", "amount": "4000", "received_on": "2026-10-29", "reference": "EFT-9001"}'
call 409 POST /payments '{"lease_id": "L-0043", "amount": "4000", "received_on": "2026-10-29", "reference": "EFT-9001"}'
call 404 POST /payments '{"lease_id": "L-0999", "amount": "10", "received_on": "2026-10-29", "reference": "EFT-1"}'
call 422 POST /payments '{"lease_id": "L-0043", "amount": "-5", "received_on": "2026-10-29", "reference": "EFT-2"}'
call 200 GET "/reports/reconcile?as_of=2026-10-29"
echo "smoke test passed"
