#!/usr/bin/env bash
# 역할: 데이터 보존형 EKS rebuild 전에 PVC, DB, Kafka, Redis 상태 inventory를 파일로 남깁니다.
# 주의: 읽기 전용 점검 스크립트입니다. write 중지 후 한 번 더 실행해 백업 기준값으로 사용합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.local-artifacts/platform-backup/${RUN_ID}}"

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --namespace NAME    Kubernetes namespace. Default: ${NAMESPACE}
  --output-dir PATH   Directory for inventory files. Default: ${OUTPUT_DIR}
  -h, --help          Show this help.

Environment:
  NAMESPACE           Same as --namespace.
  OUTPUT_DIR          Same as --output-dir.
  RUN_ID              Inventory run id used in the default output path.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
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

capture() {
  local name="$1"
  shift
  echo "capture: ${name}"
  if ! "$@" > "${OUTPUT_DIR}/${name}" 2>&1; then
    echo "warning: failed to capture ${name}" | tee -a "${OUTPUT_DIR}/WARNINGS.txt" >&2
  fi
}

capture_pod_exec() {
  local pod="$1"
  local name="$2"
  shift 2

  echo "capture: ${pod}:${name}"
  if ! kubectl get pod "${pod}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "skip: pod/${pod} not found" > "${OUTPUT_DIR}/${name}"
    return
  fi

  if ! kubectl exec "${pod}" -n "${NAMESPACE}" -- "$@" > "${OUTPUT_DIR}/${name}" 2>&1; then
    echo "warning: failed to capture ${pod}:${name}" | tee -a "${OUTPUT_DIR}/WARNINGS.txt" >&2
  fi
}

write_metadata() {
  {
    echo "run_id=${RUN_ID}"
    echo "namespace=${NAMESPACE}"
    echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "repo_root=${REPO_ROOT}"
  } > "${OUTPUT_DIR}/METADATA.env"
}

write_checksums() {
  if ! command -v shasum >/dev/null 2>&1; then
    echo "skip: shasum not found"
    return
  fi

  (
    cd "${OUTPUT_DIR}"
    find . -type f ! -name SHA256SUMS.txt -print | sort | xargs shasum -a 256 > SHA256SUMS.txt
  )
}

main() {
  require_command kubectl
  mkdir -p "${OUTPUT_DIR}"
  : > "${OUTPUT_DIR}/WARNINGS.txt"
  write_metadata

  capture namespace.txt kubectl get namespace "${NAMESPACE}" -o wide
  capture statefulsets-wide.txt kubectl get statefulset -n "${NAMESPACE}" -o wide
  capture deployments-wide.txt kubectl get deployment -n "${NAMESPACE}" -o wide
  capture jobs-wide.txt kubectl get job -n "${NAMESPACE}" -o wide
  capture cronjobs-wide.txt kubectl get cronjob -n "${NAMESPACE}" -o wide
  capture pods-wide.txt kubectl get pod -n "${NAMESPACE}" -o wide
  capture pvc-wide.txt kubectl get pvc -n "${NAMESPACE}" -o wide
  capture pvc.yaml kubectl get pvc -n "${NAMESPACE}" -o yaml
  capture pv.yaml kubectl get pv -o yaml
  capture nodepools.yaml kubectl get nodepool -o yaml

  capture_pod_exec postgres-0 postgres-disk-and-row-counts.txt sh -lc '
    set -e
    df -h /var/lib/postgresql/data
    psql -U "${POSTGRES_USER:-gops}" -d "${POSTGRES_DB:-gops}" -Atc "select schemaname || '"'"'.'"'"' || relname || E'"'"'\t'"'"' || n_live_tup from pg_stat_user_tables order by 1;"
  '

  capture_pod_exec clickhouse-0 clickhouse-disk-and-table-counts.txt sh -lc '
    set -e
    df -h /var/lib/clickhouse
    clickhouse-client \
      --user "${CLICKHOUSE_USER:-default}" \
      --password="${CLICKHOUSE_PASSWORD:-}" \
      --query "SELECT database, table, total_rows, total_bytes FROM system.tables WHERE database NOT IN ('"'"'system'"'"','"'"'INFORMATION_SCHEMA'"'"','"'"'information_schema'"'"') ORDER BY database, table FORMAT TSVWithNames"
  '

  capture_pod_exec graphdb-0 graphdb-disk-and-repositories.txt sh -lc '
    set -e
    df -h /opt/graphdb/home
    du -sh /opt/graphdb/home || true
    find /opt/graphdb/home/data/repositories -maxdepth 3 -type f -name config.ttl -print 2>/dev/null || true
  '

  capture_pod_exec redis-0 redis-disk-and-key-inventory.txt sh -lc '
    set -e
    df -h /data
    redis-cli PING
    redis-cli DBSIZE
    redis-cli INFO persistence
    redis-cli --scan | awk -F: '"'"'{count[$1]++} END {for (prefix in count) print prefix "\t" count[prefix]}'"'"' | sort
  '

  capture_pod_exec kafka-0 kafka-disk-topics-consumer-groups.txt bash -lc '
    set -e
    df -h /var/lib/kafka
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:29092 --list
    /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:29092 --all-groups --describe || true
  '

  write_checksums
  echo "done: inventory written to ${OUTPUT_DIR}"
}

main "$@"
