#!/usr/bin/env bash
# 역할: EKS가 쓰는 GOPS custom 이미지를 ECR로 push합니다.
# 규칙: infra/docker/Dockerfile.gops-foo -> alfaka-dev-gops-foo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-}"
PROJECT_NAME="${PROJECT_NAME:-alfaka}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
NAME_PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"
ECR_REGISTRY=""

usage() {
  printf 'Usage: %s [service ...]\n\n' "${0##*/}"
  printf 'No service arguments: build and push all GOPS images.\n'
  printf 'With service arguments: build and push only those images.\n'
  printf 'You can also set SERVICES="frontend,backend" or SERVICES="frontend backend".\n\n'
  printf 'Available services:\n'
  while IFS=$'\t' read -r key repository _env_var _dockerfile; do
    printf '  %-22s -> %s\n' "${key}" "${repository}"
  done < <(gops_image_entries)
  printf '\nExamples:\n'
  printf '  AWS_ACCOUNT_ID=<aws-account-id> %s frontend\n' "${0##*/}"
  printf '  AWS_ACCOUNT_ID=<aws-account-id> SERVICES=frontend,backend %s\n' "${0##*/}"
}

normalize_service_key() {
  local service="$1"

  case "${service}" in
    gops-frontend)
      echo "frontend"
      ;;
    api-server | backend | gops-api-server | gops-backend)
      echo "backend"
      ;;
    ingestor | market-ingestor | gops-market-ingestor)
      echo "market-ingestor"
      ;;
    processor | market-processor | gops-market-processor)
      echo "market-processor"
      ;;
    storage | market-storage | gops-market-storage)
      echo "market-storage"
      ;;
    order | order-worker | gops-order-worker)
      echo "order-worker"
      ;;
    kis | kis-adapter | gops-kis-adapter)
      echo "kis-adapter"
      ;;
    agent | agent-orchestrator | gops-agent-orchestrator)
      echo "agent-orchestrator"
      ;;
    simulator | gops-simulator)
      echo "simulator"
      ;;
    *)
      echo "${service}"
      ;;
  esac
}

service_exists() {
  local expected_key="$1"
  local key

  while IFS=$'\t' read -r key _repository _env_var _dockerfile; do
    if [[ "${key}" == "${expected_key}" ]]; then
      return 0
    fi
  done < <(gops_image_entries)

  return 1
}

service_selected() {
  local key="$1"
  local selected_key

  if [[ "${#SELECTED_KEYS[@]}" -eq 0 ]]; then
    return 0
  fi

  for selected_key in "${SELECTED_KEYS[@]}"; do
    if [[ "${key}" == "${selected_key}" ]]; then
      return 0
    fi
  done

  return 1
}

build_image() {
  local dockerfile="$1"
  local image="$2"
  local build_args=()

  if [[ "${dockerfile}" == "infra/docker/Dockerfile.gops-frontend" ]]; then
    local logo_dev_publishable_key="${LOGODEV_PUB_KEY:-${VITE_LOGO_DEV_PUBLISHABLE_KEY:-}}"
    build_args+=(--build-arg "VITE_LOGO_DEV_ATTRIBUTION=${VITE_LOGO_DEV_ATTRIBUTION:-true}")
    if [[ -n "${logo_dev_publishable_key}" ]]; then
      build_args+=(--build-arg "LOGODEV_PUB_KEY=${logo_dev_publishable_key}")
      build_args+=(--build-arg "VITE_LOGO_DEV_PUBLISHABLE_KEY=${logo_dev_publishable_key}")
    fi
  fi

  if docker buildx version >/dev/null 2>&1; then
    docker buildx build --platform "${DOCKER_PLATFORM}" --load "${build_args[@]}" -f "${dockerfile}" -t "${image}:${IMAGE_TAG}" .
  else
    docker build --platform "${DOCKER_PLATFORM}" "${build_args[@]}" -f "${dockerfile}" -t "${image}:${IMAGE_TAG}" .
  fi
}

resolve_image_url() {
  local repository="$1"
  local env_var="$2"
  local image="${ECR_REGISTRY}/${NAME_PREFIX}-${repository}"

  if [[ -n "${!env_var:-}" ]]; then
    image="${!env_var}"
  fi

  echo "${image}"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

REQUESTED_SERVICES=("$@")
if [[ "${#REQUESTED_SERVICES[@]}" -eq 0 && -n "${SERVICES:-}" ]]; then
  read -r -a REQUESTED_SERVICES <<< "${SERVICES//,/ }"
fi

SELECTED_KEYS=()
for requested_service in "${REQUESTED_SERVICES[@]}"; do
  if [[ -z "${requested_service}" ]]; then
    continue
  fi

  selected_key="$(normalize_service_key "${requested_service}")"
  if ! service_exists "${selected_key}"; then
    printf 'Unknown service: %s\n\n' "${requested_service}" >&2
    usage >&2
    exit 1
  fi

  SELECTED_KEYS+=("${selected_key}")
done

printf 'Image tag: %s\n' "${IMAGE_TAG}"
printf 'Docker platform: %s\n' "${DOCKER_PLATFORM}"
if [[ "${#SELECTED_KEYS[@]}" -eq 0 ]]; then
  printf 'Selected services: all\n'
else
  printf 'Selected services: %s\n' "${SELECTED_KEYS[*]}"
fi

if [[ -z "${AWS_ACCOUNT_ID}" ]]; then
  printf 'AWS_ACCOUNT_ID를 넣어주세요.\n' >&2
  exit 1
fi

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

images=()
while IFS=$'\t' read -r _key repository env_var dockerfile; do
  if ! service_selected "${_key}"; then
    continue
  fi

  image="$(resolve_image_url "${repository}" "${env_var}")"
  build_image "${dockerfile}" "${image}"
  images+=("${image}")
done < <(gops_image_entries)

for image in "${images[@]}"; do
  docker push "${image}:${IMAGE_TAG}"
done
