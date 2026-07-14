#!/usr/bin/env bash
# 역할: Git diff를 GOPS 이미지/Deployment 단위로 변환합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

BASE_SHA="${BASE_SHA:-${GITHUB_EVENT_BEFORE:-}}"
HEAD_SHA="${HEAD_SHA:-${GITHUB_SHA:-HEAD}}"
EVENT_NAME="${EVENT_NAME:-${GITHUB_EVENT_NAME:-}}"
REQUESTED_SERVICES="${REQUESTED_SERVICES:-${SERVICES:-}}"
WORKFLOW_FILE="${WORKFLOW_FILE:-deploy-dev.yml}"

SELECTED_KEYS=""
SELECTED_DEPLOYMENTS=""

service_already_selected() {
  local expected="$1"
  local selected

  for selected in ${SELECTED_KEYS}; do
    if [[ "${selected}" == "${expected}" ]]; then
      return 0
    fi
  done
  return 1
}

deployment_already_selected() {
  local expected="$1"
  local selected

  for selected in ${SELECTED_DEPLOYMENTS}; do
    if [[ "${selected}" == "${expected}" ]]; then
      return 0
    fi
  done
  return 1
}

write_output() {
  local name="$1"
  local value="$2"

  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "${name}" "${value}" >> "${GITHUB_OUTPUT}"
  fi
  printf '%s=%s\n' "${name}" "${value}"
}

add_deployment() {
  local deployment="$1"

  if deployment_already_selected "${deployment}"; then
    return
  fi

  SELECTED_DEPLOYMENTS="${SELECTED_DEPLOYMENTS}${SELECTED_DEPLOYMENTS:+ }${deployment}"
}

add_service() {
  local raw_key="$1"
  local key
  local deployment

  key="$(gops_normalize_service_key "${raw_key}")"
  if ! gops_service_exists "${key}"; then
    printf 'Unknown service: %s\n' "${raw_key}" >&2
    exit 1
  fi

  if service_already_selected "${key}"; then
    return
  fi

  SELECTED_KEYS="${SELECTED_KEYS}${SELECTED_KEYS:+ }${key}"

  while IFS= read -r deployment; do
    add_deployment "${deployment}"
  done < <(gops_deployments_for_service "${key}")

  # The trusted AI coach worker reads the order-owned schema. Always build the
  # migration image together with agent analytics so schema migrations can run
  # before the new worker is rolled out, including manual service selection.
  if [[ "${key}" == "agent-orchestrator" ]]; then
    add_service order-worker
  fi
}

add_all_services() {
  local key

  while IFS=$'\t' read -r key _repository _env_var _dockerfile; do
    add_service "${key}"
  done < <(gops_image_entries)
}

select_requested_services() {
  local requested_service

  read -r -a requested_services <<< "${REQUESTED_SERVICES//,/ }"
  for requested_service in "${requested_services[@]}"; do
    if [[ -z "${requested_service}" ]]; then
      continue
    fi
    if [[ "${requested_service}" == "all" || "${requested_service}" == "*" ]]; then
      add_all_services
      continue
    fi
    add_service "${requested_service}"
  done
}

latest_successful_workflow_sha() {
  local repository="${GITHUB_REPOSITORY:-}"
  local ref_name="${GITHUB_REF_NAME:-}"
  local token="${GITHUB_TOKEN:-}"
  local api_url

  if [[ -z "${repository}" || -z "${ref_name}" || -z "${token}" ]]; then
    return 1
  fi

  api_url="https://api.github.com/repos/${repository}/actions/workflows/${WORKFLOW_FILE}/runs?branch=${ref_name}&status=success&per_page=20"
  curl -fsSL \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${token}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${api_url}" \
    | python3 -c 'import json, sys
payload = json.load(sys.stdin)
for run in payload.get("workflow_runs", []):
    head_sha = run.get("head_sha")
    if head_sha:
        print(head_sha)
        raise SystemExit(0)
raise SystemExit(1)'
}

resolve_base_sha() {
  local detected_base

  if [[ -n "${BASE_SHA}" && ! "${BASE_SHA}" =~ ^0+$ ]]; then
    return 0
  fi
  if [[ "${EVENT_NAME}" != "workflow_dispatch" ]]; then
    return 1
  fi

  if detected_base="$(latest_successful_workflow_sha)"; then
    BASE_SHA="${detected_base}"
    echo "Manual workflow dispatch without service input: comparing from last successful deploy ${BASE_SHA}."
    return 0
  fi

  return 1
}

