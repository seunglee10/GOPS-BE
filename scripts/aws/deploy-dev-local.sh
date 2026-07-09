#!/usr/bin/env bash
# 역할: 로컬 컴퓨터에서 origin/dev 기준으로 변경된 GOPS 서비스만 EKS dev에 배포합니다.
set -euo pipefail

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-<aws-account-id>}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-gops-eks-cluster}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
KUSTOMIZE_OVERLAY="${KUSTOMIZE_OVERLAY:-infra/k8s/overlays/aws-incluster-app-ci}"
PLATFORM_KUSTOMIZE_OVERLAY="${PLATFORM_KUSTOMIZE_OVERLAY:-infra/k8s/base/platform}"
DEPLOY_STATE_CONFIGMAP="${DEPLOY_STATE_CONFIGMAP:-gops-dev-deploy-state}"
REMOTE_NAME="${REMOTE_NAME:-origin}"
REMOTE_BRANCH="${REMOTE_BRANCH:-dev}"
FORCE_SERVICES="${FORCE_SERVICES:-}"
RUN_ORDER_MIGRATIONS="${RUN_ORDER_MIGRATIONS:-false}"
REBUILD_NEWS_CACHE="${REBUILD_NEWS_CACHE:-false}"
APPLY_PLATFORM_MANIFESTS="${APPLY_PLATFORM_MANIFESTS:-false}"
DRY_RUN="${DRY_RUN:-false}"
VITE_LOGO_DEV_ATTRIBUTION="${VITE_LOGO_DEV_ATTRIBUTION:-${LOGO_DEV_ATTRIBUTION:-true}}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREE_PARENT="${LOCAL_DEPLOY_WORKTREE_PARENT:-${TMPDIR:-/tmp}}"
WORKTREE_DIR=""
TARGET_SHA=""
SELECTED_SERVICES=""
SELECTED_DEPLOYMENTS=""
APP_APPLIED="false"
STATE_UPDATE_MODE="deployed"

usage() {
  cat <<'USAGE'
Usage:
  AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh

Optional environment variables:
  FORCE_SERVICES=all|frontend,backend   Override automatic diff detection.
  DRY_RUN=true                          Resolve target/diff and server-side dry-run only.
  RUN_ORDER_MIGRATIONS=true             Run order migrations; requires order-worker selected.
  REBUILD_NEWS_CACHE=true               Rebuild news Redis cache; requires market-storage selected.
  APPLY_PLATFORM_MANIFESTS=true         Apply dedicated platform manifests before app workloads.

The deploy target is always the latest origin/dev commit. Local uncommitted
changes and the current checkout branch are not included in the build.
USAGE
}

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

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "${command_name}" >&2
    exit 1
  fi
}

cleanup() {
  if [[ -n "${WORKTREE_DIR}" ]]; then
    git -C "${REPO_ROOT}" worktree remove --force "${WORKTREE_DIR}" >/dev/null 2>&1 \
      || rm -rf "${WORKTREE_DIR}"
  fi
}

rollback_on_error() {
  local exit_code="$1"
  local line_no="$2"
  set +e

  printf 'Local dev deploy failed near line %s (exit %s).\n' "${line_no}" "${exit_code}" >&2
  if [[ "${APP_APPLIED}" == "true" ]] && ! is_true "${DRY_RUN}" && [[ -n "${SELECTED_DEPLOYMENTS}" ]]; then
    printf 'Rolling back selected deployments: %s\n' "${SELECTED_DEPLOYMENTS}" >&2
    for deployment in ${SELECTED_DEPLOYMENTS}; do
      kubectl rollout undo "deployment/${deployment}" -n "${K8S_NAMESPACE}" || true
      kubectl rollout status "deployment/${deployment}" -n "${K8S_NAMESPACE}" --timeout=600s || true
    done
  fi
  exit "${exit_code}"
}

trap cleanup EXIT
trap 'rollback_on_error $? $LINENO' ERR

