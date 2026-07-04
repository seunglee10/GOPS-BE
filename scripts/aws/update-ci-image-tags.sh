#!/usr/bin/env bash
# 역할: 선택된 서비스만 새 이미지 태그로 바꾸고, 나머지는 현재 클러스터 태그를 유지합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

KUSTOMIZE_OVERLAY="${KUSTOMIZE_OVERLAY:-infra/k8s/overlays/aws-incluster-app-ci}"
KUSTOMIZATION_FILE="${KUSTOMIZE_OVERLAY}/kustomization.yaml"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG를 넣어주세요.}"
SERVICES="${SERVICES:?SERVICES를 넣어주세요.}"

declare -A SELECTED=()
declare -A TAG_BY_IMAGE=()

read -r -a requested_services <<< "${SERVICES//,/ }"
for requested_service in "${requested_services[@]}"; do
  if [[ -z "${requested_service}" ]]; then
    continue
  fi

  key="$(gops_normalize_service_key "${requested_service}")"
  if ! gops_service_exists "${key}"; then
    printf 'Unknown service: %s\n' "${requested_service}" >&2
    exit 1
  fi
  SELECTED["${key}"]=1
done

current_tag_for_service() {
  local key="$1"
  local deployment
  local current_image

  deployment="$(gops_primary_deployment_for_service "${key}")"
  current_image="$(kubectl get deployment "${deployment}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}')"

  if [[ -z "${current_image}" ]]; then
    printf '현재 이미지 태그를 찾을 수 없습니다: deployment/%s\n' "${deployment}" >&2
    return 1
  fi
  if [[ "${current_image}" == *@* ]]; then
    printf 'digest 기반 이미지는 CI overlay newTag로 유지할 수 없습니다: %s\n' "${current_image}" >&2
    return 1
  fi

  echo "${current_image##*:}"
}

while IFS=$'\t' read -r key _repository _env_var _dockerfile; do
  image="$(gops_image_url_for_key "${key}")"
  if [[ -n "${SELECTED[${key}]:-}" ]]; then
    tag="${IMAGE_TAG}"
  else
    tag="$(current_tag_for_service "${key}")"
  fi

  TAG_BY_IMAGE["${image}"]="${tag}"
  printf 'CI image tag: %-20s %s:%s\n' "${key}" "${image}" "${tag}"
done < <(gops_image_entries)

tmp_file="$(mktemp)"
current_image=""
while IFS= read -r line || [[ -n "${line}" ]]; do
  if [[ "${line}" =~ ^[[:space:]]*-[[:space:]]name:[[:space:]](.*)$ ]]; then
    current_image="${BASH_REMATCH[1]}"
    printf '%s\n' "${line}" >> "${tmp_file}"
    continue
  fi

  if [[ "${line}" =~ ^([[:space:]]*)newTag:[[:space:]].*$ && -n "${current_image}" && -n "${TAG_BY_IMAGE[${current_image}]:-}" ]]; then
    printf '%snewTag: "%s"\n' "${BASH_REMATCH[1]}" "${TAG_BY_IMAGE[${current_image}]}" >> "${tmp_file}"
    continue
  fi

  printf '%s\n' "${line}" >> "${tmp_file}"
done < "${KUSTOMIZATION_FILE}"

mv "${tmp_file}" "${KUSTOMIZATION_FILE}"

if grep -R "ci-placeholder" "${KUSTOMIZE_OVERLAY}"; then
  echo "Image tag placeholder still remains in ${KUSTOMIZE_OVERLAY}" >&2
  exit 1
fi
