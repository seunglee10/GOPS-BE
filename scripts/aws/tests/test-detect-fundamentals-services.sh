#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT_SCRIPT="${SCRIPT_DIR}/../detect-changed-services.sh"
TEMP_REPO="$(mktemp -d)"
trap 'rm -rf "${TEMP_REPO}"' EXIT

git -C "${TEMP_REPO}" init -q
git -C "${TEMP_REPO}" config user.email "codex-test@gops.local"
git -C "${TEMP_REPO}" config user.name "GOPS fundamentals test"
mkdir -p "${TEMP_REPO}/systems/fundamentals/shared/fundamentals"
mkdir -p "${TEMP_REPO}/infra/docker"
printf 'FROM scratch\n' > "${TEMP_REPO}/infra/docker/Dockerfile.gops-market-storage"
printf 'TABLE_VERSION = 1\n' > "${TEMP_REPO}/systems/fundamentals/shared/fundamentals/schema.py"
git -C "${TEMP_REPO}" add .
git -C "${TEMP_REPO}" commit -qm base
BASE_SHA="$(git -C "${TEMP_REPO}" rev-parse HEAD)"
printf 'TABLE_VERSION = 2\n' > "${TEMP_REPO}/systems/fundamentals/shared/fundamentals/schema.py"
git -C "${TEMP_REPO}" add .
git -C "${TEMP_REPO}" commit -qm changed
HEAD_SHA="$(git -C "${TEMP_REPO}" rev-parse HEAD)"

OUTPUT="$(cd "${TEMP_REPO}" && BASE_SHA="${BASE_SHA}" HEAD_SHA="${HEAD_SHA}" EVENT_NAME=push bash "${DETECT_SCRIPT}")"
SERVICES="$(printf '%s\n' "${OUTPUT}" | sed -n 's/^services=//p')"

case " ${SERVICES} " in
  *" market-storage "*) ;;
  *) printf 'market-storage was not selected: %s\n' "${SERVICES}" >&2; exit 1 ;;
esac

printf 'fundamentals service detection passed: %s\n' "${SERVICES}"
