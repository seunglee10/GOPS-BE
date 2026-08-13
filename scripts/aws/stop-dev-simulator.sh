#!/usr/bin/env bash
# 역할: simulator Pod는 유지한 채 dev EKS를 LIVE로 전환하고 SIM 실행 상태만 정리합니다.
set -euo pipefail

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID를 넣어주세요. 예) export AWS_ACCOUNT_ID=\"$(aws sts get-caller-identity --query Account --output text)\"}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-gops-eks-cluster}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"

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

set_live_mode() {
  kubectl exec deployment/gops-simulator -n "${K8S_NAMESPACE}" -- \
    python -c 'import json, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/control/mode",
    data=b"{\"mode\":\"live\"}",
    headers={"Content-Type": "application/json"},
    method="PUT",
)
urllib.request.urlopen(request, timeout=5).read()'
}

cleanup_replay_namespace() {
  local run_id
  run_id="$(kubectl exec statefulset/redis -n "${K8S_NAMESPACE}" -- \
    redis-cli --raw GET simulator:replay:active-run 2>/dev/null || true)"
  if [[ -n "${run_id}" ]]; then
    kubectl exec statefulset/redis -n "${K8S_NAMESPACE}" -- \
      redis-cli DEL "simulator:replay:run:${run_id}" simulator:replay:active-run >/dev/null
  fi
}

require_command aws
require_command kubectl
configure_cluster
set_live_mode || printf 'Simulator LIVE 전환 호출에 실패해 Redis 네임스페이스를 직접 정리합니다.\n' >&2
cleanup_replay_namespace

printf 'dev EKS is LIVE; simulator Pod는 다음 실행을 위해 READY 상태로 유지됩니다.\n'
printf 'realtime Redis/Kafka/Alpaca deployments were not changed.\n'
