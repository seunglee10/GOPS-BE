#!/usr/bin/env bash
# 역할: dev EKS에서 고정 틱 데이터셋 importer Job을 한 번 실행하고 READY를 검증합니다.
set -Eeuo pipefail

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-<aws-account-id>}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-gops-eks-cluster}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
DATASET_ID="${SIM_REPLAY_DATASET_ID:-sp500-top20-20260715-kst-v1}"
JOB_NAME="gops-simulator-replay-import"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOB_MANIFEST="${REPO_ROOT}/infra/k8s/base/job-simulator-replay-import.yaml"
CLICKHOUSE_SCHEMA="${REPO_ROOT}/infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql"

require_command() {
  command -v "$1" >/dev/null 2>&1 || { printf 'Required command not found: %s\n' "$1" >&2; exit 1; }
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

dataset_status() {
  kubectl exec statefulset/clickhouse -n "${K8S_NAMESPACE}" -- \
    sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "$1"' -- \
    "SELECT concat(status, ':', toString(total_events)) FROM market_data.simulation_replay_datasets FINAL WHERE dataset_id = '${DATASET_ID}' LIMIT 1 FORMAT TSVRaw"
}

require_command aws
require_command kubectl
configure_cluster

kubectl exec -i statefulset/clickhouse -n "${K8S_NAMESPACE}" -- \
  sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
  < "${CLICKHOUSE_SCHEMA}"

current_status="$(dataset_status)"
if [[ "${current_status%%:*}" == "READY" && "${current_status##*:}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Replay dataset is already READY: %s (%s events)\n' "${DATASET_ID}" "${current_status##*:}"
  exit 0
fi

simulator_image="$(kubectl get deployment/gops-simulator -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[0].image}')"
if [[ -z "${simulator_image}" ]]; then
  printf 'gops-simulator image is unavailable; deploy the simulator first.\n' >&2
  exit 1
fi

kubectl delete job "${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found --wait=true
kubectl set image -f "${JOB_MANIFEST}" replay-import="${simulator_image}" --local -o yaml \
  | kubectl apply -f -
kubectl patch job "${JOB_NAME}" -n "${K8S_NAMESPACE}" --type merge -p '{"spec":{"suspend":false}}'

printf 'Replay import started with image %s\n' "${simulator_image}"
kubectl wait --for=condition=Ready pod -l "job-name=${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout=300s
kubectl logs -f "job/${JOB_NAME}" -n "${K8S_NAMESPACE}"
kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout=24h

final_status="$(dataset_status)"
if [[ "${final_status%%:*}" != "READY" || ! "${final_status##*:}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Replay import did not become READY: %s\n' "${final_status:-missing}" >&2
  exit 1
fi
printf 'Replay dataset READY: %s (%s events)\n' "${DATASET_ID}" "${final_status##*:}"
