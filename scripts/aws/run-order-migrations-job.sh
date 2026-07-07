#!/usr/bin/env bash
# 역할: 최신 order-worker 이미지로 Postgres SQL migrations Job을 1회 실행합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
ECR_ORDER_WORKER_REPO="${ECR_ORDER_WORKER_REPO:?ECR_ORDER_WORKER_REPO를 넣어주세요.}"
JOB_NAME="${ORDER_MIGRATIONS_JOB_NAME:-order-migrations}"
MANIFEST="${ORDER_MIGRATIONS_MANIFEST:-infra/k8s/base/job-order-migrations.yaml}"
TIMEOUT="${ORDER_MIGRATIONS_TIMEOUT:-300s}"

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

rendered="${tmp_dir}/job-order-migrations.yaml"
sed \
  -e "s#image: YOUR_ECR_REPOSITORY/gops-order-worker:latest#image: ${ECR_ORDER_WORKER_REPO}:${IMAGE_TAG}#" \
  -e "s#imagePullPolicy: IfNotPresent#imagePullPolicy: Always#" \
  "${MANIFEST}" > "${rendered}"

echo "Running ${JOB_NAME} with ${ECR_ORDER_WORKER_REPO}:${IMAGE_TAG}"
kubectl delete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found
kubectl apply -f "${rendered}"

if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout="${TIMEOUT}"; then
  kubectl describe "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
  exit 1
fi

kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
