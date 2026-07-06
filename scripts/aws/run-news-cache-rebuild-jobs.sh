#!/usr/bin/env bash
# 역할: 배포된 market-storage image로 기존 30일 뉴스 Redis cache를 재적재합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
WAIT_TIMEOUT="${NEWS_CACHE_REBUILD_WAIT_TIMEOUT:-3600s}"
LOG_TAIL="${NEWS_CACHE_REBUILD_LOG_TAIL:-200}"

market_storage_image="${ECR_MARKET_STORAGE_REPO:-$(gops_image_url_for_key market-storage)}:${IMAGE_TAG}"

run_rebuild_job() {
  local manifest="$1"
  local job_name="$2"
  local container_name="$3"
  local dry_run_env="$4"
  local rendered

  rendered="$(mktemp)"
  trap 'rm -f "${rendered}"' RETURN

  echo "Rebuilding news cache: job/${job_name} image=${market_storage_image}"
  kubectl delete "job/${job_name}" \
    -n "${K8S_NAMESPACE}" \
    --ignore-not-found=true \
    --wait=true

  kubectl set image \
    -f "${manifest}" \
    "${container_name}=${market_storage_image}" \
    --local \
    -o yaml \
    | kubectl set env \
        -f - \
        "${dry_run_env}=false" \
        --local \
        -o yaml \
    > "${rendered}"

  kubectl apply -f "${rendered}"

  if kubectl wait --for=condition=complete "job/${job_name}" -n "${K8S_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"; then
    kubectl logs "job/${job_name}" -n "${K8S_NAMESPACE}" --tail="${LOG_TAIL}" || true
    return 0
  fi

  echo "News cache rebuild failed: job/${job_name}" >&2
  kubectl describe "job/${job_name}" -n "${K8S_NAMESPACE}" || true
  kubectl logs "job/${job_name}" -n "${K8S_NAMESPACE}" --all-containers=true --tail="${LOG_TAIL}" || true
  return 1
}

run_rebuild_job \
  "infra/k8s/base/job-news-intelligence-rebuild.yaml" \
  "alfaka-news-intelligence-rebuild" \
  "news-intelligence-rebuild" \
  "NEWS_INTELLIGENCE_REBUILD_DRY_RUN"

run_rebuild_job \
  "infra/k8s/base/job-news-daily-summary-rebuild.yaml" \
  "alfaka-news-daily-summary-rebuild" \
  "news-daily-summary-rebuild" \
  "NEWS_DAILY_SUMMARY_REBUILD_DRY_RUN"
