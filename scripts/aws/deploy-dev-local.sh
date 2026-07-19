#!/usr/bin/env bash
# 역할: 원격 dev 또는 명시한 로컬 commit 기준으로 변경된 GOPS 서비스만 EKS dev에 배포합니다.
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
LOCAL_REF="${LOCAL_REF:-}"
FORCE_SERVICES="${FORCE_SERVICES:-}"
RUN_ORDER_MIGRATIONS="${RUN_ORDER_MIGRATIONS:-false}"
RUN_CHART_ASSET_MIGRATIONS="${RUN_CHART_ASSET_MIGRATIONS:-false}"
REBUILD_NEWS_CACHE="${REBUILD_NEWS_CACHE:-false}"
APPLY_PLATFORM_MANIFESTS="${APPLY_PLATFORM_MANIFESTS:-false}"
DRY_RUN="${DRY_RUN:-false}"
CHART_INTERPRETATION_ONLY="${CHART_INTERPRETATION_ONLY:-false}"
VITE_LOGO_DEV_ATTRIBUTION="${VITE_LOGO_DEV_ATTRIBUTION:-${LOGO_DEV_ATTRIBUTION:-true}}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREE_PARENT="${LOCAL_DEPLOY_WORKTREE_PARENT:-${TMPDIR:-/tmp}}"
WORKTREE_DIR=""
DEPLOY_STATE_JSON_FILE=""
TARGET_SHA=""
DEPLOY_TARGET_REF=""
SELECTED_SERVICES=""
SELECTED_DEPLOYMENTS=""
APP_APPLIED="false"
STATE_UPDATE_MODE="deployed"

