#!/usr/bin/env bash
# Shared helpers for GOPS Docker images and ECR repository names.

gops_image_repository_for_key() {
  local key="$1"

  case "${key}" in
    backend)
      echo "gops-api-server"
      ;;
    *)
      echo "gops-${key}"
      ;;
  esac
}

gops_image_env_var_for_repository() {
  local repository="$1"
  local suffix="${repository#gops-}"
  suffix="$(printf '%s' "${suffix}" | tr '[:lower:]-' '[:upper:]_')"

  echo "ECR_${suffix}_REPO"
}

gops_normalize_service_key() {
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
    *)
      echo "${service}"
      ;;
  esac
}

gops_service_exists() {
  local expected_key="$1"
  local key

  while IFS=$'\t' read -r key _repository _env_var _dockerfile; do
    if [[ "${key}" == "${expected_key}" ]]; then
      return 0
    fi
  done < <(gops_image_entries)

  return 1
}

gops_image_url_for_key() {
  local expected_key="$1"
  local aws_account_id="${AWS_ACCOUNT_ID:-}"
  local aws_region="${AWS_REGION:-ap-northeast-2}"
  local project_name="${PROJECT_NAME:-alfaka}"
  local environment="${ENVIRONMENT:-dev}"
  local name_prefix="${project_name}-${environment}"
  local ecr_registry
  local key
  local repository
  local env_var

  if [[ -z "${aws_account_id}" ]]; then
    printf 'AWS_ACCOUNT_ID를 넣어주세요.\n' >&2
    return 1
  fi

  ecr_registry="${aws_account_id}.dkr.ecr.${aws_region}.amazonaws.com"
  while IFS=$'\t' read -r key repository env_var _dockerfile; do
    if [[ "${key}" != "${expected_key}" ]]; then
      continue
    fi

    if [[ -n "${!env_var:-}" ]]; then
      echo "${!env_var}"
    else
      echo "${ecr_registry}/${name_prefix}-${repository}"
    fi
    return 0
  done < <(gops_image_entries)

  printf 'Unknown service: %s\n' "${expected_key}" >&2
  return 1
}

gops_deployments_for_service() {
  local key="$1"

  case "${key}" in
    agent-orchestrator)
      printf '%s\n' agent-event-detector agent-notification-publisher agent-orchestrator
      ;;
    backend)
      printf '%s\n' gops-backend
      ;;
    frontend)
      printf '%s\n' gops-frontend
      ;;
    kis-adapter)
      printf '%s\n' kis-broker-adapter
      ;;
    market-ingestor)
      printf '%s\n' alfaka-alpaca-ingestor-sip alfaka-alpaca-ingestor-boats alfaka-alpaca-news-ingestor
      ;;
    market-processor)
      printf '%s\n' alfaka-market-processor alfaka-feed-session-controller alfaka-subscription-controller
      ;;
    market-storage)
      printf '%s\n' alfaka-clickhouse-loader alfaka-s3-sink alfaka-raw-s3-archive alfaka-news-intelligence-worker alfaka-news-daily-summary-worker
      ;;
    order-worker)
      printf '%s\n' order-outbox-publisher
      ;;
    *)
      printf 'Unknown service: %s\n' "${key}" >&2
      return 1
      ;;
  esac
}

gops_primary_deployment_for_service() {
  local key="$1"

  case "${key}" in
    agent-orchestrator)
      echo "agent-orchestrator"
      ;;
    backend)
      echo "gops-backend"
      ;;
    frontend)
      echo "gops-frontend"
      ;;
    kis-adapter)
      echo "kis-broker-adapter"
      ;;
    market-ingestor)
      echo "alfaka-alpaca-ingestor-sip"
      ;;
    market-processor)
      echo "alfaka-market-processor"
      ;;
    market-storage)
      echo "alfaka-clickhouse-loader"
      ;;
    order-worker)
      echo "order-outbox-publisher"
      ;;
    *)
      printf 'Unknown service: %s\n' "${key}" >&2
      return 1
      ;;
  esac
}

gops_image_entries() {
  local dockerfile
  local key
  local repository
  local env_var

  while IFS= read -r dockerfile; do
    key="${dockerfile##*/Dockerfile.gops-}"
    repository="$(gops_image_repository_for_key "${key}")"
    env_var="$(gops_image_env_var_for_repository "${repository}")"

    printf '%s\t%s\t%s\t%s\n' "${key}" "${repository}" "${env_var}" "${dockerfile}"
  done < <(find infra/docker -maxdepth 1 -type f -name 'Dockerfile.gops-*' | sort)
}
