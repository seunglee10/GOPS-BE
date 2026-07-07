#!/usr/bin/env bash
# 역할: frontend Vite build에 필요한 Logo.dev publishable key만 AWS Secrets Manager에서 읽습니다.
set -euo pipefail

LOGODEV_SECRET_NAME="${LOGODEV_SECRET_NAME:-icon/logodev}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"

if [[ -z "${GITHUB_ENV:-}" ]]; then
  printf 'GITHUB_ENV를 찾을 수 없습니다. GitHub Actions env file이 필요합니다.\n' >&2
  exit 1
fi

secret_file="$(mktemp)"
trap 'rm -f "${secret_file}"' EXIT

if [[ -n "${LOGODEV_SECRET_STRING:-}" ]]; then
  printf '%s' "${LOGODEV_SECRET_STRING}" > "${secret_file}"
else
  aws secretsmanager get-secret-value \
    --region "${AWS_REGION}" \
    --secret-id "${LOGODEV_SECRET_NAME}" \
    --query SecretString \
    --output text > "${secret_file}"
fi

python3 -c '
import json
import os
import sys

secret_path = sys.argv[1]
with open(secret_path, "r", encoding="utf-8") as handle:
    raw_secret = handle.read().strip()

try:
    payload = json.loads(raw_secret)
except json.JSONDecodeError:
    payload = raw_secret

publishable_key = ""
if isinstance(payload, dict):
    for key in (
        "LOGODEV_PUB_KEY",
        "LOGO_DEV_PUBLISHABLE_KEY",
        "VITE_LOGO_DEV_PUBLISHABLE_KEY",
        "publishable_key",
        "publishableKey",
    ):
        value = payload.get(key)
        if value:
            publishable_key = str(value).strip()
            break
else:
    publishable_key = str(payload).strip()

if not publishable_key:
    print("Logo.dev publishable key를 찾을 수 없습니다.", file=sys.stderr)
    raise SystemExit(1)
if not publishable_key.startswith("pk_"):
    print("Logo.dev frontend build에는 pk_ publishable key만 사용할 수 있습니다.", file=sys.stderr)
    raise SystemExit(1)

print(f"::add-mask::{publishable_key}")
with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env_file:
    env_file.write(f"LOGODEV_PUB_KEY={publishable_key}\n")
    env_file.write(f"VITE_LOGO_DEV_PUBLISHABLE_KEY={publishable_key}\n")
' "${secret_file}"

printf 'Loaded Logo.dev publishable key from AWS Secrets Manager secret: %s\n' "${LOGODEV_SECRET_NAME}"
