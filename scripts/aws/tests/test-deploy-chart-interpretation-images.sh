#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="${SCRIPT_DIR}/../deploy-chart-interpretation-images.sh"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TEMP_DIR}"' EXIT

cat > "${TEMP_DIR}/kubectl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${KUBECTL_LOG}"
SH
chmod +x "${TEMP_DIR}/kubectl"

KUBECTL_LOG="${TEMP_DIR}/kubectl.log" \
PATH="${TEMP_DIR}:${PATH}" \
AWS_ACCOUNT_ID=123456789012 \
AWS_REGION=ap-northeast-2 \
PROJECT_NAME=alfaka \
ENVIRONMENT=dev \
IMAGE_TAG=chart-readonly-test \
DRY_RUN=true \
bash "${DEPLOY_SCRIPT}"

# gops-frontend Deployment는 gops-frontend 저장소가 롤아웃하므로 여기서는 건드리지 않는다.
test "$(wc -l < "${TEMP_DIR}/kubectl.log" | tr -d ' ')" = "2"
grep -q '^set image deployment/agent-analysis-worker agent-analysis-worker=.*gops-agent-orchestrator:chart-readonly-test ' "${TEMP_DIR}/kubectl.log"
grep -q '^set image deployment/agent-orchestrator agent-orchestrator=.*gops-agent-orchestrator:chart-readonly-test ' "${TEMP_DIR}/kubectl.log"
grep -q -- '--dry-run=server' "${TEMP_DIR}/kubectl.log"

if grep -Eq 'gops-frontend|chart-asset-builder|chart-geometry-build|migration|cronjob| apply ' "${TEMP_DIR}/kubectl.log"; then
  printf 'read-only chart deploy touched a forbidden workload:\n' >&2
  cat "${TEMP_DIR}/kubectl.log" >&2
  exit 1
fi

printf 'chart interpretation image scope passed\n'