usage() {
  cat <<'USAGE'
Usage:
  AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh

Optional environment variables:
  REMOTE_BRANCH=branch-name              Deploy latest origin/<branch>; defaults to dev.
  LOCAL_REF=dev                          Deploy a committed local ref without pushing it.
  FORCE_SERVICES=all|frontend,backend   Override automatic diff detection.
  DRY_RUN=true                          Resolve target/diff and server-side dry-run only.
  RUN_ORDER_MIGRATIONS=true             Legacy force switch; requires order-worker selected.
  RUN_CHART_ASSET_MIGRATIONS=true       Legacy force switch; chart migrations run automatically with agent-orchestrator.
  REBUILD_NEWS_CACHE=true               Rebuild news Redis cache; requires market-storage selected.
  APPLY_PLATFORM_MANIFESTS=true         Apply dedicated platform manifests before app workloads.
  CHART_INTERPRETATION_ONLY=true        Roll out only frontend and chart-analysis consumers.

The deploy target is the latest origin/<REMOTE_BRANCH> commit unless LOCAL_REF
is set. Local uncommitted changes are never included in the build.
Order migrations run automatically before rollout whenever order-worker is
selected. Chart migrations run automatically whenever agent-orchestrator is
selected. Selecting agent-orchestrator also selects both migration images.
CHART_INTERPRETATION_ONLY requires FORCE_SERVICES=frontend,agent-orchestrator.
It never applies Kustomize, migrations, the chart asset builder, or Geometry CronJobs.
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
  if [[ -n "${DEPLOY_STATE_JSON_FILE}" ]]; then
    rm -f "${DEPLOY_STATE_JSON_FILE}"
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
  local patch_payload

  actor="${DEPLOY_ACTOR:-$(git -C "${REPO_ROOT}" config user.email 2>/dev/null || whoami)}"
  deployed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if ! kubectl get configmap "${DEPLOY_STATE_CONFIGMAP}" -n "${K8S_NAMESPACE}" >/dev/null 2>&1; then
    kubectl create configmap "${DEPLOY_STATE_CONFIGMAP}" -n "${K8S_NAMESPACE}" >/dev/null
  fi

  patch_payload="$(
    python3 - "${TARGET_SHA}" "${IMAGE_TAG}" "${deployed_at}" "${actor}" "${SELECTED_SERVICES}" "${mode}" "${DEPLOY_TARGET_REF}" <<'PY'
import json
import sys

target_sha, image_tag, deployed_at, actor, services_raw, mode, target_ref = sys.argv[1:]
services = [service for service in services_raw.split() if service]

data = {"targetRef": target_ref}
if services:
    data.update({
        "lastSuccessfulSha": target_sha,
        "lastSuccessfulImageTag": image_tag,
        "lastSuccessfulAt": deployed_at,
        "lastSuccessfulActor": actor,
        "lastSuccessfulServices": " ".join(services),
        "lastSuccessfulMode": mode,
    })
    for service in services:
        prefix = f"service.{service}."
        data[f"{prefix}lastSuccessfulSha"] = target_sha
        data[f"{prefix}lastSuccessfulImageTag"] = image_tag
        data[f"{prefix}lastSuccessfulAt"] = deployed_at
        data[f"{prefix}lastSuccessfulActor"] = actor
        data[f"{prefix}lastSuccessfulMode"] = mode
else:
    data.update({
        "lastCheckedSha": target_sha,
        "lastCheckedAt": deployed_at,
        "lastCheckedActor": actor,
        "lastCheckedMode": mode,
    })

print(json.dumps({"data": data}, separators=(",", ":")))
PY
  )"

  kubectl patch configmap "${DEPLOY_STATE_CONFIGMAP}" \
    -n "${K8S_NAMESPACE}" \
    --type merge \
    -p "${patch_payload}" >/dev/null
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
  if [[ -n "${LOCAL_REF}" ]]; then
    TARGET_SHA="$(git rev-parse --verify "${LOCAL_REF}^{commit}")"
    DEPLOY_TARGET_REF="local:${LOCAL_REF}"
    printf 'Deploy target: %s %s\n' "${DEPLOY_TARGET_REF}" "${TARGET_SHA}"
    return 0
  fi
  printf 'Fetching %s/%s...\n' "${REMOTE_NAME}" "${REMOTE_BRANCH}"
  git fetch "${REMOTE_NAME}" "+${REMOTE_BRANCH}:refs/remotes/${REMOTE_NAME}/${REMOTE_BRANCH}"
  TARGET_SHA="$(git rev-parse "refs/remotes/${REMOTE_NAME}/${REMOTE_BRANCH}^{commit}")"
  DEPLOY_TARGET_REF="${REMOTE_NAME}/${REMOTE_BRANCH}"
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

read_deploy_state() {
  DEPLOY_STATE_JSON_FILE="$(mktemp)"
  if ! kubectl get configmap "${DEPLOY_STATE_CONFIGMAP}" -n "${K8S_NAMESPACE}" -o json > "${DEPLOY_STATE_JSON_FILE}" 2>/dev/null; then
    printf '{"data":{}}\n' > "${DEPLOY_STATE_JSON_FILE}"
  fi
}

deploy_state_value() {
  local key="$1"

  python3 - "${DEPLOY_STATE_JSON_FILE}" "${key}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as state_file:
    payload = json.load(state_file)
print((payload.get("data") or {}).get(sys.argv[2], ""))
PY
}

list_all_services() {
  (
    cd "${WORKTREE_DIR}"
    # shellcheck source=scripts/aws/lib-gops-images.sh
    source scripts/aws/lib-gops-images.sh
    while IFS=$'\t' read -r key _repository _env_var _dockerfile; do
      printf '%s\n' "${key}"
    done < <(gops_image_entries)
  )
}

normalize_service_key() {
  local service="$1"
  (
    cd "${WORKTREE_DIR}"
    # shellcheck source=scripts/aws/lib-gops-images.sh
    source scripts/aws/lib-gops-images.sh
    gops_normalize_service_key "${service}"
  )
}

