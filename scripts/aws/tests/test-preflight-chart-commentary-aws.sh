#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT_SCRIPT="${SCRIPT_DIR}/../preflight-chart-commentary-aws.sh"
LOCAL_DEPLOY_SCRIPT="${SCRIPT_DIR}/../deploy-dev-local.sh"
WORKFLOW_FILE="${SCRIPT_DIR}/../../../.github/workflows/deploy-dev.yml"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TEMP_DIR}"' EXIT

cat > "${TEMP_DIR}/kubectl" <<'SH'
#!/usr/bin/env bash
case "$*" in
  "get deployment/chart-asset-builder -n alfaka-market-data -o jsonpath={.spec.template.spec.containers[0].image}")
    printf 'registry/gops-agent-orchestrator:writer-test'
    ;;
  "get configmap/gops-dev-deploy-state -n alfaka-market-data -o "*)
    printf 'sha-test\tnormal'
    ;;
  "exec -i deployment/chart-asset-builder -n alfaka-market-data -c chart-asset-builder -- python -u -")
    cat >/dev/null
    printf '{"keyConfigured":true,"model":"gpt-test","promptVersion":"%s","provider":"openai","required":true}\n' \
      "${FAKE_PROMPT_VERSION:-chart-commentary.ko.v5}"
    ;;
  *)
    printf 'unexpected kubectl call: %s\n' "$*" >&2
    exit 1
    ;;
esac
SH
chmod +x "${TEMP_DIR}/kubectl"

OUTPUT="$(
  PATH="${TEMP_DIR}:${PATH}" \
  EXPECTED_IMAGE_TAG=writer-test \
  EXPECTED_PROMPT_VERSION=chart-commentary.ko.v5 \
  bash "${PREFLIGHT_SCRIPT}"
)"

printf '%s\n' "${OUTPUT}" | grep -q '^Chart commentary preflight passed\.$'
printf '%s\n' "${OUTPUT}" | grep -q '^builderImage=registry/gops-agent-orchestrator:writer-test$'
printf '%s\n' "${OUTPUT}" | grep -q '"keyConfigured":true'
if printf '%s\n' "${OUTPUT}" | grep -Eq 'OPENAI_API_KEY|sk-'; then
  printf 'preflight leaked a credential field\n' >&2
  exit 1
fi

if PATH="${TEMP_DIR}:${PATH}" \
  EXPECTED_IMAGE_TAG=stale-writer \
  EXPECTED_PROMPT_VERSION=chart-commentary.ko.v5 \
  bash "${PREFLIGHT_SCRIPT}" >/dev/null 2>&1; then
  printf 'preflight accepted a mismatched writer image\n' >&2
  exit 1
fi

if PATH="${TEMP_DIR}:${PATH}" \
  FAKE_PROMPT_VERSION=chart-commentary.ko.v4 \
  EXPECTED_IMAGE_TAG=writer-test \
  EXPECTED_PROMPT_VERSION=chart-commentary.ko.v5 \
  bash "${PREFLIGHT_SCRIPT}" >/dev/null 2>&1; then
  printf 'preflight accepted a mismatched prompt version\n' >&2
  exit 1
fi

local_rollout_line="$(grep -n '^  deploy_app_workloads$' "${LOCAL_DEPLOY_SCRIPT}" | cut -d: -f1)"
local_preflight_line="$(grep -n '^  verify_chart_commentary_writer$' "${LOCAL_DEPLOY_SCRIPT}" | cut -d: -f1)"
local_smoke_line="$(grep -n '^  run_smoke_tests$' "${LOCAL_DEPLOY_SCRIPT}" | cut -d: -f1)"
if [[ -z "${local_rollout_line}" || -z "${local_preflight_line}" || -z "${local_smoke_line}" \
  || "${local_rollout_line}" -ge "${local_preflight_line}" \
  || "${local_preflight_line}" -ge "${local_smoke_line}" ]]; then
  printf 'local deploy does not gate success on commentary preflight after rollout\n' >&2
  exit 1
fi
grep -q 'EXPECTED_PROMPT_VERSION="chart-commentary.ko.v5"' "${LOCAL_DEPLOY_SCRIPT}"

workflow_rollout_line="$(grep -n '^- name: Verify rollout$\|^      - name: Verify rollout$' "${WORKFLOW_FILE}" | head -n 1 | cut -d: -f1)"
workflow_preflight_line="$(grep -n '^- name: Verify chart commentary writer$\|^      - name: Verify chart commentary writer$' "${WORKFLOW_FILE}" | head -n 1 | cut -d: -f1)"
workflow_smoke_line="$(grep -n '^- name: Smoke test public endpoints$\|^      - name: Smoke test public endpoints$' "${WORKFLOW_FILE}" | head -n 1 | cut -d: -f1)"
if [[ -z "${workflow_rollout_line}" || -z "${workflow_preflight_line}" || -z "${workflow_smoke_line}" \
  || "${workflow_rollout_line}" -ge "${workflow_preflight_line}" \
  || "${workflow_preflight_line}" -ge "${workflow_smoke_line}" ]]; then
  printf 'GitHub deploy does not gate success on commentary preflight after rollout\n' >&2
  exit 1
fi
grep -q 'EXPECTED_PROMPT_VERSION: chart-commentary.ko.v5' "${WORKFLOW_FILE}"

printf 'chart commentary AWS preflight contract passed\n'