select_services_for_path() {
  local path="$1"

  case "${path}" in
    requirements.txt | .dockerignore)
      add_all_services
      ;;
    apps/gops-frontend/* | apps/chart-engine/* | infra/docker/nginx/*)
      add_service frontend
      ;;
    shared/chart-contract/*)
      add_service frontend
      add_service agent-orchestrator
      ;;
    systems/api-server/pods/api-server/gops-backend/app/contracts/*)
      add_service backend
      add_service agent-orchestrator
      ;;
    systems/api-server/pods/api-server/gops-backend/requirements.txt)
      add_service backend
      add_service agent-orchestrator
      ;;
    systems/api-server/*)
      add_service backend
      ;;
    systems/agent-orchestration/shared/* | systems/agent-orchestration/config/*)
      add_service backend
      add_service agent-orchestrator
      ;;
    systems/agent-orchestration/*)
      add_service agent-orchestrator
      ;;
    systems/simulator/*)
      add_service simulator
      ;;
    systems/market-data/config/*)
      add_service backend
      add_service agent-orchestrator
      add_service market-ingestor
      add_service market-processor
      add_service market-storage
      ;;
    systems/market-data/shared/*)
      add_service backend
      add_service market-ingestor
      add_service market-processor
      add_service market-storage
      add_service agent-orchestrator
      add_service order-worker
      ;;
    systems/market-data/pods/market-ingestor/* | systems/market-data/pods/news-ingestor/*)
      add_service market-ingestor
      ;;
    systems/market-data/pods/market-processor/* | systems/market-data/pods/feed-session-controller/* | systems/market-data/pods/subscription-controller/* | systems/market-data/jobs/symbol-registry-sync/* | systems/market-data/jobs/coverage-repair/*)
      add_service market-processor
      ;;
    systems/market-data/pods/s3-sink/* | systems/market-data/pods/clickhouse-loader/* | systems/market-data/pods/news-intelligence-worker/* | systems/market-data/pods/news-daily-summary-worker/* | systems/market-data/jobs/news-backfill/* | systems/market-data/jobs/news-intelligence-rebuild/* | systems/market-data/jobs/news-daily-summary-rebuild/*)
      add_service market-storage
      ;;
    systems/order/shared/*)
      add_service backend
      add_service order-worker
      add_service kis-adapter
      ;;
    systems/order/pods/order-outbox/* | systems/order/pods/paper-order-matcher/* | systems/order/jobs/*)
      add_service order-worker
      ;;
    systems/order/pods/kis-adapter/*)
      add_service kis-adapter
      ;;
    infra/docker/Dockerfile.gops-agent-orchestrator)
      add_service agent-orchestrator
      ;;
    infra/docker/Dockerfile.gops-backend)
      add_service backend
      ;;
    infra/docker/Dockerfile.gops-frontend)
      add_service frontend
      ;;
    infra/docker/Dockerfile.gops-kis-adapter)
      add_service kis-adapter
      ;;
    infra/docker/Dockerfile.gops-market-ingestor)
      add_service market-ingestor
      ;;
    infra/docker/Dockerfile.gops-market-processor)
      add_service market-processor
      ;;
    infra/docker/Dockerfile.gops-market-storage)
      add_service market-storage
      ;;
    infra/docker/Dockerfile.gops-order-worker)
      add_service order-worker
      ;;
    infra/docker/Dockerfile.gops-simulator)
      add_service simulator
      ;;
    infra/k8s/overlays/aws/scheduled/cronjob-chart-geometry-build.yaml)
      add_service agent-orchestrator
      ;;
    infra/k8s/overlays/aws/scheduled/cronjob-order-flow-daily-rollup.yaml)
      add_service market-processor
      ;;
    infra/k8s/overlays/aws/scheduled/cronjob-notification-schedules.yaml)
      add_service backend
      ;;
    infra/k8s/overlays/aws/scheduled/cronjob-sec-fundamentals-sync.yaml | infra/k8s/overlays/aws/scheduled/cronjob-yahoo-estimates-sync.yaml | infra/k8s/overlays/aws/scheduled/externalsecret-sec-fundamentals.yaml)
      add_service market-storage
      ;;
    infra/k8s/overlays/aws/scheduled/*)
      add_service agent-orchestrator
      add_service market-processor
      add_service market-storage
      ;;
    infra/k8s/base/platform/* | infra/k8s/overlays/aws-incluster-platform/* | infra/k8s/overlays/aws-incluster-app-rebuild/*)
      ;;
    .github/workflows/deploy-dev.yml | scripts/aws/*)
      ;;
    infra/docker/* | infra/k8s/base/* | infra/k8s/base/stream-processor/* | infra/k8s/overlays/aws-incluster-app/* | infra/k8s/overlays/aws-incluster-app-ci/*)
      add_all_services
      ;;
  esac
}

if [[ -n "${REQUESTED_SERVICES}" ]]; then
  echo "Manual service selection: ${REQUESTED_SERVICES}"
  select_requested_services
elif ! resolve_base_sha; then
  echo "No automatic diff base SHA found: selecting all app services."
  add_all_services
elif [[ -z "${BASE_SHA}" || "${BASE_SHA}" =~ ^0+$ ]]; then
  echo "No usable base SHA: selecting all app services."
  add_all_services
elif ! git rev-parse --verify "${BASE_SHA}^{commit}" >/dev/null 2>&1; then
  echo "Base SHA not found locally: selecting all app services."
  add_all_services
else
  while IFS= read -r changed_path; do
    if [[ -z "${changed_path}" ]]; then
      continue
    fi
    echo "Changed path: ${changed_path}"
    select_services_for_path "${changed_path}"
  done < <(git diff --name-only "${BASE_SHA}" "${HEAD_SHA}")
fi

services="${SELECTED_KEYS}"
deployments="${SELECTED_DEPLOYMENTS}"
has_services="false"
smoke_frontend="false"
smoke_backend="false"
order_migrations_required="false"
if [[ -n "${SELECTED_KEYS}" ]]; then
  has_services="true"
fi
if service_already_selected frontend; then
  smoke_frontend="true"
fi
if service_already_selected backend; then
  smoke_backend="true"
fi
if service_already_selected order-worker; then
  order_migrations_required="true"
fi
write_output "has_services" "${has_services}"
write_output "services" "${services}"
write_output "deployments" "${deployments}"
write_output "smoke_frontend" "${smoke_frontend}"
write_output "smoke_backend" "${smoke_backend}"
write_output "order_migrations_required" "${order_migrations_required}"
