#!/usr/bin/env bash
# 역할: rollout 실패 시 GitHub Actions 로그에 원인 파악용 Kubernetes 정보를 남깁니다.
set -euo pipefail

DEPLOYMENT="${1:?deployment 이름을 넣어주세요.}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"

echo "::group::deployment/${DEPLOYMENT}"
kubectl get deployment "${DEPLOYMENT}" -n "${K8S_NAMESPACE}" -o wide || true
kubectl describe deployment "${DEPLOYMENT}" -n "${K8S_NAMESPACE}" || true
echo "::endgroup::"

echo "::group::pods for ${DEPLOYMENT}"
app_label="$(kubectl get deployment "${DEPLOYMENT}" \
  -n "${K8S_NAMESPACE}" \
  -o jsonpath='{.spec.selector.matchLabels.app}' 2>/dev/null || true)"
selector=""
if [[ -n "${app_label}" ]]; then
  selector="app=${app_label}"
fi
if [[ -n "${selector}" ]]; then
  kubectl get pods -n "${K8S_NAMESPACE}" -l "${selector}" -o wide || true
  kubectl describe pods -n "${K8S_NAMESPACE}" -l "${selector}" || true
else
  echo "selector를 찾지 못했습니다."
fi
echo "::endgroup::"

echo "::group::recent namespace events"
kubectl get events -n "${K8S_NAMESPACE}" --sort-by=.lastTimestamp | tail -n 80 || true
echo "::endgroup::"

if [[ -n "${selector}" ]]; then
  echo "::group::recent pod logs"
  while IFS= read -r pod; do
    if [[ -z "${pod}" ]]; then
      continue
    fi
    echo "### ${pod}"
    kubectl logs "${pod}" -n "${K8S_NAMESPACE}" --all-containers --tail=120 || true
  done < <(kubectl get pods -n "${K8S_NAMESPACE}" -l "${selector}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
  echo "::endgroup::"
fi
