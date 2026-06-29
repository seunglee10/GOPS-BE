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
