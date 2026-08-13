#!/usr/bin/env bash
# 역할: AWS/EKS 안에서 한 종목 live market-data path를 read-only로 추적합니다.
# 사용: scripts/aws/check-live-path.sh NVDA
set -euo pipefail

SYMBOL="${1:-NVDA}"
INTERVAL="${INTERVAL:-1m}"
NAMESPACE="${NAMESPACE:-alfaka-market-data}"
TARGET="${TRACE_TARGET:-deploy/alfaka-market-processor}"
API_BASE_URL="${GOPS_API_BASE_URL:-http://gops-backend:8000}"

if [[ ! "${SYMBOL}" =~ ^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$ ]]; then
  echo "Invalid trace symbol: ${SYMBOL}" >&2
  exit 2
fi

EXTRA_ARGS=()
if [[ "${TRACE_REQUIRE_LIVE:-0}" == "1" ]]; then
  EXTRA_ARGS+=(--require-live)
fi
if [[ "${TRACE_STRICT:-0}" == "1" ]]; then
  EXTRA_ARGS+=(--strict)
fi

TRACE_CMD=(
  python -m market_data.tools.live_path_trace "${SYMBOL}"
  --interval "${INTERVAL}"
  --api-base-url "${API_BASE_URL}"
  --json
)
if (( ${#EXTRA_ARGS[@]} )); then
  TRACE_CMD+=("${EXTRA_ARGS[@]}")
fi

kubectl exec -n "${NAMESPACE}" "${TARGET}" -- "${TRACE_CMD[@]}"
