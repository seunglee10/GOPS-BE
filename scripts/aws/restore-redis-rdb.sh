#!/usr/bin/env bash
# Restore a local Redis RDB backup into the EKS Redis PVC.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
STATEFULSET_NAME="${STATEFULSET_NAME:-redis}"
PVC_NAME="${PVC_NAME:-redis-data-redis-0}"
RESTORE_POD_NAME="${RESTORE_POD_NAME:-redis-rdb-restore}"
ARTIFACT_PATH="${REDIS_RDB_PATH:-${REPO_ROOT}/.local-artifacts/redis/20260709T014811Z/dump.rdb}"
S3_URI="${S3_URI:-}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
RESTORE_IMAGE="${RESTORE_IMAGE:-alpine:3.20}"
RESTORE_NODEPOOL="${RESTORE_NODEPOOL:-cache-db}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300s}"
CHUNK_SIZE="${CHUNK_SIZE:-32m}"
FORCE_RESTORE=false
START_REDIS=true

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --artifact PATH      Redis RDB backup path. Default: ${ARTIFACT_PATH}
  --s3-uri URI         Download the RDB from S3 inside the restore pod.
  --namespace NAME     Kubernetes namespace. Default: ${NAMESPACE}
  --force              Clear the target Redis data directory before restoring.
  --no-start           Leave the redis StatefulSet scaled to 0 after restore.
  -h, --help           Show this help.

Environment:
  REDIS_RDB_PATH       Same as --artifact.
  S3_URI               Same as --s3-uri.
  AWS_REGION           Region used for S3 presigned URLs. Default: ${AWS_REGION}
  RESTORE_NODEPOOL     NodePool used by the restore pod. Default: ${RESTORE_NODEPOOL}
  CHUNK_SIZE           Local split chunk size for kubectl streaming. Default: ${CHUNK_SIZE}
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --artifact)
      ARTIFACT_PATH="$2"
      shift 2
      ;;
    --s3-uri)
      S3_URI="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --force)
      FORCE_RESTORE=true
      shift
      ;;
    --no-start)
      START_REDIS=false
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

jsonpath() {
  kubectl get "$1" "$2" -n "${NAMESPACE}" -o "jsonpath=$3" 2>/dev/null || true
}

cleanup_restore_pod() {
  kubectl delete pod "${RESTORE_POD_NAME}" -n "${NAMESPACE}" --ignore-not-found=true >/dev/null 2>&1 || true
}

verify_checksum_if_available() {
  checksum_file="$(dirname "${ARTIFACT_PATH}")/SHA256SUMS.txt"
  if [ ! -f "${checksum_file}" ]; then
    echo "skip: SHA256SUMS.txt not found next to ${ARTIFACT_PATH}"
    return
  fi

  checksum_dir="$(dirname "${ARTIFACT_PATH}")"
  checksum_name="$(basename "${ARTIFACT_PATH}")"
  checksum_line="$(grep "  ${checksum_name}\$" "${checksum_file}" || true)"
  if [ -z "${checksum_line}" ]; then
    echo "warning: ${checksum_file} does not include ${checksum_name}; checksum was not verified" >&2
    return
  fi

  (
    cd "${checksum_dir}"
    printf '%s\n' "${checksum_line}" | shasum -a 256 -c -
  )
}

scale_redis_down() {
  if ! kubectl get statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "StatefulSet ${STATEFULSET_NAME} does not exist in namespace ${NAMESPACE}" >&2
    exit 1
  fi

  echo "scale: statefulset/${STATEFULSET_NAME} -> 0"
  kubectl scale statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" --replicas=0 >/dev/null
  kubectl wait --for=delete "pod/${STATEFULSET_NAME}-0" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}" >/dev/null 2>&1 || true
}

