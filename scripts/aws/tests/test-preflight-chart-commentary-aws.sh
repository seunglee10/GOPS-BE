#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT_SCRIPT="${SCRIPT_DIR}/../preflight-chart-commentary-aws.sh"
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
    printf '%s\n' '{"keyConfigured":true,"model":"gpt-test","promptVersion":"chart-commentary.ko.v2","provider":"openai","required":true}'
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
  EXPECTED_PROMPT_VERSION=chart-commentary.ko.v2 \
  bash "${PREFLIGHT_SCRIPT}"
)"

printf '%s\n' "${OUTPUT}" | grep -q '^Chart commentary preflight passed\.$'
printf '%s\n' "${OUTPUT}" | grep -q '^builderImage=registry/gops-agent-orchestrator:writer-test$'
printf '%s\n' "${OUTPUT}" | grep -q '"keyConfigured":true'
if printf '%s\n' "${OUTPUT}" | grep -Eq 'OPENAI_API_KEY|sk-'; then
  printf 'preflight leaked a credential field\n' >&2
  exit 1
fi

printf 'chart commentary AWS preflight contract passed\n'
