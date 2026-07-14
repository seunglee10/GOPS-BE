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

SELECTED_KEYS=""

service_selected() {
  local expected="$1"
  local selected

  for selected in ${SELECTED_KEYS}; do
    if [[ "${selected}" == "${expected}" ]]; then
      return 0
    fi
  done
  return 1
}

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
  if ! service_selected "${key}"; then
    SELECTED_KEYS="${SELECTED_KEYS}${SELECTED_KEYS:+ }${key}"
  fi
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

tag_map_file="$(mktemp)"
tmp_file="$(mktemp)"
cleanup() {
  rm -f "${tag_map_file}" "${tmp_file}"
}
trap cleanup EXIT

while IFS=$'\t' read -r key _repository _env_var _dockerfile; do
  image="$(gops_image_url_for_key "${key}")"
  if service_selected "${key}"; then
    tag="${IMAGE_TAG}"
  else
    tag="$(current_tag_for_service "${key}")"
  fi

  printf '%s\t%s\n' "${image}" "${tag}" >> "${tag_map_file}"
  printf 'CI image tag: %-20s %s:%s\n' "${key}" "${image}" "${tag}"
done < <(gops_image_entries)

tag_for_image() {
  local expected="$1"
  local mapped_image
  local mapped_tag

  while IFS=$'\t' read -r mapped_image mapped_tag; do
    if [[ "${mapped_image}" == "${expected}" ]]; then
      printf '%s\n' "${mapped_tag}"
      return 0
    fi
  done < "${tag_map_file}"
  return 1
}

current_image=""
while IFS= read -r line || [[ -n "${line}" ]]; do
  if [[ "${line}" =~ ^[[:space:]]*-[[:space:]]name:[[:space:]](.*)$ ]]; then
    current_image="${BASH_REMATCH[1]}"
    printf '%s\n' "${line}" >> "${tmp_file}"
    continue
  fi

  if [[ "${line}" =~ ^([[:space:]]*)newTag:[[:space:]].*$ && -n "${current_image}" ]]; then
    if mapped_tag="$(tag_for_image "${current_image}")"; then
      printf '%snewTag: "%s"\n' "${BASH_REMATCH[1]}" "${mapped_tag}" >> "${tmp_file}"
      continue
    fi
  fi

  printf '%s\n' "${line}" >> "${tmp_file}"
done < "${KUSTOMIZATION_FILE}"

mv "${tmp_file}" "${KUSTOMIZATION_FILE}"
tmp_file=""

if grep -R "ci-placeholder" "${KUSTOMIZE_OVERLAY}"; then
  echo "Image tag placeholder still remains in ${KUSTOMIZE_OVERLAY}" >&2
  exit 1
fi
