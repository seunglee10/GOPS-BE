#!/usr/bin/env bash
# Restore the local Nasdaq FIBO GraphDB volume archive into the EKS GraphDB PVC.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
STATEFULSET_NAME="${STATEFULSET_NAME:-graphdb}"
PVC_NAME="${PVC_NAME:-graphdb-data-graphdb-0}"
RESTORE_POD_NAME="${RESTORE_POD_NAME:-graphdb-volume-restore}"
ARTIFACT_PATH="${GRAPHDB_VOLUME_TGZ:-${REPO_ROOT}/.local-artifacts/graphdb/graphdb-volume.tgz}"
STORAGE_CLASS_NAME="${STORAGE_CLASS_NAME:-eks-auto-ebs}"
STORAGE_SIZE="${STORAGE_SIZE:-10Gi}"
RESTORE_IMAGE="${RESTORE_IMAGE:-alpine:3.20}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300s}"
REPLACE_PENDING_PVC=false
FORCE_RESTORE=false
START_GRAPHDB=true

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --artifact PATH          GraphDB volume archive path.
  --namespace NAME         Kubernetes namespace. Default: ${NAMESPACE}
  --replace-pending-pvc    Delete and recreate the existing Pending PVC when it has no storageClassName.
  --force                  Clear a non-empty target PVC before restoring.
  --no-start               Leave the graphdb StatefulSet scaled to 0 after restore.
  -h, --help               Show this help.

Environment:
  GRAPHDB_VOLUME_TGZ       Same as --artifact.
  STORAGE_CLASS_NAME       PVC storage class. Default: ${STORAGE_CLASS_NAME}
  STORAGE_SIZE             PVC size. Default: ${STORAGE_SIZE}
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
    --replace-pending-pvc)
      REPLACE_PENDING_PVC=true
      shift
      ;;
    --force)
      FORCE_RESTORE=true
      shift
      ;;
    --no-start)
      START_GRAPHDB=false
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

jsonpath() {
  kubectl get "$1" "$2" -n "${NAMESPACE}" -o "jsonpath=$3" 2>/dev/null || true
}

prepare_statefulset() {
  if ! kubectl get statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    return
  fi

  live_storage_class="$(jsonpath statefulset "${STATEFULSET_NAME}" '{.spec.volumeClaimTemplates[0].spec.storageClassName}')"
  if [ -n "${live_storage_class}" ]; then
    return
  fi

  if [ "${REPLACE_PENDING_PVC}" != "true" ]; then
    echo "StatefulSet ${STATEFULSET_NAME} has no volumeClaimTemplates storageClassName." >&2
    echo "Re-run with --replace-pending-pvc to recreate the broken initial StatefulSet/PVC pair." >&2
    exit 1
  fi

  echo "delete: statefulset/${STATEFULSET_NAME} to allow immutable volumeClaimTemplates update"
  kubectl delete statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" --wait=true
  kubectl wait --for=delete "pod/${STATEFULSET_NAME}-0" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}" >/dev/null 2>&1 || true
}

ensure_pvc() {
  pvc_phase="$(jsonpath pvc "${PVC_NAME}" '{.status.phase}')"
  pvc_storage_class="$(jsonpath pvc "${PVC_NAME}" '{.spec.storageClassName}')"

  if [ "${pvc_phase}" = "Pending" ] && [ -z "${pvc_storage_class}" ]; then
    if [ "${REPLACE_PENDING_PVC}" != "true" ]; then
      echo "PVC ${PVC_NAME} is Pending and has no storageClassName." >&2
      echo "Re-run with --replace-pending-pvc after confirming it has no data to preserve." >&2
      exit 1
    fi
    echo "replace: deleting Pending PVC ${PVC_NAME}"
    kubectl delete pvc "${PVC_NAME}" -n "${NAMESPACE}" --wait=true
    pvc_phase=""
  fi

  if [ -z "${pvc_phase}" ]; then
    echo "create: pvc/${PVC_NAME}"
    kubectl apply -f - <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: graphdb
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${STORAGE_CLASS_NAME}
  resources:
    requests:
      storage: ${STORAGE_SIZE}
YAML
  fi
}

scale_graphdb_down() {
  if kubectl get statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "scale: statefulset/${STATEFULSET_NAME} -> 0"
    kubectl scale statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" --replicas=0 >/dev/null
    kubectl wait --for=delete "pod/${STATEFULSET_NAME}-0" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}" >/dev/null 2>&1 || true
  fi
}

scale_graphdb_up() {
  if [ "${START_GRAPHDB}" != "true" ]; then
    echo "skip: graphdb StatefulSet left at 0 replicas"
    return
  fi
  if ! kubectl get statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "apply: infra/k8s/base/app/statefulset-graphdb.yaml"
    kubectl apply -f "${REPO_ROOT}/infra/k8s/base/app/statefulset-graphdb.yaml"
  fi
  echo "scale: statefulset/${STATEFULSET_NAME} -> 1"
  kubectl scale statefulset "${STATEFULSET_NAME}" -n "${NAMESPACE}" --replicas=1 >/dev/null
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
    app: graphdb-volume-restore
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
    karpenter.sh/nodepool: platform-core
  tolerations:
    - key: gops.io/dedicated
      operator: Equal
      value: platform-core
      effect: NoSchedule
  containers:
    - name: restore
      image: ${RESTORE_IMAGE}
      command:
        - sh
        - -c
        - sleep 3600
      volumeMounts:
        - name: graphdb-data
          mountPath: /volume
  volumes:
    - name: graphdb-data
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
YAML
  kubectl wait --for=condition=Ready "pod/${RESTORE_POD_NAME}" -n "${NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

restore_archive() {
  if kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'test -n "$(find /volume -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)"'; then
    if [ "${FORCE_RESTORE}" != "true" ]; then
      echo "PVC ${PVC_NAME} is not empty. Re-run with --force to clear it before restore." >&2
      exit 1
    fi
    echo "clear: /volume"
    kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- sh -c 'find /volume -mindepth 1 -maxdepth 1 ! -name lost+found -exec rm -rf {} +'
  fi

  echo "copy: ${ARTIFACT_PATH} -> pod/${RESTORE_POD_NAME}:/tmp/graphdb-volume.tgz"
  kubectl cp "${ARTIFACT_PATH}" "${NAMESPACE}/${RESTORE_POD_NAME}:/tmp/graphdb-volume.tgz"
  echo "extract: /tmp/graphdb-volume.tgz -> /volume"
  kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- tar xzf /tmp/graphdb-volume.tgz -C /volume
  kubectl exec "${RESTORE_POD_NAME}" -n "${NAMESPACE}" -- test -f /volume/data/repositories/nasdaq-fibo/config.ttl
}

main() {
  require_command kubectl
  require_command shasum

  if [ ! -f "${ARTIFACT_PATH}" ]; then
    echo "GraphDB volume archive not found: ${ARTIFACT_PATH}" >&2
    echo "Place graphdb-volume.tgz under .local-artifacts/graphdb/ or pass --artifact PATH." >&2
    exit 1
  fi

  verify_checksum_if_available
  trap cleanup_restore_pod EXIT INT TERM
  kubectl get namespace "${NAMESPACE}" >/dev/null
  prepare_statefulset
  scale_graphdb_down
  ensure_pvc
  create_restore_pod
  restore_archive
  cleanup_restore_pod
  scale_graphdb_up
  echo "done: GraphDB PVC restored from ${ARTIFACT_PATH}"
}

main "$@"
