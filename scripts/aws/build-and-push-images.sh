#!/usr/bin/env bash
# 역할: EKS가 쓰는 GOPS custom 이미지를 모두 ECR로 push합니다.
# 사용: Terraform output의 각 ECR repository URL을 ECR_*_REPO 변수로 넣습니다.
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
ECR_FRONTEND_REPO="${ECR_FRONTEND_REPO:?ECR_FRONTEND_REPO를 넣어주세요}"
ECR_API_SERVER_REPO="${ECR_API_SERVER_REPO:?ECR_API_SERVER_REPO를 넣어주세요}"
ECR_MARKET_INGESTOR_REPO="${ECR_MARKET_INGESTOR_REPO:?ECR_MARKET_INGESTOR_REPO를 넣어주세요}"
ECR_MARKET_PROCESSOR_REPO="${ECR_MARKET_PROCESSOR_REPO:?ECR_MARKET_PROCESSOR_REPO를 넣어주세요}"
ECR_MARKET_STORAGE_REPO="${ECR_MARKET_STORAGE_REPO:?ECR_MARKET_STORAGE_REPO를 넣어주세요}"
ECR_BACKFILL_WORKER_REPO="${ECR_BACKFILL_WORKER_REPO:?ECR_BACKFILL_WORKER_REPO를 넣어주세요}"
ECR_ORDER_WORKER_REPO="${ECR_ORDER_WORKER_REPO:?ECR_ORDER_WORKER_REPO를 넣어주세요}"
ECR_KIS_ADAPTER_REPO="${ECR_KIS_ADAPTER_REPO:?ECR_KIS_ADAPTER_REPO를 넣어주세요}"
ECR_AGENT_ORCHESTRATOR_REPO="${ECR_AGENT_ORCHESTRATOR_REPO:?ECR_AGENT_ORCHESTRATOR_REPO를 넣어주세요}"

docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-frontend -t "${ECR_FRONTEND_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-backend -t "${ECR_API_SERVER_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-market-ingestor -t "${ECR_MARKET_INGESTOR_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-market-processor -t "${ECR_MARKET_PROCESSOR_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-market-storage -t "${ECR_MARKET_STORAGE_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-backfill-worker -t "${ECR_BACKFILL_WORKER_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-order-worker -t "${ECR_ORDER_WORKER_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-kis-adapter -t "${ECR_KIS_ADAPTER_REPO}:${IMAGE_TAG}" .
docker buildx build --platform "${DOCKER_PLATFORM}" --load -f infra/docker/Dockerfile.gops-agent-orchestrator -t "${ECR_AGENT_ORCHESTRATOR_REPO}:${IMAGE_TAG}" .

docker push "${ECR_FRONTEND_REPO}:${IMAGE_TAG}"
docker push "${ECR_API_SERVER_REPO}:${IMAGE_TAG}"
docker push "${ECR_MARKET_INGESTOR_REPO}:${IMAGE_TAG}"
docker push "${ECR_MARKET_PROCESSOR_REPO}:${IMAGE_TAG}"
docker push "${ECR_MARKET_STORAGE_REPO}:${IMAGE_TAG}"
docker push "${ECR_BACKFILL_WORKER_REPO}:${IMAGE_TAG}"
docker push "${ECR_ORDER_WORKER_REPO}:${IMAGE_TAG}"
docker push "${ECR_KIS_ADAPTER_REPO}:${IMAGE_TAG}"
docker push "${ECR_AGENT_ORCHESTRATOR_REPO}:${IMAGE_TAG}"