primary_deployment_for_service() {
  local service="$1"
  (
    cd "${WORKTREE_DIR}"
    # shellcheck source=scripts/aws/lib-gops-images.sh
    source scripts/aws/lib-gops-images.sh
    gops_primary_deployment_for_service "${service}"
  )
}

service_list_contains() {
  local expected_service="$1"
  local raw_services="$2"
  local service
  local normalized
  local services=()

  read -r -a services <<< "${raw_services//,/ }"
  for service in "${services[@]}"; do
    if [[ -z "${service}" ]]; then
      continue
    fi
    if [[ "${service}" == "all" || "${service}" == "*" ]]; then
      return 0
    fi
    normalized="$(normalize_service_key "${service}")"
    if [[ "${normalized}" == "${expected_service}" ]]; then
      return 0
    fi
  done
  return 1
}

live_deployment_sha_for_service() {
  local service="$1"
  local deployment
  local image
  local tag

  deployment="$(primary_deployment_for_service "${service}" 2>/dev/null || true)"
  if [[ -z "${deployment}" ]]; then
    return 0
  fi

  image="$(kubectl get deployment "${deployment}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  if [[ -z "${image}" || "${image}" != *:* ]]; then
    return 0
  fi

  tag="${image##*:}"
  git -C "${WORKTREE_DIR}" rev-parse --verify "${tag}^{commit}" 2>/dev/null || true
}

baseline_sha_for_service() {
  local service="$1"
  local service_sha
  local legacy_sha
  local legacy_services
  local live_sha

  service_sha="$(deploy_state_value "service.${service}.lastSuccessfulSha")"
  if [[ -n "${service_sha}" ]]; then
    printf '%s\n' "${service_sha}"
    return 0
  fi

  legacy_sha="$(deploy_state_value "lastSuccessfulSha")"
  legacy_services="$(deploy_state_value "lastSuccessfulServices")"
  if [[ -n "${legacy_sha}" ]] && service_list_contains "${service}" "${legacy_services}"; then
    printf '%s\n' "${legacy_sha}"
    return 0
  fi

  live_sha="$(live_deployment_sha_for_service "${service}")"
  if [[ -n "${live_sha}" ]]; then
    printf '%s\n' "${live_sha}"
  fi
}

changed_services_between() {
  local base_sha="$1"
  local detect_output
  local services

  detect_output="$(mktemp)"
  (
    cd "${WORKTREE_DIR}"
    BASE_SHA="${base_sha}" \
      HEAD_SHA="${TARGET_SHA}" \
      EVENT_NAME="local-dev-deploy" \
      scripts/aws/detect-changed-services.sh
  ) > "${detect_output}"
  services="$(sed -n 's/^services=//p' "${detect_output}" | tail -n 1)"
  rm -f "${detect_output}"
  printf '%s\n' "${services}"
}

service_needs_deploy() {
  local service="$1"
  local base_sha="$2"
  local changed_services

  if [[ -z "${base_sha}" ]]; then
    printf 'Service %s has no deploy baseline; selecting it.\n' "${service}"
    return 0
  fi
  if [[ "${base_sha}" == "${TARGET_SHA}" ]]; then
    printf 'Service %s already at target SHA %s.\n' "${service}" "${TARGET_SHA}"
    return 1
  fi
  if ! git -C "${WORKTREE_DIR}" rev-parse --verify "${base_sha}^{commit}" >/dev/null 2>&1; then
    printf 'Service %s baseline SHA is not available locally; selecting it: %s\n' "${service}" "${base_sha}"
    return 0
  fi
  if ! git -C "${WORKTREE_DIR}" merge-base --is-ancestor "${base_sha}" "${TARGET_SHA}"; then
    printf 'Service %s baseline is not an ancestor of the deploy target; selecting it: %s\n' "${service}" "${base_sha}"
    return 0
  fi

  changed_services="$(changed_services_between "${base_sha}")"
  if service_list_contains "${service}" "${changed_services}"; then
    printf 'Service %s changed since deployed baseline %s.\n' "${service}" "${base_sha}"
    return 0
  fi

  printf 'Service %s has no service-owned changes since %s.\n' "${service}" "${base_sha}"
  return 1
}

