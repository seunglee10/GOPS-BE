#!/usr/bin/env bash
# 역할: 자동 배포된 dev EKS simulator의 데이터셋과 LIVE 대기 상태를 수동 점검합니다.
set -Eeuo pipefail

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-<aws-account-id>}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-gops-eks-cluster}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
DATASET_ID="${SIM_REPLAY_DATASET_ID:-sp500-top20-plus-amd-mu-20260715-kst-v2}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLICKHOUSE_SCHEMA="${REPO_ROOT}/infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

configure_cluster() {
  local actual_account
  actual_account="$(aws sts get-caller-identity --query Account --output text)"
  if [[ "${actual_account}" != "${AWS_ACCOUNT_ID}" ]]; then
    printf 'AWS account mismatch: expected %s, got %s\n' "${AWS_ACCOUNT_ID}" "${actual_account}" >&2
    exit 1
  fi
  aws eks update-kubeconfig --name "${EKS_CLUSTER_NAME}" --region "${AWS_REGION}" >/dev/null
  kubectl get namespace "${K8S_NAMESPACE}" >/dev/null
}

apply_replay_schema() {
  kubectl exec -i statefulset/clickhouse -n "${K8S_NAMESPACE}" -- \
    sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
    < "${CLICKHOUSE_SCHEMA}"
}

require_ready_dataset() {
  local result status total_events
  result="$(kubectl exec statefulset/clickhouse -n "${K8S_NAMESPACE}" -- \
    sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "$1"' -- \
    "SELECT concat(status, ':', toString(total_events)) FROM market_data.simulation_replay_datasets FINAL WHERE dataset_id = '${DATASET_ID}' LIMIT 1 FORMAT TSVRaw")"
  status="${result%%:*}"
  total_events="${result##*:}"
  if [[ "${status}" != "READY" || ! "${total_events}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Replay dataset is not READY: dataset=%s status=%s events=%s\n' \
      "${DATASET_ID}" "${status:-missing}" "${total_events:-0}" >&2
    exit 1
  fi
  printf 'READY dataset verified: %s (%s events)\n' "${DATASET_ID}" "${total_events}"
}

set_simulator_mode() {
  local mode="$1"
  kubectl exec deployment/gops-simulator -n "${K8S_NAMESPACE}" -- \
    python -c 'import json, sys, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/control/mode",
    data=json.dumps({"mode": sys.argv[1]}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="PUT",
)
payload = json.loads(urllib.request.urlopen(request, timeout=5).read())
if payload.get("mode") != sys.argv[1]:
    raise SystemExit(f"mode switch failed: {payload}")' "${mode}"
}

verify_simulator_health() {
  kubectl exec deployment/gops-simulator -n "${K8S_NAMESPACE}" -- \
    python -c 'import json, urllib.request
payload = json.loads(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5).read())
if not payload.get("datasetReady"):
    raise SystemExit(f"dataset is not ready: {payload}")
print(f"simulator health ok: {payload.get('"'"'datasetId'"'"')} events={payload.get('"'"'totalEventCount'"'"')}")'
}

require_command aws
require_command kubectl
configure_cluster
apply_replay_schema
require_ready_dataset

kubectl rollout status deployment/gops-simulator -n "${K8S_NAMESPACE}" --timeout=300s
verify_simulator_health
set_simulator_mode live

printf 'dev EKS tick replay is READY in LIVE 대기 상태: %s\n' "${DATASET_ID}"
printf '일반 app 배포가 simulator Pod와 backend 연결을 선언적으로 유지합니다.\n'
printf '화면 상단의 플레이 버튼을 누르면 새 run을 준비하고 즉시 재생합니다.\n'
