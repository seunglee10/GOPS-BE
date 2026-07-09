#!/usr/bin/env bash
# 역할: EKS PVC 뒤의 EBS volume snapshot 계획을 출력하고, --execute 때만 snapshot을 생성합니다.
# 주의: stateful pod를 quiesce한 뒤 실행해야 DB별 일관성을 기대할 수 있습니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.local-artifacts/ebs-snapshots/${RUN_ID}}"
EXECUTE=false
PVC_NAMES=()

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --namespace NAME    Kubernetes namespace. Default: ${NAMESPACE}
  --output-dir PATH   Directory for snapshot JSON outputs. Default: ${OUTPUT_DIR}
  --pvc NAME          PVC to snapshot. May be repeated. Defaults to platform PVCs.
  --execute           Actually call aws ec2 create-snapshot. Without this, print the plan only.
  -h, --help          Show this help.

Environment:
  NAMESPACE           Same as --namespace.
  OUTPUT_DIR          Same as --output-dir.
  RUN_ID              Snapshot run id used in tags and default output path.
  AWS_REGION          Optional AWS region passed to aws CLI.
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
    --pvc)
      PVC_NAMES+=("$2")
      shift 2
      ;;
    --execute)
      EXECUTE=true
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

volume_id_for_pvc() {
  local pvc="$1"
  local pv
  local handle

  pv="$(kubectl get pvc "${pvc}" -n "${NAMESPACE}" -o jsonpath='{.spec.volumeName}')"
  if [ -z "${pv}" ]; then
    echo "PVC ${pvc} is not bound to a PV." >&2
    return 1
  fi

  handle="$(kubectl get pv "${pv}" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)"
  if [ -z "${handle}" ]; then
    handle="$(kubectl get pv "${pv}" -o jsonpath='{.spec.awsElasticBlockStore.volumeID}' 2>/dev/null || true)"
  fi
  if [ -z "${handle}" ]; then
    echo "PV ${pv} does not expose an EBS volume handle." >&2
    return 1
  fi

  printf '%s\n' "${handle##*/}"
}

create_snapshot() {
  local pvc="$1"
  local volume_id="$2"
  local output_file="${OUTPUT_DIR}/${pvc}-snapshot.json"
  local description="gops data-preserving rebuild ${RUN_ID} ${NAMESPACE}/${pvc}"
  local tag_spec

  tag_spec="ResourceType=snapshot,Tags=[{Key=Name,Value=gops-${RUN_ID}-${pvc}},{Key=Project,Value=gops},{Key=Purpose,Value=data-preserving-rebuild},{Key=Namespace,Value=${NAMESPACE}},{Key=PVC,Value=${pvc}},{Key=RunId,Value=${RUN_ID}}]"

  if [ "${EXECUTE}" != "true" ]; then
    printf 'plan\t%s\t%s\n' "${pvc}" "${volume_id}" | tee -a "${OUTPUT_DIR}/snapshot-plan.tsv"
    return
  fi

  echo "snapshot: ${pvc} volume=${volume_id}"
  if [ -n "${AWS_REGION:-}" ]; then
    aws ec2 create-snapshot \
      --region "${AWS_REGION}" \
      --volume-id "${volume_id}" \
      --description "${description}" \
      --tag-specifications "${tag_spec}" \
      --output json | tee "${output_file}"
  else
    aws ec2 create-snapshot \
      --volume-id "${volume_id}" \
      --description "${description}" \
      --tag-specifications "${tag_spec}" \
      --output json | tee "${output_file}"
  fi
}

main() {
  require_command kubectl
  if [ "${EXECUTE}" = "true" ]; then
    require_command aws
  fi

  if [ "${#PVC_NAMES[@]}" -eq 0 ]; then
    PVC_NAMES=(
      clickhouse-data-clickhouse-0
      kafka-data-kafka-0
      graphdb-data-graphdb-0
      redis-data-redis-0
      postgres-data-postgres-0
    )
  fi

  mkdir -p "${OUTPUT_DIR}"
  : > "${OUTPUT_DIR}/snapshot-plan.tsv"
  echo "run_id=${RUN_ID}" > "${OUTPUT_DIR}/METADATA.env"
  echo "namespace=${NAMESPACE}" >> "${OUTPUT_DIR}/METADATA.env"
  echo "execute=${EXECUTE}" >> "${OUTPUT_DIR}/METADATA.env"

  if [ "${EXECUTE}" != "true" ]; then
    echo "dry-run: no AWS snapshots will be created. Pass --execute after approval."
  fi

  local pvc
  for pvc in "${PVC_NAMES[@]}"; do
    volume_id="$(volume_id_for_pvc "${pvc}")"
    create_snapshot "${pvc}" "${volume_id}"
  done

  echo "done: outputs written to ${OUTPUT_DIR}"
}

main "$@"