resolve_selected_services() {
  local requested_services="$1"
  local detect_output
  local has_services
  local smoke_frontend
  local smoke_backend

  if [[ -z "${requested_services}" ]]; then
    SELECTED_SERVICES=""
    SELECTED_DEPLOYMENTS=""
    export LOCAL_DEPLOY_SMOKE_FRONTEND="false"
    export LOCAL_DEPLOY_SMOKE_BACKEND="false"
    return 0
  fi

  detect_output="$(mktemp)"
  (
    cd "${WORKTREE_DIR}"
    REQUESTED_SERVICES="${requested_services}" \
      EVENT_NAME="local-dev-deploy" \
      HEAD_SHA="${TARGET_SHA}" \
      scripts/aws/detect-changed-services.sh
  ) | tee "${detect_output}"

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

detect_services() {
  local requested_services=""
  local service
  local base_sha
  local selected_services=()

  if [[ -n "${FORCE_SERVICES}" ]]; then
    requested_services="${FORCE_SERVICES}"
    printf 'Forced service selection: %s\n' "${requested_services}"
    resolve_selected_services "${requested_services}"
    return 0
  fi

  while IFS= read -r service; do
    base_sha="$(baseline_sha_for_service "${service}")"
    if service_needs_deploy "${service}" "${base_sha}"; then
      selected_services+=("${service}")
    fi
  done < <(list_all_services)

  requested_services="${selected_services[*]}"
  resolve_selected_services "${requested_services}"
}

apply_chart_interpretation_scope() {
  if ! is_true "${CHART_INTERPRETATION_ONLY}"; then
    return 0
  fi

  if [[ "${FORCE_SERVICES// /}" != "frontend,agent-orchestrator" && "${FORCE_SERVICES// /}" != "agent-orchestrator,frontend" ]]; then
    printf 'CHART_INTERPRETATION_ONLY=true requires FORCE_SERVICES=frontend,agent-orchestrator.\n' >&2
    exit 1
  fi
  if is_true "${RUN_ORDER_MIGRATIONS}" \
    || is_true "${RUN_CHART_ASSET_MIGRATIONS}" \
    || is_true "${REBUILD_NEWS_CACHE}" \
    || is_true "${APPLY_PLATFORM_MANIFESTS}"; then
    printf 'CHART_INTERPRETATION_ONLY=true cannot run migrations, rebuilds, or platform apply.\n' >&2
    exit 1
  fi

  # 일반 agent service의 order migration 및 공유-image workload 결합을 제거한다.
  SELECTED_SERVICES="frontend agent-orchestrator"
  SELECTED_DEPLOYMENTS="gops-frontend agent-analysis-worker agent-orchestrator"
  export LOCAL_DEPLOY_SMOKE_FRONTEND="true"
  export LOCAL_DEPLOY_SMOKE_BACKEND="false"
  printf 'Reader-only chart deploy selected: chart-asset-builder is not updated; do not regenerate assets until a normal agent-orchestrator rollout completes.\n'
}

validate_optional_tasks() {
  if is_true "${RUN_ORDER_MIGRATIONS}" && ! service_selected "order-worker"; then
    printf 'RUN_ORDER_MIGRATIONS=true requires order-worker to be selected.\n' >&2
    exit 1
  fi
  if is_true "${RUN_CHART_ASSET_MIGRATIONS}" && ! service_selected "agent-orchestrator"; then
    printf 'RUN_CHART_ASSET_MIGRATIONS=true requires agent-orchestrator to be selected.\n' >&2
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
  if service_selected "market-processor"; then
    if is_true "${DRY_RUN}"; then
      printf 'DRY_RUN=true: company journal benchmark candle bootstrap selected; skipping live Job.\n'
    else
      (
        cd "${WORKTREE_DIR}"
        # shellcheck source=scripts/aws/lib-gops-images.sh
        source scripts/aws/lib-gops-images.sh
        ECR_MARKET_PROCESSOR_REPO="${ECR_MARKET_PROCESSOR_REPO:-$(gops_image_url_for_key market-processor)}" \
          IMAGE_TAG="${IMAGE_TAG}" \
          K8S_NAMESPACE="${K8S_NAMESPACE}" \
          scripts/aws/run-company-journal-benchmark-bootstrap-job.sh
      )
    fi
  fi

  if service_selected "backend"; then
    if is_true "${DRY_RUN}"; then
      printf 'DRY_RUN=true: automatic company journal ClickHouse migration gate selected; skipping live Job.\n'
    else
      (
        cd "${WORKTREE_DIR}"
        # shellcheck source=scripts/aws/lib-gops-images.sh
        source scripts/aws/lib-gops-images.sh
        ECR_API_SERVER_REPO="${ECR_API_SERVER_REPO:-$(gops_image_url_for_key backend)}" \
          IMAGE_TAG="${IMAGE_TAG}" \
          K8S_NAMESPACE="${K8S_NAMESPACE}" \
          scripts/aws/run-company-journal-migrations-job.sh
      )
    fi
  fi

  if service_selected "order-worker"; then
    if is_true "${DRY_RUN}"; then
      printf 'DRY_RUN=true: automatic order migration gate selected; skipping live Job.\n'
    else
      (
        cd "${WORKTREE_DIR}"
        # shellcheck source=scripts/aws/lib-gops-images.sh
        source scripts/aws/lib-gops-images.sh
        ECR_ORDER_WORKER_REPO="${ECR_ORDER_WORKER_REPO:-$(gops_image_url_for_key order-worker)}" \
          IMAGE_TAG="${IMAGE_TAG}" \
          K8S_NAMESPACE="${K8S_NAMESPACE}" \
          scripts/aws/run-order-migrations-job.sh
      )
    fi
  fi

  if service_selected "agent-orchestrator" && ! is_true "${CHART_INTERPRETATION_ONLY}"; then
    if is_true "${DRY_RUN}"; then
      printf 'DRY_RUN=true: automatic chart asset migration gate selected; skipping live Job.\n'
    else
      (
        cd "${WORKTREE_DIR}"
        # shellcheck source=scripts/aws/lib-gops-images.sh
        source scripts/aws/lib-gops-images.sh
        ECR_AGENT_ORCHESTRATOR_REPO="${ECR_AGENT_ORCHESTRATOR_REPO:-$(gops_image_url_for_key agent-orchestrator)}" \
          IMAGE_TAG="${IMAGE_TAG}" \
          K8S_NAMESPACE="${K8S_NAMESPACE}" \
          scripts/aws/run-chart-asset-migrations-job.sh
      )
    fi
  fi
}

run_simulator_replay_import_if_selected() {
  local simulator_image

  if ! service_selected "simulator"; then
    return 0
  fi
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping simulator replay dataset import.\n'
    return 0
  fi

  simulator_image="$(
    cd "${WORKTREE_DIR}"
    # shellcheck source=scripts/aws/lib-gops-images.sh
    source scripts/aws/lib-gops-images.sh
    gops_image_url_for_key simulator
  )"
  (
    cd "${WORKTREE_DIR}"
    SIMULATOR_IMAGE="${simulator_image}:${IMAGE_TAG}" \
      SIM_REPLAY_RESUME_FROM_S3="${SIM_REPLAY_RESUME_FROM_S3:-false}" \
      K8S_NAMESPACE="${K8S_NAMESPACE}" \
      scripts/aws/run-simulator-replay-import.sh
  )
}

