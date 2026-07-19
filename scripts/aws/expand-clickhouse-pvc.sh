#!/usr/bin/env bash
# 역할: dev EKS ClickHouse PVC를 데이터 보존 상태로 확장하고 실제 파일시스템 여유를 검증합니다.
set -Eeuo pipefail

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-<aws-account-id>}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-gops-eks-cluster}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
PVC_NAME="${CLICKHOUSE_PVC_NAME:-clickhouse-data-clickhouse-0}"
TARGET_STORAGE_GIB="${CLICKHOUSE_TARGET_STORAGE_GIB:-80}"
MIN_FREE_GIB="${CLICKHOUSE_MIN_FREE_GIB:-15}"
CHECK_ONLY=false

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=true
elif [[ -n "${1:-}" ]]; then
  printf 'Usage: %s [--check]\n' "$0" >&2
  exit 2
fi

require_command() {
  command -v "$1" >/dev/null 2>&1 || { printf 'Required command not found: %s\n' "$1" >&2; exit 1; }
}

configure_cluster() {
  local actual_account
  actual_account="$(aws sts get-caller-identity --query Account --output text)"
  if [[ "${actual_account}" != "${AWS_ACCOUNT_ID}" ]]; then
    printf 'AWS account mismatch: expected %s, got %s\n' "${AWS_ACCOUNT_ID}" "${actual_account}" >&2
    exit 1
  fi
  aws eks update-kubeconfig --name "${EKS_CLUSTER_NAME}" --region "${AWS_REGION}" >/dev/null
  kubectl get namespace "${K8S_NAMESPACE}" >/dev/null
}

storage_gib() {
  local value="$1"
  if [[ ! "${value}" =~ ^([0-9]+)Gi$ ]]; then
    printf 'Unsupported PVC storage value: %s\n' "${value}" >&2
    exit 1
  fi
  printf '%s' "${BASH_REMATCH[1]}"
}

verify_capacity_and_free_space() {
  local requested capacity requested_gib capacity_gib free_kib min_free_kib
  requested="$(kubectl get pvc "${PVC_NAME}" -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.resources.requests.storage}')"
  capacity="$(kubectl get pvc "${PVC_NAME}" -n "${K8S_NAMESPACE}" -o jsonpath='{.status.capacity.storage}')"
  requested_gib="$(storage_gib "${requested}")"
  capacity_gib="$(storage_gib "${capacity}")"
  if (( requested_gib < TARGET_STORAGE_GIB || capacity_gib < TARGET_STORAGE_GIB )); then
    printf 'ClickHouse PVC is smaller than %sGi: request=%s capacity=%s\n' \
      "${TARGET_STORAGE_GIB}" "${requested}" "${capacity}" >&2
    return 1
  fi
  free_kib="$(kubectl exec statefulset/clickhouse -n "${K8S_NAMESPACE}" -- \
    df -Pk /var/lib/clickhouse | awk 'END {print $4}')"
  min_free_kib=$((MIN_FREE_GIB * 1024 * 1024))
  if [[ ! "${free_kib}" =~ ^[0-9]+$ ]] || (( free_kib < min_free_kib )); then
    printf 'ClickHouse free space is below %sGi: %sKiB\n' "${MIN_FREE_GIB}" "${free_kib:-unknown}" >&2
    return 1
  fi
  printf 'ClickHouse storage verified: request=%s capacity=%s free=%sGi\n' \
    "${requested}" "${capacity}" "$((free_kib / 1024 / 1024))"
}

require_command aws
require_command kubectl
configure_cluster

if [[ "${CHECK_ONLY}" == "true" ]]; then
  verify_capacity_and_free_space
  exit 0
fi

storage_class="$(kubectl get pvc "${PVC_NAME}" -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.storageClassName}')"
allow_expansion="$(kubectl get storageclass "${storage_class}" -o jsonpath='{.allowVolumeExpansion}')"
if [[ "${allow_expansion}" != "true" ]]; then
  printf 'StorageClass %s does not allow volume expansion.\n' "${storage_class}" >&2
  exit 1
fi

current_request="$(kubectl get pvc "${PVC_NAME}" -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.resources.requests.storage}')"
current_request_gib="$(storage_gib "${current_request}")"
if (( current_request_gib < TARGET_STORAGE_GIB )); then
  kubectl patch pvc "${PVC_NAME}" -n "${K8S_NAMESPACE}" --type merge \
    -p "{\"spec\":{\"resources\":{\"requests\":{\"storage\":\"${TARGET_STORAGE_GIB}Gi\"}}}}"
  printf 'ClickHouse PVC expansion requested: %s -> %sGi\n' "${current_request}" "${TARGET_STORAGE_GIB}"
else
  printf 'ClickHouse PVC request is already %s (target %sGi).\n' "${current_request}" "${TARGET_STORAGE_GIB}"
fi

for attempt in $(seq 1 120); do
  if verify_capacity_and_free_space; then
    exit 0
  fi
  printf 'Waiting for ClickHouse filesystem expansion (%s/120)...\n' "${attempt}"
  sleep 10
done

printf 'ClickHouse PVC did not reach the required capacity within 20 minutes.\n' >&2
exit 1
