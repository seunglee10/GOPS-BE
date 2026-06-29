#!/usr/bin/env bash
# 역할: EKS가 쓰는 GOPS custom 이미지를 모두 ECR로 push합니다.
# 규칙: infra/docker/Dockerfile.gops-foo -> alfaka-dev-gops-foo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID를 넣어주세요}"
PROJECT_NAME="${PROJECT_NAME:-alfaka}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
NAME_PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

build_image() {
  local dockerfile="$1"
  local image="$2"

  if docker buildx version >/dev/null 2>&1; then
    docker buildx build --platform "${DOCKER_PLATFORM}" --load -f "${dockerfile}" -t "${image}:${IMAGE_TAG}" .
  else
    docker build --platform "${DOCKER_PLATFORM}" -f "${dockerfile}" -t "${image}:${IMAGE_TAG}" .
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

images=()
while IFS=$'\t' read -r _key repository env_var dockerfile; do
  image="$(resolve_image_url "${repository}" "${env_var}")"
  build_image "${dockerfile}" "${image}"
  images+=("${image}")
done < <(gops_image_entries)

for image in "${images[@]}"; do
  docker push "${image}:${IMAGE_TAG}"
done
