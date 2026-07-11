#!/usr/bin/env bash
# 역할: 최신 agent image로 chart asset PostgreSQL schema/sync/parity Job을 1회 실행합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
ECR_AGENT_ORCHESTRATOR_REPO="${ECR_AGENT_ORCHESTRATOR_REPO:-${ECR_AGENT_REPO:-}}"
ECR_AGENT_ORCHESTRATOR_REPO="${ECR_AGENT_ORCHESTRATOR_REPO:?ECR_AGENT_ORCHESTRATOR_REPO를 넣어주세요.}"
ACTION="${CHART_ASSET_MIGRATION_ACTION:-migrate}"
PRUNE="${CHART_ASSET_MIGRATION_PRUNE:-false}"
JOB_NAME="${CHART_ASSET_MIGRATIONS_JOB_NAME:-chart-asset-migrations}"
MANIFEST="${CHART_ASSET_MIGRATIONS_MANIFEST:-infra/k8s/base/job-chart-asset-migrations.yaml}"
TIMEOUT="${CHART_ASSET_MIGRATIONS_TIMEOUT:-600s}"
BUILDER_DEPLOYMENT="chart-asset-builder"
BACKEND_LABEL="app=gops-backend"

case "${ACTION}" in
  migrate|sync|verify) ;;
  *) echo "CHART_ASSET_MIGRATION_ACTION must be migrate, sync, or verify" >&2; exit 2 ;;
esac
case "${PRUNE}" in
  true|false) ;;
  *) echo "CHART_ASSET_MIGRATION_PRUNE must be true or false" >&2; exit 2 ;;
esac
if [[ ${#JOB_NAME} -gt 63 || ! "${JOB_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "CHART_ASSET_MIGRATIONS_JOB_NAME must be a Kubernetes DNS label" >&2
  exit 2
fi
if [[ ${#K8S_NAMESPACE} -gt 63 || ! "${K8S_NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "K8S_NAMESPACE must be a Kubernetes DNS label" >&2
  exit 2
fi

require_builder_stopped() {
  local desired_replicas
  local remaining_pods

  if ! desired_replicas="$(
    kubectl get "deployment/${BUILDER_DEPLOYMENT}" -n "${K8S_NAMESPACE}" \
      -o jsonpath='{.spec.replicas}'
  )"; then
    echo "Cannot verify ${BUILDER_DEPLOYMENT}; refusing ${ACTION}." >&2
    exit 2
  fi
  if [[ "${desired_replicas}" != "0" ]]; then
    echo "Scale deployment/${BUILDER_DEPLOYMENT} to 0 before ${ACTION}." >&2
    exit 2
  fi

  remaining_pods="$(
    kubectl get pods -n "${K8S_NAMESPACE}" -l app=chart-asset-builder -o name
  )"
  if [[ -n "${remaining_pods}" ]]; then
    echo "Wait for all ${BUILDER_DEPLOYMENT} pods to terminate before ${ACTION}." >&2
    exit 2
  fi
}

require_backend_maintenance() {
  local backend_pods
  local pod
  local maintenance

  backend_pods="$(
    kubectl get pods -n "${K8S_NAMESPACE}" -l "${BACKEND_LABEL}" -o name
  )"
  if [[ -z "${backend_pods}" ]]; then
    echo "No ready gops-backend pod is available to verify maintenance mode." >&2
    exit 2
  fi
  for pod in ${backend_pods}; do
    if ! maintenance="$(
      kubectl exec -n "${K8S_NAMESPACE}" "${pod}" -c gops-backend -- \
        printenv CHART_ASSET_STORAGE_MAINTENANCE
    )"; then
      echo "Cannot verify maintenance mode in ${pod}; refusing ${ACTION}." >&2
      exit 2
    fi
    if [[ "${maintenance}" != "true" ]]; then
      echo "Restart every gops-backend pod with CHART_ASSET_STORAGE_MAINTENANCE=true before ${ACTION}." >&2
      exit 2
    fi
  done
}

if [[ "${ACTION}" == "sync" || "${ACTION}" == "verify" ]]; then
  require_backend_maintenance
  require_builder_stopped
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

rendered="${tmp_dir}/job-chart-asset-migrations.yaml"
sed \
  -e "s#^  name: chart-asset-migrations\$#  name: ${JOB_NAME}#" \
  -e "s#^  namespace: alfaka-market-data\$#  namespace: ${K8S_NAMESPACE}#" \
  -e "s#image: YOUR_ECR_REPOSITORY/gops-agent-orchestrator:latest#image: ${ECR_AGENT_ORCHESTRATOR_REPO}:${IMAGE_TAG}#" \
  -e "s#imagePullPolicy: IfNotPresent#imagePullPolicy: Always#" \
  -e "s#value: migrate#value: ${ACTION}#" \
  -e "s#value: \"false\"#value: \"${PRUNE}\"#" \
  "${MANIFEST}" > "${rendered}"

echo "Running ${JOB_NAME} action=${ACTION} prune=${PRUNE}"
kubectl delete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found
kubectl apply -f "${rendered}"

if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout="${TIMEOUT}"; then
  kubectl describe "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
  exit 1
fi

kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200