service_selected() {
  local service="$1"
  [[ " ${SELECTED_SERVICES} " == *" ${service} "* ]]
}

smoke_url() {
  local url="$1"
  local attempt

  for attempt in $(seq 1 20); do
    if curl -fsS "${url}" >/dev/null; then
      printf 'Smoke passed: %s\n' "${url}"
      return 0
    fi
    printf 'Smoke retry %s/20: %s\n' "${attempt}" "${url}"
    sleep 15
  done
  curl -i -fsS "${url}" >/dev/null
}

write_state_configmap() {
  local mode="$1"
  local actor
  local deployed_at

  actor="${DEPLOY_ACTOR:-$(git -C "${REPO_ROOT}" config user.email 2>/dev/null || whoami)}"
  deployed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  kubectl create configmap "${DEPLOY_STATE_CONFIGMAP}" \
    -n "${K8S_NAMESPACE}" \
    --from-literal="lastSuccessfulSha=${TARGET_SHA}" \
    --from-literal="lastSuccessfulAt=${deployed_at}" \
    --from-literal="lastSuccessfulActor=${actor}" \
    --from-literal="lastSuccessfulServices=${SELECTED_SERVICES}" \
    --from-literal="lastSuccessfulMode=${mode}" \
    --from-literal="targetRef=${REMOTE_NAME}/${REMOTE_BRANCH}" \
    --dry-run=client \
    -o yaml \
    | kubectl apply -f -
}

preflight() {
  require_command git
  require_command aws
  require_command kubectl
  require_command docker
  require_command curl
  require_command python3

  if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
    printf 'AWS_PROFILE or AWS_ACCESS_KEY_ID must be set for local deploy.\n' >&2
    exit 1
  fi

  if ! is_true "${DRY_RUN}"; then
    docker info >/dev/null
  fi
}

fetch_target() {
  cd "${REPO_ROOT}"
  printf 'Fetching %s/%s...\n' "${REMOTE_NAME}" "${REMOTE_BRANCH}"
  git fetch "${REMOTE_NAME}" "+${REMOTE_BRANCH}:refs/remotes/${REMOTE_NAME}/${REMOTE_BRANCH}"
  TARGET_SHA="$(git rev-parse "refs/remotes/${REMOTE_NAME}/${REMOTE_BRANCH}^{commit}")"
  printf 'Deploy target: %s/%s %s\n' "${REMOTE_NAME}" "${REMOTE_BRANCH}" "${TARGET_SHA}"
}

create_target_worktree() {
  mkdir -p "${WORKTREE_PARENT}"
  WORKTREE_DIR="$(mktemp -d "${WORKTREE_PARENT%/}/gops-dev-deploy.XXXXXX")"
  rmdir "${WORKTREE_DIR}"
  git -C "${REPO_ROOT}" worktree add --detach "${WORKTREE_DIR}" "${TARGET_SHA}" >/dev/null
  printf 'Using temporary worktree: %s\n' "${WORKTREE_DIR}"
}

configure_cluster() {
  local aws_account

  aws_account="$(aws sts get-caller-identity --query Account --output text)"
  if [[ "${aws_account}" != "${AWS_ACCOUNT_ID}" ]]; then
    printf 'AWS account mismatch: expected %s, got %s\n' "${AWS_ACCOUNT_ID}" "${aws_account}" >&2
    exit 1
  fi

  aws eks update-kubeconfig --name "${EKS_CLUSTER_NAME}" --region "${AWS_REGION}" >/dev/null
  kubectl get namespace "${K8S_NAMESPACE}" >/dev/null
}

read_last_successful_sha() {
  kubectl get configmap "${DEPLOY_STATE_CONFIGMAP}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.data.lastSuccessfulSha}' 2>/dev/null || true
}

