#!/usr/bin/env bash
# Read-only preflight for the deployed chart commentary writer.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
DEPLOYMENT="${CHART_COMMENTARY_DEPLOYMENT:-chart-asset-builder}"
CONTAINER="${CHART_COMMENTARY_CONTAINER:-chart-asset-builder}"
DEPLOY_STATE_CONFIGMAP="${DEPLOY_STATE_CONFIGMAP:-gops-dev-deploy-state}"
EXPECTED_IMAGE_TAG="${EXPECTED_IMAGE_TAG:-}"
EXPECTED_PROMPT_VERSION="${EXPECTED_PROMPT_VERSION:-}"

image="$(
  kubectl get "deployment/${DEPLOYMENT}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}'
)"
if [[ -z "${image}" ]]; then
  printf 'Chart commentary preflight failed: deployment/%s has no image.\n' "${DEPLOYMENT}" >&2
  exit 1
fi
if [[ -n "${EXPECTED_IMAGE_TAG}" && "${image}" != *":${EXPECTED_IMAGE_TAG}" ]]; then
  printf 'Chart commentary preflight failed: builder image is %s, expected tag %s.\n' \
    "${image}" "${EXPECTED_IMAGE_TAG}" >&2
  exit 1
fi

deploy_state="$(
  kubectl get "configmap/${DEPLOY_STATE_CONFIGMAP}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.data.service\.agent-orchestrator\.lastSuccessfulSha}{"\t"}{.data.service\.agent-orchestrator\.lastSuccessfulMode}' \
    2>/dev/null || true
)"

runtime="$(
  kubectl exec -i "deployment/${DEPLOYMENT}" \
    -n "${K8S_NAMESPACE}" \
    -c "${CONTAINER}" \
    -- python -u - <<'PY'
import json
import os

from gops_agents.chart_assets.commentary import COMMENTARY_PROMPT_VERSION, OpenAIChartCommentaryWriter

OpenAIChartCommentaryWriter().validate_configuration()

payload = {
    "provider": os.getenv("CHART_COMMENTARY_PROVIDER", "disabled").strip().lower(),
    "required": os.getenv("CHART_COMMENTARY_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"},
    "keyConfigured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    "model": (os.getenv("CHART_COMMENTARY_MODEL") or os.getenv("OPENAI_MODEL") or "").strip(),
    "promptVersion": COMMENTARY_PROMPT_VERSION,
}
print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
if payload["provider"] != "openai" or payload["required"] is not True or not payload["keyConfigured"] or not payload["model"]:
    raise SystemExit(1)
PY
)"

if [[ -n "${EXPECTED_PROMPT_VERSION}" && "${runtime}" != *"\"promptVersion\":\"${EXPECTED_PROMPT_VERSION}\""* ]]; then
  printf 'Chart commentary preflight failed: runtime prompt version mismatch: %s\n' "${runtime}" >&2
  exit 1
fi

printf 'Chart commentary preflight passed.\n'
printf 'builderImage=%s\n' "${image}"
printf 'deployState=%s\n' "${deploy_state:-unknown}"
printf 'runtime=%s\n' "${runtime}"
