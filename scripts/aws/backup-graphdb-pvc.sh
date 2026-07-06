#!/usr/bin/env bash
# 역할: EKS GraphDB PVC 내용을 local bootstrap archive로 저장합니다.
# 주의: 서비스 재구성 직전, writer를 멈춘 상태에서 실행하는 bootstrap 용도입니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
SOURCE_POD="${SOURCE_POD:-graphdb-0}"
SOURCE_PATH="${SOURCE_PATH:-/opt/graphdb/home}"
ARTIFACT_PATH="${GRAPHDB_VOLUME_TGZ:-${REPO_ROOT}/.local-artifacts/graphdb/graphdb-volume.tgz}"
FORCE=false

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --artifact PATH      Output archive path. Default: ${ARTIFACT_PATH}
  --namespace NAME     Kubernetes namespace. Default: ${NAMESPACE}
  --source-pod NAME    GraphDB pod to archive. Default: ${SOURCE_POD}
  --source-path PATH   Path inside the pod. Default: ${SOURCE_PATH}
  --force              Overwrite an existing archive.
  -h, --help           Show this help.

Environment:
  GRAPHDB_VOLUME_TGZ   Same as --artifact.
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
    --source-path)
      SOURCE_PATH="$2"
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
  kubectl exec "${SOURCE_POD}" -n "${NAMESPACE}" -- test -d "${SOURCE_PATH}"

  echo "backup: pod/${SOURCE_POD}:${SOURCE_PATH} -> ${ARTIFACT_PATH}"
  kubectl exec "${SOURCE_POD}" -n "${NAMESPACE}" -- tar czf - -C "${SOURCE_PATH}" . > "${tmp_archive}"
  mv "${tmp_archive}" "${ARTIFACT_PATH}"

  (
    cd "${artifact_dir}"
    shasum -a 256 "$(basename "${ARTIFACT_PATH}")" > SHA256SUMS.txt
  )

  echo "done: GraphDB archive written to ${ARTIFACT_PATH}"
  echo "done: checksum written to ${artifact_dir}/SHA256SUMS.txt"
}

main "$@"