detect_services() {
  local last_successful_sha="$1"
  local requested_services=""
  local detect_output
  local has_services
  local smoke_frontend
  local smoke_backend

  detect_output="$(mktemp)"

  if [[ -n "${FORCE_SERVICES}" ]]; then
    requested_services="${FORCE_SERVICES}"
    printf 'Forced service selection: %s\n' "${requested_services}"
  elif [[ -z "${last_successful_sha}" ]]; then
    requested_services="all"
    printf 'No %s ConfigMap state found; selecting all services for first local deploy.\n' "${DEPLOY_STATE_CONFIGMAP}"
  elif [[ "${last_successful_sha}" == "${TARGET_SHA}" ]]; then
    printf 'origin/dev is already recorded as deployed: %s\n' "${TARGET_SHA}"
    SELECTED_SERVICES=""
    SELECTED_DEPLOYMENTS=""
    rm -f "${detect_output}"
    return 0
  elif ! git -C "${WORKTREE_DIR}" rev-parse --verify "${last_successful_sha}^{commit}" >/dev/null 2>&1; then
    requested_services="all"
    printf 'Last successful SHA is not available locally; selecting all services: %s\n' "${last_successful_sha}"
  elif ! git -C "${WORKTREE_DIR}" merge-base --is-ancestor "${last_successful_sha}" "${TARGET_SHA}"; then
    requested_services="all"
    printf 'Last successful SHA is not an ancestor of origin/dev; selecting all services: %s\n' "${last_successful_sha}"
  fi

  if [[ -n "${requested_services}" ]]; then
    (
      cd "${WORKTREE_DIR}"
      REQUESTED_SERVICES="${requested_services}" \
        EVENT_NAME="local-dev-deploy" \
        HEAD_SHA="${TARGET_SHA}" \
        scripts/aws/detect-changed-services.sh
    ) | tee "${detect_output}"
  else
    (
      cd "${WORKTREE_DIR}"
      BASE_SHA="${last_successful_sha}" \
        HEAD_SHA="${TARGET_SHA}" \
        EVENT_NAME="local-dev-deploy" \
        scripts/aws/detect-changed-services.sh
    ) | tee "${detect_output}"
  fi

  has_services="$(sed -n 's/^has_services=//p' "${detect_output}" | tail -n 1)"
  SELECTED_SERVICES="$(sed -n 's/^services=//p' "${detect_output}" | tail -n 1)"
  SELECTED_DEPLOYMENTS="$(sed -n 's/^deployments=//p' "${detect_output}" | tail -n 1)"
  smoke_frontend="$(sed -n 's/^smoke_frontend=//p' "${detect_output}" | tail -n 1)"
  smoke_backend="$(sed -n 's/^smoke_backend=//p' "${detect_output}" | tail -n 1)"
  export LOCAL_DEPLOY_SMOKE_FRONTEND="${smoke_frontend:-false}"
  export LOCAL_DEPLOY_SMOKE_BACKEND="${smoke_backend:-false}"
  rm -f "${detect_output}"

  if [[ "${has_services}" != "true" ]]; then
    SELECTED_SERVICES=""
    SELECTED_DEPLOYMENTS=""
  fi
}

validate_optional_tasks() {
  if is_true "${RUN_ORDER_MIGRATIONS}" && ! service_selected "order-worker"; then
    printf 'RUN_ORDER_MIGRATIONS=true requires order-worker to be selected.\n' >&2
    exit 1
  fi
  if is_true "${REBUILD_NEWS_CACHE}" && ! service_selected "market-storage"; then
    printf 'REBUILD_NEWS_CACHE=true requires market-storage to be selected.\n' >&2
    exit 1
  fi
}

load_frontend_build_secret() {
  local env_file

  if ! service_selected "frontend"; then
    return 0
  fi
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping Logo.dev secret load.\n'
    return 0
  fi

  env_file="$(mktemp)"
  (
    cd "${WORKTREE_DIR}"
    GITHUB_ENV="${env_file}" scripts/aws/load-logodev-build-env.sh
  )
  while IFS= read -r env_line; do
    case "${env_line}" in
      LOGODEV_PUB_KEY=* | VITE_LOGO_DEV_PUBLISHABLE_KEY=*)
        export "${env_line}"
        ;;
    esac
  done < "${env_file}"
  rm -f "${env_file}"
}

