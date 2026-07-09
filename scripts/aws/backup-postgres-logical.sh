#!/usr/bin/env bash
# 역할: EKS Postgres를 pg_dump custom archive로 백업합니다.
# 주의: app/worker write를 멈춘 뒤 실행해야 일관된 rebuild 기준 백업이 됩니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
SOURCE_POD="${SOURCE_POD:-postgres-0}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
ARTIFACT_PATH="${POSTGRES_DUMP_PATH:-${REPO_ROOT}/.local-artifacts/postgres/${RUN_ID}/postgres.dump}"
FORCE=false

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --artifact PATH      Output pg_dump archive path. Default: ${ARTIFACT_PATH}
  --namespace NAME     Kubernetes namespace. Default: ${NAMESPACE}
  --source-pod NAME    Postgres pod to dump. Default: ${SOURCE_POD}
  --force              Overwrite an existing archive.
  -h, --help           Show this help.

Environment:
  POSTGRES_DUMP_PATH   Same as --artifact.
  RUN_ID               Backup run id used in the default output path.
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
    --source-pod)
      SOURCE_POD="$2"
      shift 2
      ;;
    --force)
      FORCE=true
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
  require_command shasum

  if [ -f "${ARTIFACT_PATH}" ] && [ "${FORCE}" != "true" ]; then
    echo "Archive already exists: ${ARTIFACT_PATH}" >&2
    echo "Re-run with --force to overwrite it." >&2
    exit 1
  fi

  artifact_dir="$(dirname "${ARTIFACT_PATH}")"
  mkdir -p "${artifact_dir}"
  tmp_archive="${ARTIFACT_PATH}.tmp"
  trap 'rm -f "${tmp_archive}"' EXIT INT TERM

  kubectl get pod "${SOURCE_POD}" -n "${NAMESPACE}" >/dev/null

  echo "backup: pod/${SOURCE_POD} -> ${ARTIFACT_PATH}"
  kubectl exec "${SOURCE_POD}" -n "${NAMESPACE}" -- sh -lc '
    pg_dump \
      -U "${POSTGRES_USER:-gops}" \
      -d "${POSTGRES_DB:-gops}" \
      --format=custom \
      --no-owner \
      --no-privileges
  ' > "${tmp_archive}"

  mv "${tmp_archive}" "${ARTIFACT_PATH}"

  (
    cd "${artifact_dir}"
    shasum -a 256 "$(basename "${ARTIFACT_PATH}")" > SHA256SUMS.txt
  )

  echo "done: Postgres dump written to ${ARTIFACT_PATH}"
  echo "done: checksum written to ${artifact_dir}/SHA256SUMS.txt"
}

main "$@"
