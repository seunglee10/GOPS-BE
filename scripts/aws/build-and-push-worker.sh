#!/usr/bin/env bash
# 역할: Python worker 이미지를 빌드해서 ECR로 push합니다.
# 사용: Terraform output worker_ecr_repository_url 값을 ECR_WORKER_REPO에 넣습니다.
# 출력: services/* worker가 EKS에서 쓸 이미지가 ECR에 올라갑니다.
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-latest}"
ECR_WORKER_REPO="${ECR_WORKER_REPO:?ECR_WORKER_REPO를 넣어주세요}"

docker build -f infra/docker/Dockerfile.worker -t "${ECR_WORKER_REPO}:${IMAGE_TAG}" .
docker push "${ECR_WORKER_REPO}:${IMAGE_TAG}"
