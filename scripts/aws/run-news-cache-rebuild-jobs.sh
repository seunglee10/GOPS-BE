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

timeout_seconds() {
  local value="${1}"
  case "${value}" in
    *s) printf '%s\n' "${value%s}" ;;
    *m) printf '%s\n' "$(( ${value%m} * 60 ))" ;;
    *h) printf '%s\n' "$(( ${value%h} * 3600 ))" ;;
    *) printf '%s\n' "${value}" ;;
  esac
}

print_job_debug() {
  local job_name="$1"

  kubectl describe "job/${job_name}" -n "${K8S_NAMESPACE}" || true
  kubectl get pods -n "${K8S_NAMESPACE}" -l "job-name=${job_name}" -o wide || true
  kubectl get events -n "${K8S_NAMESPACE}" \
    --field-selector "involvedObject.kind=Pod" \
    --sort-by=.lastTimestamp || true
  kubectl logs -n "${K8S_NAMESPACE}" \
    -l "job-name=${job_name}" \
    --all-containers=true \
    --tail="${LOG_TAIL}" \
    --ignore-errors=true || true
  kubectl logs -n "${K8S_NAMESPACE}" \
    -l "job-name=${job_name}" \
    --all-containers=true \
    --previous=true \
    --tail="${LOG_TAIL}" \
    --ignore-errors=true || true
}

wait_for_rebuild_job() {
  local job_name="$1"
  local deadline_seconds
  local started_at

  deadline_seconds="$(timeout_seconds "${WAIT_TIMEOUT}")"
  started_at="$(date +%s)"
  while true; do
    local succeeded
    local failed_condition
    local now

    succeeded="$(kubectl get "job/${job_name}" -n "${K8S_NAMESPACE}" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
    failed_condition="$(kubectl get "job/${job_name}" -n "${K8S_NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true)"
    if [[ "${succeeded:-0}" != "" && "${succeeded:-0}" -gt 0 ]]; then
      return 0
    fi
    if [[ "${failed_condition}" == "True" ]]; then
      echo "News cache rebuild failed: job/${job_name}" >&2
      print_job_debug "${job_name}"
      return 1
    fi
    now="$(date +%s)"
    if (( now - started_at > deadline_seconds )); then
      echo "Timed out waiting for job/${job_name} after ${WAIT_TIMEOUT}" >&2
      print_job_debug "${job_name}"
      return 1
    fi
    sleep 5
  done
}

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
        "CLICKHOUSE_ENSURE_SCHEMA_ON_START=false" \
        "NEWS_INTELLIGENCE_REBUILD_REWRITE_CLICKHOUSE=false" \
        --local \
        -o yaml \
    > "${rendered}"

  kubectl apply -f "${rendered}"

  if wait_for_rebuild_job "${job_name}"; then
    kubectl logs "job/${job_name}" -n "${K8S_NAMESPACE}" --tail="${LOG_TAIL}" || true
    return 0
  fi
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
