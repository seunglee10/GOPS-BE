#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT_SCRIPT="${SCRIPT_DIR}/../detect-changed-services.sh"
TEMP_REPO="$(mktemp -d)"
trap 'rm -rf "${TEMP_REPO}"' EXIT

git -C "${TEMP_REPO}" init -q
git -C "${TEMP_REPO}" config user.email "codex-test@gops.local"
git -C "${TEMP_REPO}" config user.name "GOPS contract test"
mkdir -p "${TEMP_REPO}/shared/chart-contract"
mkdir -p "${TEMP_REPO}/infra/docker"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-frontend"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-agent-orchestrator"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-order-worker"
printf '%s\n' '{"version":1}' > "${TEMP_REPO}/shared/chart-contract/chart-explanation.schema.json"
git -C "${TEMP_REPO}" add shared/chart-contract/chart-explanation.schema.json infra/docker
git -C "${TEMP_REPO}" commit -qm base
BASE_SHA="$(git -C "${TEMP_REPO}" rev-parse HEAD)"
printf '%s\n' '{"version":2}' > "${TEMP_REPO}/shared/chart-contract/chart-explanation.schema.json"
git -C "${TEMP_REPO}" add shared/chart-contract/chart-explanation.schema.json
git -C "${TEMP_REPO}" commit -qm changed
HEAD_SHA="$(git -C "${TEMP_REPO}" rev-parse HEAD)"

OUTPUT="$(cd "${TEMP_REPO}" && BASE_SHA="${BASE_SHA}" HEAD_SHA="${HEAD_SHA}" EVENT_NAME=push bash "${DETECT_SCRIPT}")"
SERVICES="$(printf '%s\n' "${OUTPUT}" | sed -n 's/^services=//p')"

case " ${SERVICES} " in
  *" frontend "*) ;;
  *) printf 'frontend was not selected: %s\n' "${SERVICES}" >&2; exit 1 ;;
esac
case " ${SERVICES} " in
  *" agent-orchestrator "*) ;;
  *) printf 'agent-orchestrator was not selected: %s\n' "${SERVICES}" >&2; exit 1 ;;
esac

printf 'chart contract service detection passed: %s\n' "${SERVICES}"