verify_ai_coach_snapshot_archive() {
  if ! service_selected "agent-orchestrator"; then
    return 0
  fi
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping AI coach snapshot S3 write gate.\n'
    return 0
  fi

  (
    cd "${WORKTREE_DIR}"
    K8S_NAMESPACE="${K8S_NAMESPACE}" scripts/aws/verify-ai-coach-snapshot-s3.sh
  )
}

deploy_app_workloads() {
  if is_true "${CHART_INTERPRETATION_ONLY}"; then
    (
      cd "${WORKTREE_DIR}"
      AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}" \
        AWS_REGION="${AWS_REGION}" \
        IMAGE_TAG="${IMAGE_TAG}" \
        K8S_NAMESPACE="${K8S_NAMESPACE}" \
        DRY_RUN="${DRY_RUN}" \
        scripts/aws/deploy-chart-interpretation-images.sh
    )

    if is_true "${DRY_RUN}"; then
      printf 'DRY_RUN=true: skipping chart interpretation rollout status.\n'
      return 0
    fi

    APP_APPLIED="true"
    for deployment in ${SELECTED_DEPLOYMENTS}; do
      if ! kubectl rollout status "deployment/${deployment}" -n "${K8S_NAMESPACE}" --timeout=600s; then
        "${WORKTREE_DIR}/scripts/aws/print-rollout-diagnostics.sh" "${deployment}"
        exit 1
      fi
    done
    return 0
  fi

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
  kubectl delete cronjob alfaka-news-daily-summary-nvda \
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

