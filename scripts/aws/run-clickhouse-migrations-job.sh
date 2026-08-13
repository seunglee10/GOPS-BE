#!/usr/bin/env bash
# 역할: 새 market-storage 이미지의 버전형 ClickHouse 마이그레이션을 앱 rollout 전에 적용합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
ECR_MARKET_STORAGE_REPO="${ECR_MARKET_STORAGE_REPO:?ECR_MARKET_STORAGE_REPO를 넣어주세요.}"
JOB_NAME="${CLICKHOUSE_MIGRATIONS_JOB_NAME:-clickhouse-migrations}"
MANIFEST="${CLICKHOUSE_MIGRATIONS_MANIFEST:-infra/k8s/base/job-clickhouse-migrations.yaml}"
TIMEOUT="${CLICKHOUSE_MIGRATIONS_TIMEOUT:-600s}"

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf "${tmp_dir}"; }
trap cleanup EXIT

rendered="${tmp_dir}/job-clickhouse-migrations.yaml"
sed \
  -e "s#image: YOUR_ECR_REPOSITORY/gops-market-storage:latest#image: ${ECR_MARKET_STORAGE_REPO}:${IMAGE_TAG}#" \
  -e "s#imagePullPolicy: IfNotPresent#imagePullPolicy: Always#" \
  "${MANIFEST}" > "${rendered}"

echo "Running ${JOB_NAME} with ${ECR_MARKET_STORAGE_REPO}:${IMAGE_TAG}"
kubectl delete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found
kubectl apply -f "${rendered}"
if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout="${TIMEOUT}"; then
  kubectl describe "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
  exit 1
fi
kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
