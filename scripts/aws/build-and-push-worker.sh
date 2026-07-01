#!/usr/bin/env bash
# 역할: 호환용 Python worker 이미지를 빌드해서 ECR로 push합니다.
# 사용: Terraform 또는 ECR 출력의 worker repository URL을 ECR_WORKER_REPO에 넣습니다.
# 출력: systems/* worker 호환 이미지가 ECR에 올라갑니다.
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
ECR_WORKER_REPO="${ECR_WORKER_REPO:?ECR_WORKER_REPO를 넣어주세요}"

docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.worker -t "${ECR_WORKER_REPO}:${IMAGE_TAG}" .
docker push "${ECR_WORKER_REPO}:${IMAGE_TAG}"
