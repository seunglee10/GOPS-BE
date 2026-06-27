#!/usr/bin/env bash
# 역할: missing chart data -> explicit backfill -> S3 processed/final -> ClickHouse -> REST ready 경로를 검증합니다.
# 사용: bash scripts/local/smoke-backfill-missing-data.sh INTC
set -euo pipefail

SYMBOL="${1:-INTC}"
if [[ ! "${SYMBOL}" =~ ^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$ ]]; then
  echo "Invalid smoke symbol: ${SYMBOL}" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OVERRIDE_FILE="$(mktemp "${TMPDIR:-/tmp}/alfaka-backfill-smoke-compose.XXXXXX.yml")"
COMPOSE=(docker compose -f "${ROOT_DIR}/docker-compose.yml" -f "${OVERRIDE_FILE}")

cleanup() {
  local status=$?
  rm -f "${OVERRIDE_FILE}"
  exit "${status}"
}
trap cleanup EXIT

cat > "${OVERRIDE_FILE}" <<'YAML'
services:
  gops-backend:
    environment:
      BACKFILL_EXECUTION_MODE: "sample-dev"
      BACKFILL_ALLOW_REQUESTED_MODE: "false"
      S3_RAW_PREFIX: "market-data/smoke/raw/alpaca"
      S3_FINAL_PREFIX: "market-data/smoke/final"
      S3_PROCESSED_FORMAT: "jsonl"
YAML

wait_for() {
  local description="$1"
  shift
  local attempts="${SMOKE_WAIT_ATTEMPTS:-60}"
  local sleep_seconds="${SMOKE_WAIT_SECONDS:-2}"
  for _ in $(seq 1 "${attempts}"); do
    if "$@" >/dev/null 2>&1; then
      echo "ok: ${description}"
      return 0
    fi
    sleep "${sleep_seconds}"
  done
  echo "timeout: ${description}" >&2
  "$@" || true
  return 1
}

backend_health() {
  curl -fsS http://localhost:8000/health
}

snapshot_empty_or_ready() {
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=1m&limit=30" \
    | python -c 'import json,sys; p=json.load(sys.stdin); assert p["symbol"]; assert p["dataStatus"] in {"empty","partial","ready"}'
}

request_backfill() {
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "{\"symbol\":\"${SYMBOL}\",\"interval\":\"1m\"}" \
    http://localhost:8000/api/charts/backfill
}

status_succeeded() {
  curl -fsS "http://localhost:8000/api/charts/backfill/status?symbol=${SYMBOL}&interval=1m" \
    | python -c 'import json,sys; p=json.load(sys.stdin); assert p["status"] == "succeeded", p'
}

rest_ready() {
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=1m&limit=30" \
    | python -c 'import json,sys; p=json.load(sys.stdin); assert p["dataStatus"] in {"partial","ready"}, p; assert len(p["candles"]) > 0, p'
}

clickhouse_has_materialized_candles() {
  local count
  count="$(docker exec alfaka-clickhouse clickhouse-client \
    --user alfaka \
    --password alfaka \
    --database market_data \
    --query "SELECT count() FROM chart_candles WHERE symbol = '${SYMBOL}'")"
  [[ "${count}" =~ ^[1-9][0-9]*$ ]]
}

minio_has_backfill_objects() {
  docker run --rm --network alfaka-data-net --entrypoint /bin/sh minio/mc:latest -c \
    "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && \
     test -n \"\$(mc find local/alfaka-market-data/market-data/smoke/raw --name '*.jsonl')\" && \
     test -n \"\$(mc find local/alfaka-market-data/market-data/smoke/final --name '*.jsonl')\""
}

cd "${ROOT_DIR}"

"${COMPOSE[@]}" config --quiet

if [[ "${SMOKE_BUILD:-1}" == "1" ]]; then
  "${COMPOSE[@]}" up -d --build redis minio minio-init clickhouse gops-backend
else
  "${COMPOSE[@]}" up -d redis minio minio-init clickhouse gops-backend
fi

wait_for "backend health" backend_health
wait_for "initial REST snapshot status" snapshot_empty_or_ready

request_backfill >/tmp/alfaka-backfill-smoke-response.json
cat /tmp/alfaka-backfill-smoke-response.json
echo

wait_for "backfill succeeded" status_succeeded
wait_for "ClickHouse materialized candles" clickhouse_has_materialized_candles
wait_for "S3 raw and processed/final objects" minio_has_backfill_objects
wait_for "REST snapshot ready" rest_ready

echo "backfill smoke passed: symbol=${SYMBOL}"
