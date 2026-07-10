#!/usr/bin/env bash
# Retired gops-dev resources are previewed by default. Pass --apply to delete only the audited names below.
set -euo pipefail

namespace="${GOPS_RETIRED_NAMESPACE:-gops-dev}"
mode="dry-run"
if [[ "${1:-}" == "--apply" ]]; then
  mode="apply"
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

deployments=(
  alpaca-connector alpaca-connector-untagged
  api-websocket api-websocket-untagged
  clickhouse-store clickhouse-store-untagged
  flink-stream-processor flink-stream-processor-untagged
  frontend frontend-untagged
  kis-trader kis-trader-untagged
  s3-store s3-store-untagged
)
services=(api-websocket api-websocket-untagged frontend frontend-untagged)

echo "Retired resource cleanup mode=${mode} namespace=${namespace}"
kubectl get deployment,service -n "${namespace}"

if [[ "${mode}" == "dry-run" ]]; then
  kubectl delete deployment "${deployments[@]}" -n "${namespace}" --ignore-not-found --dry-run=server -o name
  kubectl delete service "${services[@]}" -n "${namespace}" --ignore-not-found --dry-run=server -o name
  echo "Preview only. Re-run with --apply after reviewing the names above."
  exit 0
fi

kubectl delete deployment "${deployments[@]}" -n "${namespace}" --ignore-not-found
kubectl delete service "${services[@]}" -n "${namespace}" --ignore-not-found
kubectl get deployment,service -n "${namespace}"
