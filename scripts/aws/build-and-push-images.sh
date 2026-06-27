#!/usr/bin/env bash
# 역할: EKS가 쓰는 worker, backend, frontend 이미지를 모두 ECR로 push합니다.
# 사용: ECR_WORKER_REPO, ECR_BACKEND_REPO, ECR_FRONTEND_REPO를 Terraform output 값으로 넣습니다.
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
ECR_WORKER_REPO="${ECR_WORKER_REPO:?ECR_WORKER_REPO를 넣어주세요}"
ECR_BACKEND_REPO="${ECR_BACKEND_REPO:?ECR_BACKEND_REPO를 넣어주세요}"
ECR_FRONTEND_REPO="${ECR_FRONTEND_REPO:?ECR_FRONTEND_REPO를 넣어주세요}"

docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.worker -t "${ECR_WORKER_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-backend -t "${ECR_BACKEND_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-frontend -t "${ECR_FRONTEND_REPO}:${IMAGE_TAG}" .

docker push "${ECR_WORKER_REPO}:${IMAGE_TAG}"
docker push "${ECR_BACKEND_REPO}:${IMAGE_TAG}"
docker push "${ECR_FRONTEND_REPO}:${IMAGE_TAG}"