login_to_ecr() {
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping ECR login.\n'
    return 0
  fi
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
}

apply_platform_if_requested() {
  if ! is_true "${APPLY_PLATFORM_MANIFESTS}"; then
    return 0
  fi

  (
    cd "${WORKTREE_DIR}"
    kubectl apply -k "${PLATFORM_KUSTOMIZE_OVERLAY}" --dry-run=server
    kubectl apply \
      -f infra/k8s/base/app/service-graphdb.yaml \
      -f infra/k8s/base/app/statefulset-graphdb.yaml \
      --dry-run=server
  )

  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping platform apply.\n'
    return 0
  fi

  (
    cd "${WORKTREE_DIR}"
    kubectl apply -k "${PLATFORM_KUSTOMIZE_OVERLAY}"
    kubectl apply \
      -f infra/k8s/base/app/service-graphdb.yaml \
      -f infra/k8s/base/app/statefulset-graphdb.yaml
  )
  for statefulset in clickhouse kafka postgres redis graphdb; do
    kubectl rollout status "statefulset/${statefulset}" -n "${K8S_NAMESPACE}" --timeout=900s
  done
}

prepare_kustomize_overlay() {
  (
    cd "${WORKTREE_DIR}"
    IMAGE_TAG="${IMAGE_TAG}" \
      SERVICES="${SELECTED_SERVICES}" \
      AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}" \
      AWS_REGION="${AWS_REGION}" \
      K8S_NAMESPACE="${K8S_NAMESPACE}" \
      KUSTOMIZE_OVERLAY="${KUSTOMIZE_OVERLAY}" \
      scripts/aws/update-ci-image-tags.sh

    SERVICES="${SELECTED_SERVICES}" \
      K8S_NAMESPACE="${K8S_NAMESPACE}" \
      KUSTOMIZE_OVERLAY="${KUSTOMIZE_OVERLAY}" \
      scripts/aws/enable-ci-api-workers.sh
  )
}

build_and_push_images() {
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping Docker build and ECR push for services: %s\n' "${SELECTED_SERVICES}"
    return 0
  fi

  (
    cd "${WORKTREE_DIR}"
    AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}" \
      AWS_REGION="${AWS_REGION}" \
      DOCKER_PLATFORM="${DOCKER_PLATFORM}" \
      IMAGE_TAG="${IMAGE_TAG}" \
      SERVICES="${SELECTED_SERVICES}" \
      VITE_LOGO_DEV_ATTRIBUTION="${VITE_LOGO_DEV_ATTRIBUTION}" \
      scripts/aws/build-and-push-images.sh
  )
}

run_migrations_if_requested() {
  if ! is_true "${RUN_ORDER_MIGRATIONS}"; then
    return 0
  fi
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping order migrations job.\n'
    return 0
  fi

  (
    cd "${WORKTREE_DIR}"
    # shellcheck source=scripts/aws/lib-gops-images.sh
    source scripts/aws/lib-gops-images.sh
    ECR_ORDER_WORKER_REPO="${ECR_ORDER_WORKER_REPO:-$(gops_image_url_for_key order-worker)}" \
      IMAGE_TAG="${IMAGE_TAG}" \
      K8S_NAMESPACE="${K8S_NAMESPACE}" \
      scripts/aws/run-order-migrations-job.sh
  )
}

deploy_app_workloads() {
  (
    cd "${WORKTREE_DIR}"
    kubectl apply -k "${KUSTOMIZE_OVERLAY}" --dry-run=server
  )

  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping app apply and rollout.\n'
    return 0
  fi

  kubectl delete deployment alfaka-alpaca-tick-ingestor-sip \
    -n "${K8S_NAMESPACE}" \
    --ignore-not-found=true

  (
    cd "${WORKTREE_DIR}"
    kubectl apply -k "${KUSTOMIZE_OVERLAY}"
  )
  APP_APPLIED="true"

  for deployment in ${SELECTED_DEPLOYMENTS}; do
    if ! kubectl rollout status "deployment/${deployment}" -n "${K8S_NAMESPACE}" --timeout=600s; then
      "${WORKTREE_DIR}/scripts/aws/print-rollout-diagnostics.sh" "${deployment}"
      exit 1
    fi
  done
}