verify_simulator_rollout() {
  if is_true "${CHART_INTERPRETATION_ONLY}"; then
    return 0
  fi
  if is_true "${DRY_RUN}"; then
    printf 'DRY_RUN=true: skipping simulator rollout status.\n'
    return 0
  fi

  if ! kubectl rollout status deployment/gops-simulator -n "${K8S_NAMESPACE}" --timeout=600s; then
    "${WORKTREE_DIR}/scripts/aws/print-rollout-diagnostics.sh" gops-simulator
    exit 1
  fi
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

  IMAGE_TAG="${IMAGE_TAG:-${TARGET_SHA:0:7}-$(date -u +%Y%m%d%H%M%S)}"
  export AWS_ACCOUNT_ID AWS_REGION DOCKER_PLATFORM IMAGE_TAG K8S_NAMESPACE VITE_LOGO_DEV_ATTRIBUTION

  read_deploy_state
  last_successful_sha="$(deploy_state_value "lastSuccessfulSha")"
  if [[ -n "${last_successful_sha}" ]]; then
    printf 'Last successful local deploy SHA (legacy/global): %s\n' "${last_successful_sha}"
  fi

  detect_services
  apply_chart_interpretation_scope
  apply_platform_if_requested

  if [[ -z "${SELECTED_SERVICES}" ]]; then
    printf 'No app service changes detected for %s. Nothing to deploy.\n' "${DEPLOY_TARGET_REF}"
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
  if ! is_true "${CHART_INTERPRETATION_ONLY}"; then
    prepare_kustomize_overlay
  fi

  (
    cd "${WORKTREE_DIR}"
    K8S_NAMESPACE="${K8S_NAMESPACE}" scripts/aws/validate-dedicated-platform.sh
  )

  run_simulator_replay_import_if_selected
  run_migrations_if_requested
  deploy_app_workloads
  verify_simulator_rollout
  if ! is_true "${CHART_INTERPRETATION_ONLY}"; then
    verify_ai_coach_snapshot_archive
  fi
  run_smoke_tests
  run_news_cache_rebuild_if_requested

  if ! is_true "${DRY_RUN}"; then
    APP_APPLIED="false"
    write_state_configmap "${STATE_UPDATE_MODE}"
  fi

  printf 'Local dev deploy completed for %s (%s): %s\n' "${DEPLOY_TARGET_REF}" "${TARGET_SHA}" "${SELECTED_SERVICES}"
}

main "$@"
