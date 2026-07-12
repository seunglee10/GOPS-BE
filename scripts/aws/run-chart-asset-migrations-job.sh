#!/usr/bin/env bash
# 역할: 최신 agent image로 chart geometry PostgreSQL schema Job을 1회 실행합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
ECR_AGENT_ORCHESTRATOR_REPO="${ECR_AGENT_ORCHESTRATOR_REPO:-${ECR_AGENT_REPO:-}}"
ECR_AGENT_ORCHESTRATOR_REPO="${ECR_AGENT_ORCHESTRATOR_REPO:?ECR_AGENT_ORCHESTRATOR_REPO를 넣어주세요.}"
JOB_NAME="${CHART_ASSET_MIGRATIONS_JOB_NAME:-chart-asset-migrations}"
MANIFEST="${CHART_ASSET_MIGRATIONS_MANIFEST:-infra/k8s/base/job-chart-asset-migrations.yaml}"
TIMEOUT="${CHART_ASSET_MIGRATIONS_TIMEOUT:-600s}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
rendered="${tmp_dir}/job-chart-asset-migrations.yaml"
sed \
  -e "s#^  name: chart-asset-migrations\$#  name: ${JOB_NAME}#" \
  -e "s#^  namespace: alfaka-market-data\$#  namespace: ${K8S_NAMESPACE}#" \
  -e "s#image: YOUR_ECR_REPOSITORY/gops-agent-orchestrator:latest#image: ${ECR_AGENT_ORCHESTRATOR_REPO}:${IMAGE_TAG}#" \
  -e "s#imagePullPolicy: IfNotPresent#imagePullPolicy: Always#" \
  "${MANIFEST}" > "${rendered}"

kubectl delete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found
kubectl apply -f "${rendered}"
if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout="${TIMEOUT}"; then
  kubectl describe "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
  exit 1
fi
kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200