scale_redis_up() {
  if [ "${START_REDIS}" != "true" ]; then
    echo "skip: redis StatefulSet left at 0 replicas"
    return
  fi

  echo "scale: statefulset/${STATEFULSET_NAME} -> 1"
  kubectl scale statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" --replicas=1 >/dev/null
  kubectl rollout status "statefulset/${STATEFULSET_NAME}" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

patch_redis_for_rdb_load() {
  echo "patch: statefulset/${STATEFULSET_NAME} temporary appendonly=no for RDB load"
  kubectl patch statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" --type=json -p='[
    {
      "op": "replace",
      "path": "/spec/template/spec/containers/0/command",
      "value": [
        "redis-server",
        "--appendonly",
        "no",
        "--save",
        "",
        "--dir",
        "/data",
        "--maxmemory",
        "3gb",
        "--maxmemory-policy",
        "volatile-lru"
      ]
    }
  ]' >/dev/null
}

restore_redis_manifest() {
  echo "apply: infra/k8s/base/platform/redis-statefulset.yaml"
  kubectl apply -f "${REPO_ROOT}/infra/k8s/base/platform/redis-statefulset.yaml" >/dev/null
  kubectl rollout status "statefulset/${STATEFULSET_NAME}" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

create_restore_pod() {
  cleanup_restore_pod
  echo "create: pod/${RESTORE_POD_NAME}"
  kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${RESTORE_POD_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: redis-rdb-restore
  annotations:
    cloudwatch.aws.amazon.com/auto-annotate-dotnet: "false"
    cloudwatch.aws.amazon.com/auto-annotate-java: "false"
    cloudwatch.aws.amazon.com/auto-annotate-nodejs: "false"
    cloudwatch.aws.amazon.com/auto-annotate-python: "false"
    instrumentation.opentelemetry.io/inject-dotnet: "false"
    instrumentation.opentelemetry.io/inject-java: "false"
    instrumentation.opentelemetry.io/inject-nodejs: "false"
    instrumentation.opentelemetry.io/inject-python: "false"
spec:
  restartPolicy: Never
  nodeSelector:
    karpenter.sh/nodepool: ${RESTORE_NODEPOOL}
  tolerations:
    - key: gops.io/dedicated
      operator: Equal
      value: ${RESTORE_NODEPOOL}
      effect: NoSchedule
  containers:
    - name: restore
      image: ${RESTORE_IMAGE}
      command:
        - sh
        - -c
        - sleep 3600
      volumeMounts:
        - name: redis-data
          mountPath: /volume
  volumes:
    - name: redis-data
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
YAML
  kubectl wait --for=condition=Ready "pod/${RESTORE_POD_NAME}" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

restore_rdb() {
  if kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'test -n "$(find /volume -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)"'; then
    if [ "${FORCE_RESTORE}" != "true" ]; then
      echo "PVC ${PVC_NAME} is not empty. Re-run with --force to clear it before restore." >&2
      exit 1
    fi
    echo "clear: /volume"
    kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'find /volume -mindepth 1 -maxdepth 1 ! -name lost+found -exec rm -rf {} +'
  fi

  echo "copy: ${ARTIFACT_PATH} -> pod/${RESTORE_POD_NAME}:/volume/dump.rdb"
  if [ -n "${S3_URI}" ]; then
    presigned_url="$(aws s3 presign "${S3_URI}" --expires-in 3600 --region "${AWS_REGION}")"
    kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- rm -f /volume/dump.rdb.tmp /volume/dump.rdb
    kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- env REDIS_RDB_URL="${presigned_url}" sh -lc '
      set -e
      command -v wget >/dev/null
      wget -q -O /volume/dump.rdb.tmp "${REDIS_RDB_URL}"
      mv /volume/dump.rdb.tmp /volume/dump.rdb
    '
    kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'ls -lh /volume/dump.rdb && test -s /volume/dump.rdb'
    verify_restored_file_checksum
    return
  fi

  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' RETURN
  split -b "${CHUNK_SIZE}" "${ARTIFACT_PATH}" "${tmp_dir}/redis-rdb-part-"
  kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- rm -f /volume/dump.rdb.tmp /volume/dump.rdb
  for chunk in "${tmp_dir}"/redis-rdb-part-*; do
    echo "copy chunk: $(basename "${chunk}")"
    kubectl exec -i "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'cat >> /volume/dump.rdb.tmp' < "${chunk}"
  done
  kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- mv /volume/dump.rdb.tmp /volume/dump.rdb
  kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'ls -lh /volume/dump.rdb && test -s /volume/dump.rdb'

  verify_restored_file_checksum
}

verify_restored_file_checksum() {
  expected_sha="$(shasum -a 256 "${ARTIFACT_PATH}" | awk '{print $1}')"
  actual_sha="$(kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sha256sum /volume/dump.rdb | awk '{print $1}')"
  if [ "${expected_sha}" != "${actual_sha}" ]; then
    echo "Redis RDB checksum mismatch after copy." >&2
    echo "expected: ${expected_sha}" >&2
    echo "actual:   ${actual_sha}" >&2
    exit 1
  fi
  echo "verify: remote RDB checksum matches local artifact"
}

wait_for_redis_ready() {
  kubectl wait --for=condition=Ready "pod/${STATEFULSET_NAME}-0" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
  kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli PING
}

enable_aof_from_loaded_rdb() {
  loaded_keys="$(kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli DBSIZE | tr -d '\r')"
  if [ "${loaded_keys}" = "0" ]; then
    echo "Redis loaded 0 keys from the restored RDB; refusing to finalize." >&2
    exit 1
  fi

  echo "verify: Redis loaded ${loaded_keys} keys from RDB"
  echo "enable: appendonly yes"
  kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli CONFIG SET appendonly yes

  for _ in $(seq 1 120); do
    rewrite_in_progress="$(kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli INFO persistence | awk -F: '/^aof_rewrite_in_progress:/{gsub("\r","",$2); print $2}')"
    if [ "${rewrite_in_progress}" = "0" ]; then
      break
    fi
    sleep 2
  done

  rewrite_in_progress="$(kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli INFO persistence | awk -F: '/^aof_rewrite_in_progress:/{gsub("\r","",$2); print $2}')"
  if [ "${rewrite_in_progress}" != "0" ]; then
    echo "AOF rewrite did not finish before timeout." >&2
    exit 1
  fi

  echo "verify: AOF rewrite finished"
}

verify_final_redis() {
  wait_for_redis_ready
  final_keys="$(kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli DBSIZE | tr -d '\r')"
  aof_enabled="$(kubectl exec "${STATEFULSET_NAME}-0" -n "${NAMESPACE}" -- redis-cli INFO persistence | awk -F: '/^aof_enabled:/{gsub("\r","",$2); print $2}')"
  if [ "${final_keys}" = "0" ] || [ "${aof_enabled}" != "1" ]; then
    echo "Redis final verification failed: keys=${final_keys}, aof_enabled=${aof_enabled}" >&2
    exit 1
  fi
  echo "verify: Redis final keys=${final_keys}, aof_enabled=${aof_enabled}"
}

main() {
  require_command kubectl
  require_command shasum
  require_command split
  if [ -n "${S3_URI}" ]; then
    require_command aws
  fi

  if [ ! -f "${ARTIFACT_PATH}" ]; then
    echo "Redis RDB backup not found: ${ARTIFACT_PATH}" >&2
    exit 1
  fi

  verify_checksum_if_available
  scale_redis_down
  create_restore_pod
  restore_rdb
  cleanup_restore_pod
  patch_redis_for_rdb_load
  scale_redis_up
  wait_for_redis_ready
  enable_aof_from_loaded_rdb
  restore_redis_manifest
  verify_final_redis

  echo "done: Redis RDB restored from ${ARTIFACT_PATH}"
}

main "$@"
