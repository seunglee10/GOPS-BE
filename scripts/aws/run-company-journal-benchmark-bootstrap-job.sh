#!/usr/bin/env bash
# 역할: 새 market-processor 이미지로 기업저널 비교 ETF의 2년 일봉 누락분을 멱등 보강합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
ECR_MARKET_PROCESSOR_REPO="${ECR_MARKET_PROCESSOR_REPO:?ECR_MARKET_PROCESSOR_REPO를 넣어주세요.}"
JOB_NAME="${COMPANY_JOURNAL_BENCHMARK_JOB_NAME:-company-journal-benchmark-bootstrap}"
MANIFEST="${COMPANY_JOURNAL_BENCHMARK_MANIFEST:-infra/k8s/base/job-company-journal-benchmark-bootstrap.yaml}"
TIMEOUT="${COMPANY_JOURNAL_BENCHMARK_TIMEOUT:-1800s}"

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf "${tmp_dir}"; }
trap cleanup EXIT

rendered="${tmp_dir}/job-company-journal-benchmark-bootstrap.yaml"
sed \
  -e "s#image: YOUR_ECR_REPOSITORY/gops-market-processor:latest#image: ${ECR_MARKET_PROCESSOR_REPO}:${IMAGE_TAG}#" \
  -e "s#imagePullPolicy: IfNotPresent#imagePullPolicy: Always#" \
  "${MANIFEST}" > "${rendered}"

echo "Running ${JOB_NAME} with ${ECR_MARKET_PROCESSOR_REPO}:${IMAGE_TAG}"
kubectl delete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --ignore-not-found
kubectl apply -f "${rendered}"
if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --timeout="${TIMEOUT}"; then
  kubectl describe "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
  exit 1
fi
kubectl logs "job/${JOB_NAME}" -n "${K8S_NAMESPACE}" --all-containers=true --tail=200 || true
