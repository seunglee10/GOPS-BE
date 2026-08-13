#!/usr/bin/env bash
# 역할: 기존 Geometry 자산을 읽는 차트 해설 consumer만 새 이미지로 교체합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
DRY_RUN="${DRY_RUN:-false}"

is_true() {
  case "${1:-false}" in
    true | TRUE | 1 | yes | YES)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

agent_image="$(gops_image_url_for_key agent-orchestrator):${IMAGE_TAG}"

set_deployment_image() {
  local deployment="$1"
  local container="$2"
  local image="$3"

  if is_true "${DRY_RUN}"; then
    kubectl set image "deployment/${deployment}" "${container}=${image}" \
      -n "${K8S_NAMESPACE}" --dry-run=server -o name
    return 0
  fi

  kubectl set image "deployment/${deployment}" "${container}=${image}" \
    -n "${K8S_NAMESPACE}"
}

# 의도적으로 이 두 consumer만 갱신한다. chart-asset-builder, Geometry CronJob,
# migration/maintenance Job 및 다른 agent runtime은 이 경로의 대상이 아니다.
# gops-frontend Deployment는 gops-frontend 저장소의 CI가 롤아웃한다.
set_deployment_image agent-analysis-worker agent-analysis-worker "${agent_image}"
set_deployment_image agent-orchestrator agent-orchestrator "${agent_image}"
