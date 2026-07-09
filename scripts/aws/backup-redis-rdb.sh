#!/usr/bin/env bash
# 역할: Redis memory 상태를 RDB 파일로 저장해 로컬 백업 artifact로 가져옵니다.
# 주의: 현재 live Redis가 AOF/RDB 자동 저장 없이 뜬 경우에도 수동 SAVE로 기준 백업을 만듭니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
SOURCE_POD="${SOURCE_POD:-redis-0}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
ARTIFACT_PATH="${REDIS_RDB_PATH:-${REPO_ROOT}/.local-artifacts/redis/${RUN_ID}/dump.rdb}"
REMOTE_COPY_PATH="${REMOTE_COPY_PATH:-/tmp/gops-redis-dump.rdb}"
FORCE=false

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --artifact PATH      Output RDB path. Default: ${ARTIFACT_PATH}
  --namespace NAME     Kubernetes namespace. Default: ${NAMESPACE}
  --source-pod NAME    Redis pod to save. Default: ${SOURCE_POD}
  --force              Overwrite an existing archive.
  -h, --help           Show this help.

Environment:
  REDIS_RDB_PATH       Same as --artifact.
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
    echo "Redis RDB already exists: ${ARTIFACT_PATH}" >&2
    echo "Re-run with --force to overwrite it." >&2
    exit 1
  fi

  artifact_dir="$(dirname "${ARTIFACT_PATH}")"
  mkdir -p "${artifact_dir}"
  tmp_artifact="${ARTIFACT_PATH}.tmp"
  trap 'rm -f "${tmp_artifact}"' EXIT INT TERM

  kubectl get pod "${SOURCE_POD}" -n "${NAMESPACE}" >/dev/null

  echo "backup: pod/${SOURCE_POD} Redis memory -> ${ARTIFACT_PATH}"
  kubectl exec "${SOURCE_POD}" -n "${NAMESPACE}" -- env REMOTE_COPY_PATH="${REMOTE_COPY_PATH}" sh -lc '
    set -e
    redis-cli SAVE
    redis_dir="$(redis-cli CONFIG GET dir | tail -n 1)"
    redis_dbfilename="$(redis-cli CONFIG GET dbfilename | tail -n 1)"
    test -n "${redis_dir}"
    test -n "${redis_dbfilename}"
    cp "${redis_dir}/${redis_dbfilename}" "${REMOTE_COPY_PATH}"
    ls -lh "${REMOTE_COPY_PATH}"
  '

  kubectl exec "${SOURCE_POD}" -n "${NAMESPACE}" -- cat "${REMOTE_COPY_PATH}" > "${tmp_artifact}"
  mv "${tmp_artifact}" "${ARTIFACT_PATH}"

  (
    cd "${artifact_dir}"
    shasum -a 256 "$(basename "${ARTIFACT_PATH}")" > SHA256SUMS.txt
  )

  echo "done: Redis RDB written to ${ARTIFACT_PATH}"
  echo "done: checksum written to ${artifact_dir}/SHA256SUMS.txt"
}

main "$@"
