#!/usr/bin/env bash
# 역할: 새 API 이미지로 AI 기업저널 ClickHouse 스키마를 앱 rollout 전에 멱등 적용합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
ECR_API_SERVER_REPO="${ECR_API_SERVER_REPO:?ECR_API_SERVER_REPO를 넣어주세요.}"
JOB_NAME="${COMPANY_JOURNAL_MIGRATIONS_JOB_NAME:-company-journal-migrations}"
MANIFEST="${COMPANY_JOURNAL_MIGRATIONS_MANIFEST:-infra/k8s/base/job-company-journal-migrations.yaml}"
TIMEOUT="${COMPANY_JOURNAL_MIGRATIONS_TIMEOUT:-300s}"

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf "${tmp_dir}"; }
trap cleanup EXIT

rendered="${tmp_dir}/job-company-journal-migrations.yaml"
sed \
  -e "s#image: YOUR_ECR_REPOSITORY/gops-api-server:latest#image: ${ECR_API_SERVER_REPO}:${IMAGE_TAG}#" \
  -e "s#imagePullPolicy: IfNotPresent#imagePullPolicy: Always#" \
  "${MANIFEST}" > "${rendered}"

echo "Running ${JOB_NAME} with ${ECR_API_SERVER_REPO}:${IMAGE_TAG}"
kubectl delete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found
kubectl apply -f "${rendered}"
if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout="${TIMEOUT}"; then
  kubectl describe "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
  exit 1
fi
kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