run_smoke_tests() {
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping public smoke tests.\n'
    return 0
  fi
  if [[ "${LOCAL_DEPLOY_SMOKE_FRONTEND:-false}" == "true" ]]; then
    smoke_url https://stargops.com/
  fi
  if [[ "${LOCAL_DEPLOY_SMOKE_BACKEND:-false}" == "true" ]]; then
    smoke_url https://stargops.com/api/health
  fi
}

run_news_cache_rebuild_if_requested() {
  if ! is_true "${REBUILD_NEWS_CACHE}"; then
    return 0
  fi
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping news cache rebuild jobs.\n'
    return 0
  fi

  (
    cd "${WORKTREE_DIR}"
    AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}" \
      AWS_REGION="${AWS_REGION}" \
      IMAGE_TAG="${IMAGE_TAG}" \
      K8S_NAMESPACE="${K8S_NAMESPACE}" \
      scripts/aws/run-news-cache-rebuild-jobs.sh
  )
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi
  if [[ "$#" -gt 0 ]]; then
    printf 'This script takes no positional arguments. Use FORCE_SERVICES=... for emergency overrides.\n' >&2
    usage >&2
    exit 1
  fi

  preflight
  fetch_target
  create_target_worktree
  configure_cluster

  IMAGE_TAG="${IMAGE_TAG:-${TARGET_SHA:0:7}}"
  export AWS_ACCOUNT_ID AWS_REGION DOCKER_PLATFORM IMAGE_TAG K8S_NAMESPACE VITE_LOGO_DEV_ATTRIBUTION

  last_successful_sha="$(read_last_successful_sha)"
  if [[ -n "${last_successful_sha}" ]]; then
    printf 'Last successful local deploy SHA: %s\n' "${last_successful_sha}"
  fi

  detect_services "${last_successful_sha}"
  apply_platform_if_requested

  if [[ -z "${SELECTED_SERVICES}" ]]; then
    printf 'No app service changes detected for %s. Nothing to deploy.\n' "${TARGET_SHA}"
    STATE_UPDATE_MODE="skipped-no-service"
    if ! is_true "${DRY_RUN}"; then
      write_state_configmap "${STATE_UPDATE_MODE}"
    fi
    exit 0
  fi

  printf 'Selected services: %s\n' "${SELECTED_SERVICES}"
  printf 'Selected deployments: %s\n' "${SELECTED_DEPLOYMENTS}"

  validate_optional_tasks
  login_to_ecr
  load_frontend_build_secret

  if ! is_true "${DRY_RUN}"; then
    (
      cd "${WORKTREE_DIR}"
      AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}" \
        AWS_REGION="${AWS_REGION}" \
        scripts/aws/ensure-ecr-repositories.sh
    )
  else
    printf 'DRY_RUN=true: skipping ECR repository creation/check.\n'
  fi

  build_and_push_images
  prepare_kustomize_overlay

  (
    cd "${WORKTREE_DIR}"
    K8S_NAMESPACE="${K8S_NAMESPACE}" scripts/aws/validate-dedicated-platform.sh
  )

  run_migrations_if_requested
  deploy_app_workloads
  run_smoke_tests
  run_news_cache_rebuild_if_requested

  if ! is_true "${DRY_RUN}"; then
    APP_APPLIED="false"
    write_state_configmap "${STATE_UPDATE_MODE}"
  fi

  printf 'Local dev deploy completed for %s (%s): %s\n' "${REMOTE_NAME}/${REMOTE_BRANCH}" "${TARGET_SHA}" "${SELECTED_SERVICES}"
}

main "$@"
