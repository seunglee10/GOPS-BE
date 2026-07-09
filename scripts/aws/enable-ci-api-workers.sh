#!/usr/bin/env bash
# 역할: CI overlay가 API 이미지 기반 worker replica 수를 의도치 않게 바꾸지 않도록 보정합니다.
set -euo pipefail

KUSTOMIZE_OVERLAY="${KUSTOMIZE_OVERLAY:-infra/k8s/overlays/aws-incluster-app-ci}"
KUSTOMIZATION_FILE="${KUSTOMIZE_OVERLAY}/kustomization.yaml"
K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
SERVICES="${SERVICES:-}"
MARKER="# CI-only API worker replicas"

declare -A DESIRED_REPLICAS=()

if [[ ! -f "${KUSTOMIZATION_FILE}" ]]; then
  echo "kustomization not found: ${KUSTOMIZATION_FILE}" >&2
  exit 1
fi

if grep -Fq "${MARKER}" "${KUSTOMIZATION_FILE}"; then
  echo "CI API worker replica patches already present."
  exit 0
fi

replicas_for() {
  local deployment="$1"

  kubectl get deployment "${deployment}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || printf '0'
}

if [[ " ${SERVICES} " == *" backend "* ]]; then
  DESIRED_REPLICAS[alert-evaluator]=1
  DESIRED_REPLICAS[recommendation-worker]=1
  echo "backend image selected; enabling API image workers in CI overlay."
else
  DESIRED_REPLICAS[alert-evaluator]="$(replicas_for alert-evaluator)"
  DESIRED_REPLICAS[recommendation-worker]="$(replicas_for recommendation-worker)"
  echo "backend image not selected; preserving live API worker replicas in CI overlay."
fi

python3 - "${KUSTOMIZATION_FILE}" "${MARKER}" "${DESIRED_REPLICAS[alert-evaluator]}" "${DESIRED_REPLICAS[recommendation-worker]}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
marker = sys.argv[2]
alert_replicas = sys.argv[3]
recommendation_replicas = sys.argv[4]
text = path.read_text(encoding="utf-8")

insert = f"""  {marker}
  - target:
      kind: Deployment
      name: alert-evaluator
    patch: |-
      - op: replace
        path: /spec/replicas
        value: {alert_replicas}
  - target:
      kind: Deployment
      name: recommendation-worker
    patch: |-
      - op: replace
        path: /spec/replicas
        value: {recommendation_replicas}

"""

needle = "\nimages:\n"
if needle not in text:
    raise SystemExit(f"images section not found in {path}")

path.write_text(text.replace(needle, "\n" + insert + "images:\n", 1), encoding="utf-8")
PY

echo "Synced alert-evaluator=${DESIRED_REPLICAS[alert-evaluator]} and recommendation-worker=${DESIRED_REPLICAS[recommendation-worker]} in ${KUSTOMIZATION_FILE}."
