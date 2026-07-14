#!/usr/bin/env bash
# 역할: dev EKS의 backend/SIP 수집기를 live 경로로 복구하고 시뮬레이터 Pod를 0개로 내립니다.
set -euo pipefail

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

require_command aws
require_command kubectl
configure_cluster

kubectl set env deployment/alfaka-alpaca-ingestor-sip -n "${K8S_NAMESPACE}" \
  ALPACA_STREAM_BASE_URL- \
  ALPACA_COLLECTION_SYMBOLS- \
  ALPACA_CHANNELS- \
  ALPACA_ACTIVE_CHANNELS=bars,updatedBars,dailyBars,trades,quotes \
  ALPACA_MAX_TRADE_SYMBOLS- \
  ALPACA_ENFORCE_FEED_SESSION_WINDOW-

kubectl set env deployment/gops-backend -n "${K8S_NAMESPACE}" GOPS_SIMULATOR_URL-

kubectl set env deployment/alfaka-market-processor deployment/gops-backend -n "${K8S_NAMESPACE}" \
  ORDER_FLOW_PINNED_SYMBOLS=NVDA,AMZN,MU,AAPL,GOOGL

kubectl set env deployment/trade-condition-executor -n "${K8S_NAMESPACE}" \
  TRADE_CONDITION_EXECUTION_MODE=demo

kubectl rollout status deployment/alfaka-alpaca-ingestor-sip -n "${K8S_NAMESPACE}" --timeout=300s
kubectl rollout status deployment/gops-backend -n "${K8S_NAMESPACE}" --timeout=300s
kubectl rollout status deployment/alfaka-market-processor -n "${K8S_NAMESPACE}" --timeout=300s
kubectl rollout status deployment/trade-condition-executor -n "${K8S_NAMESPACE}" --timeout=300s
kubectl scale deployment/gops-simulator --replicas=0 -n "${K8S_NAMESPACE}"

printf 'Live Alpaca SIP path restored; EKS simulator replicas are now 0.\n'
