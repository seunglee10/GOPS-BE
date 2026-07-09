#!/usr/bin/env bash
# 역할: 데이터 보존형 rebuild 전에 app/worker Deployment를 0으로 내리고 CronJob을 중지합니다.
# 주의: 기본은 plan만 출력합니다. --execute 없이는 클러스터를 변경하지 않습니다.
set -euo pipefail

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
EXECUTE=false
DELETE_ACTIVE_JOBS=false

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --namespace NAME       Kubernetes namespace. Default: ${NAMESPACE}
  --execute              Actually scale Deployments to 0 and suspend CronJobs.
  --delete-active-jobs   With --execute, delete active Jobs after listing them.
  -h, --help             Show this help.

Environment:
  NAMESPACE              Same as --namespace.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --execute)
      EXECUTE=true
      shift
      ;;
    --delete-active-jobs)
      DELETE_ACTIVE_JOBS=true
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

print_plan() {
  echo "namespace: ${NAMESPACE}"
  echo
  echo "Deployments to scale to 0:"
  kubectl get deployment -n "${NAMESPACE}" -o name
  echo
  echo "CronJobs to suspend:"
  kubectl get cronjob -n "${NAMESPACE}" -o name 2>/dev/null || true
  echo
  echo "Active Jobs to review:"
  kubectl get job -n "${NAMESPACE}" \
    --field-selector status.successful!=1 \
    -o wide 2>/dev/null || true
}

main() {
  require_command kubectl
  kubectl get namespace "${NAMESPACE}" >/dev/null

  print_plan

  if [ "${EXECUTE}" != "true" ]; then
    echo
    echo "dry-run: no workload was changed. Re-run with --execute after approval."
    return
  fi

  echo
  echo "scale: all deployments -> 0"
  kubectl scale deployment --all -n "${NAMESPACE}" --replicas=0

  if kubectl get cronjob -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "suspend: all cronjobs"
    while IFS= read -r cronjob_name; do
      [ -n "${cronjob_name}" ] || continue
      kubectl patch "${cronjob_name}" -n "${NAMESPACE}" -p '{"spec":{"suspend":true}}'
    done < <(kubectl get cronjob -n "${NAMESPACE}" -o name)
  fi

  if [ "${DELETE_ACTIVE_JOBS}" = "true" ]; then
    echo "delete: active jobs"
    kubectl delete job -n "${NAMESPACE}" \
      --field-selector status.successful!=1 \
      --ignore-not-found=true
  else
    echo "skip: active Jobs were not deleted. Pass --delete-active-jobs only after review."
  fi
}

main "$@"
