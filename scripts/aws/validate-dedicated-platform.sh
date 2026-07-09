#!/usr/bin/env bash
# 역할: 전용 NodePool 재구성 이후 app 배포 전에 platform 상태와 배치를 검증합니다.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"

required_nodepools=(app-agent cache-db streaming graphdb clickhouse batch)
statefulsets=(clickhouse kafka postgres redis graphdb)

expected_pool_for_statefulset() {
  case "$1" in
    clickhouse) echo "clickhouse" ;;
    kafka) echo "streaming" ;;
    postgres | redis) echo "cache-db" ;;
    graphdb) echo "graphdb" ;;
    *)
      echo "unknown" >&2
      return 1
      ;;
  esac
}

echo "validate: required NodePools"
for nodepool in "${required_nodepools[@]}"; do
  kubectl get nodepool "${nodepool}" >/dev/null
  echo "  ok nodepool/${nodepool}"
done

echo "validate: app-agent has Ready nodes"
ready_app_agent_nodes=()
while IFS= read -r node; do
  if [[ -z "${node}" ]]; then
    continue
  fi
  ready_status="$(kubectl get node "${node}" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}')"
  if [[ "${ready_status}" == "True" ]]; then
    ready_app_agent_nodes+=("${node}")
  fi
done < <(kubectl get nodes -l karpenter.sh/nodepool=app-agent -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

if [[ "${#ready_app_agent_nodes[@]}" -eq 0 ]]; then
  echo "app-agent NodePool has no Ready nodes" >&2
  exit 1
fi
printf '%s\n' "${ready_app_agent_nodes[@]}" | sed 's/^/  ok node /'

echo "validate: stateful services ready and placed"
for statefulset in "${statefulsets[@]}"; do
  replicas="$(kubectl get statefulset "${statefulset}" -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.replicas}')"
  ready="$(kubectl get statefulset "${statefulset}" -n "${K8S_NAMESPACE}" -o jsonpath='{.status.readyReplicas}')"
  replicas="${replicas:-0}"
  ready="${ready:-0}"
  if [[ "${replicas}" != "${ready}" ]]; then
    echo "statefulset/${statefulset} is not ready: ready=${ready}, desired=${replicas}" >&2
    exit 1
  fi

  if [[ "${replicas}" == "0" ]]; then
    echo "  ok statefulset/${statefulset} replicas=0"
    continue
  fi

  pod="${statefulset}-0"
  node="$(kubectl get pod "${pod}" -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.nodeName}')"
  actual_pool="$(kubectl get node "${node}" -o jsonpath='{.metadata.labels.karpenter\.sh/nodepool}')"
  expected_pool="$(expected_pool_for_statefulset "${statefulset}")"
  if [[ "${actual_pool}" != "${expected_pool}" ]]; then
    echo "pod/${pod} is on ${actual_pool}, expected ${expected_pool}" >&2
    exit 1
  fi
  echo "  ok pod/${pod} -> ${actual_pool}"
done
