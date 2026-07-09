#!/usr/bin/env bash
# 역할: backup-postgres-logical.sh가 만든 pg_dump custom archive를 EKS Postgres에 복원합니다.
# 주의: 새 PVC의 빈 Postgres 또는 명시적으로 정리 승인된 DB에만 실행합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
TARGET_POD="${TARGET_POD:-postgres-0}"
ARTIFACT_PATH="${POSTGRES_DUMP_PATH:-${REPO_ROOT}/.local-artifacts/postgres/postgres.dump}"
REMOTE_PATH="${REMOTE_PATH:-/tmp/gops-postgres.dump}"
CLEAN_BEFORE_RESTORE=false

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --artifact PATH          Input pg_dump archive path. Default: ${ARTIFACT_PATH}
  --namespace NAME         Kubernetes namespace. Default: ${NAMESPACE}
  --target-pod NAME        Postgres pod to restore into. Default: ${TARGET_POD}
  --clean-before-restore   Pass pg_restore --clean --if-exists. Use only after explicit approval.
  -h, --help               Show this help.

Environment:
  POSTGRES_DUMP_PATH       Same as --artifact.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --artifact)
      ARTIFACT_PATH="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --target-pod)
      TARGET_POD="$2"
      shift 2
      ;;
    --clean-before-restore)
      CLEAN_BEFORE_RESTORE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

main() {
  require_command kubectl

  if [ ! -f "${ARTIFACT_PATH}" ]; then
    echo "Postgres dump not found: ${ARTIFACT_PATH}" >&2
    exit 1
  fi

  kubectl get pod "${TARGET_POD}" -n "${NAMESPACE}" >/dev/null

  echo "copy: ${ARTIFACT_PATH} -> pod/${TARGET_POD}:${REMOTE_PATH}"
  kubectl cp "${ARTIFACT_PATH}" "${NAMESPACE}/${TARGET_POD}:${REMOTE_PATH}"

  restore_flags=(--no-owner --no-privileges)
  if [ "${CLEAN_BEFORE_RESTORE}" = "true" ]; then
    restore_flags+=(--clean --if-exists)
  fi

  echo "restore: pod/${TARGET_POD}:${REMOTE_PATH}"
  kubectl exec "${TARGET_POD}" -n "${NAMESPACE}" -- env REMOTE_PATH="${REMOTE_PATH}" sh -lc '
    pg_restore "$@" \
      -U "${POSTGRES_USER:-gops}" \
      -d "${POSTGRES_DB:-gops}" \
      "${REMOTE_PATH}"
  ' sh "${restore_flags[@]}"

  kubectl exec "${TARGET_POD}" -n "${NAMESPACE}" -- sh -lc 'pg_isready -U "${POSTGRES_USER:-gops}" -d "${POSTGRES_DB:-gops}"'
  echo "done: Postgres restore completed"
}

main "$@"
