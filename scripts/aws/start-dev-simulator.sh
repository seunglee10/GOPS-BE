#!/usr/bin/env bash
# 역할: dev EKS에서 시뮬레이터를 켜고 backend와 SIP 수집기만 시연 경로로 전환합니다.
set -Eeuo pipefail

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-<aws-account-id>}"
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

reset_simulator_to_live() {
  local live_payload='{"mode":"live"}'

  kubectl exec deployment/gops-simulator -n "${K8S_NAMESPACE}" -- \
    python -c 'import sys, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/control/mode",
    data=sys.argv[1].encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="PUT",
)
urllib.request.urlopen(request, timeout=2).read()' "${live_payload}"
}

restore_live_path() {
  local exit_code="$1"
  trap - ERR
  set +e

  printf 'Simulator start failed; restoring the live SIP path.\n' >&2
  kubectl set env deployment/alfaka-alpaca-ingestor-sip -n "${K8S_NAMESPACE}" \
    ALPACA_STREAM_BASE_URL- \
    ALPACA_COLLECTION_SYMBOLS- \
    ALPACA_CHANNELS- \
    ALPACA_ACTIVE_CHANNELS=bars,updatedBars,dailyBars,trades,quotes \
    ALPACA_MAX_TRADE_SYMBOLS- \
    ALPACA_ENFORCE_FEED_SESSION_WINDOW-
  kubectl set env deployment/gops-backend -n "${K8S_NAMESPACE}" GOPS_SIMULATOR_URL-
  kubectl scale deployment/gops-simulator --replicas=0 -n "${K8S_NAMESPACE}"
  exit "${exit_code}"
}

require_command aws
require_command kubectl
configure_cluster
trap 'restore_live_path $?' ERR

kubectl scale deployment/gops-simulator --replicas=1 -n "${K8S_NAMESPACE}"
kubectl rollout status deployment/gops-simulator -n "${K8S_NAMESPACE}" --timeout=180s
reset_simulator_to_live

kubectl set env deployment/gops-backend -n "${K8S_NAMESPACE}" \
  GOPS_SIMULATOR_URL=http://gops-simulator:8765

kubectl set env deployment/alfaka-alpaca-ingestor-sip -n "${K8S_NAMESPACE}" \
  ALPACA_STREAM_BASE_URL=ws://gops-simulator:8765 \
  ALPACA_COLLECTION_SYMBOLS=NVDA,AMD,AVGO,MU,TSM,XOM,CVX,COP \
  ALPACA_CHANNELS=trades \
  ALPACA_ACTIVE_CHANNELS= \
  ALPACA_MAX_TRADE_SYMBOLS=8 \
  ALPACA_ENFORCE_FEED_SESSION_WINDOW=false

kubectl rollout status deployment/gops-backend -n "${K8S_NAMESPACE}" --timeout=300s
kubectl rollout status deployment/alfaka-alpaca-ingestor-sip -n "${K8S_NAMESPACE}" --timeout=300s

trap - ERR
printf 'EKS simulator is ready. LIVE→SIM 토글을 누르면 5초 뒤 속보가 공개됩니다.\n'
printf '종료 후 반드시 AWS_PROFILE=%s scripts/aws/stop-dev-simulator.sh 를 실행하세요.\n' "${AWS_PROFILE:-gops-dev}"
