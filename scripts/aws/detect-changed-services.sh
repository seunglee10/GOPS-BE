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

declare -A SELECTED=()
declare -A DEPLOYMENT_SELECTED=()
declare -a SELECTED_KEYS=()
declare -a SELECTED_DEPLOYMENTS=()

write_output() {
  local name="$1"
  local value="$2"

  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "${name}" "${value}" >> "${GITHUB_OUTPUT}"
  fi
  printf '%s=%s\n' "${name}" "${value}"
}

join_by_space() {
  local IFS=" "
  echo "$*"
}

add_deployment() {
  local deployment="$1"

  if [[ -n "${DEPLOYMENT_SELECTED[${deployment}]:-}" ]]; then
    return
  fi

  DEPLOYMENT_SELECTED["${deployment}"]=1
  SELECTED_DEPLOYMENTS+=("${deployment}")
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

  if [[ -n "${SELECTED[${key}]:-}" ]]; then
    return
  fi

  SELECTED["${key}"]=1
  SELECTED_KEYS+=("${key}")

  while IFS= read -r deployment; do
    add_deployment "${deployment}"
  done < <(gops_deployments_for_service "${key}")
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
    add_service "${requested_service}"
  done
}

select_services_for_path() {
  local path="$1"

  case "${path}" in
    requirements.txt | .dockerignore)
      add_all_services
      ;;
    apps/gops-frontend/* | apps/chart-engine/* | shared/chart-contract/* | infra/docker/nginx/*)
      add_service frontend
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
    systems/agent-orchestration/*)
      add_service agent-orchestrator
      ;;
    systems/market-data/config/*)
      add_service backend
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
      ;;
    systems/market-data/pods/market-ingestor/* | systems/market-data/pods/news-ingestor/*)
      add_service market-ingestor
      ;;
    systems/market-data/pods/market-processor/* | systems/market-data/pods/feed-session-controller/* | systems/market-data/pods/subscription-controller/* | systems/market-data/jobs/symbol-registry-sync/* | systems/market-data/jobs/coverage-repair/*)
      add_service market-processor
      ;;
    systems/market-data/pods/s3-sink/* | systems/market-data/pods/clickhouse-loader/* | systems/market-data/pods/news-intelligence-worker/* | systems/market-data/jobs/news-backfill/* | systems/market-data/jobs/news-intelligence-rebuild/*)
      add_service market-storage
      ;;
    systems/order/shared/*)
      add_service backend
      add_service order-worker
      add_service kis-adapter
      ;;
    systems/order/pods/order-outbox/* | systems/order/jobs/*)
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
    infra/k8s/base/platform/* | infra/k8s/overlays/aws-incluster-platform/*)
      ;;
    infra/docker/* | infra/k8s/base/* | infra/k8s/base/stream-processor/* | infra/k8s/overlays/aws-incluster-app/* | infra/k8s/overlays/aws-incluster-app-ci/* | .github/workflows/deploy-dev.yml | scripts/aws/*)
      add_all_services
      ;;
  esac
}

if [[ -n "${REQUESTED_SERVICES}" ]]; then
  echo "Manual service selection: ${REQUESTED_SERVICES}"
  select_requested_services
elif [[ "${EVENT_NAME}" == "workflow_dispatch" ]]; then
  echo "Manual workflow dispatch without service input: selecting all app services."
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

services="$(join_by_space "${SELECTED_KEYS[@]}")"
deployments="$(join_by_space "${SELECTED_DEPLOYMENTS[@]}")"
has_services="false"
smoke_frontend="false"
smoke_backend="false"

if [[ "${#SELECTED_KEYS[@]}" -gt 0 ]]; then
  has_services="true"
fi
if [[ -n "${SELECTED[frontend]:-}" ]]; then
  smoke_frontend="true"
fi
if [[ -n "${SELECTED[backend]:-}" ]]; then
  smoke_backend="true"
fi

write_output "has_services" "${has_services}"
write_output "services" "${services}"
write_output "deployments" "${deployments}"
write_output "smoke_frontend" "${smoke_frontend}"
write_output "smoke_backend" "${smoke_backend}"
