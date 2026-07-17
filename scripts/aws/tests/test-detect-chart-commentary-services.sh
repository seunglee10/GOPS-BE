#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT_SCRIPT="${SCRIPT_DIR}/../detect-changed-services.sh"
TEMP_REPO="$(mktemp -d)"
trap 'rm -rf "${TEMP_REPO}"' EXIT

git -C "${TEMP_REPO}" init -q
git -C "${TEMP_REPO}" config user.email "codex-test@gops.local"
git -C "${TEMP_REPO}" config user.name "GOPS contract test"
mkdir -p "${TEMP_REPO}/systems/agent-orchestration/shared/gops_agents/chart_assets"
mkdir -p "${TEMP_REPO}/infra/docker"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-agent-orchestrator"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-backend"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-order-worker"
printf 'COMMENTARY_PROMPT_VERSION = "chart-commentary.ko.v1"\n' \
  > "${TEMP_REPO}/systems/agent-orchestration/shared/gops_agents/chart_assets/commentary.py"
git -C "${TEMP_REPO}" add .
git -C "${TEMP_REPO}" commit -qm base
BASE_SHA="$(git -C "${TEMP_REPO}" rev-parse HEAD)"
printf 'COMMENTARY_PROMPT_VERSION = "chart-commentary.ko.v2"\n' \
  > "${TEMP_REPO}/systems/agent-orchestration/shared/gops_agents/chart_assets/commentary.py"
git -C "${TEMP_REPO}" add .
git -C "${TEMP_REPO}" commit -qm changed
HEAD_SHA="$(git -C "${TEMP_REPO}" rev-parse HEAD)"

OUTPUT="$(cd "${TEMP_REPO}" && BASE_SHA="${BASE_SHA}" HEAD_SHA="${HEAD_SHA}" EVENT_NAME=push bash "${DETECT_SCRIPT}")"
SERVICES="$(printf '%s\n' "${OUTPUT}" | sed -n 's/^services=//p')"
DEPLOYMENTS="$(printf '%s\n' "${OUTPUT}" | sed -n 's/^deployments=//p')"

case " ${SERVICES} " in
  *" agent-orchestrator "*) ;;
  *) printf 'agent-orchestrator was not selected: %s\n' "${SERVICES}" >&2; exit 1 ;;
esac
case " ${DEPLOYMENTS} " in
  *" chart-asset-builder "*) ;;
  *) printf 'chart-asset-builder was not selected: %s\n' "${DEPLOYMENTS}" >&2; exit 1 ;;
esac

printf 'chart commentary writer deployment scope passed: %s\n' "${DEPLOYMENTS}"
