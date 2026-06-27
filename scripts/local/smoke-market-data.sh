#!/usr/bin/env bash
# 역할: 로컬 Docker Compose 시장 데이터 경로를 한 번에 검증합니다.
# 검증: Kafka raw ingest -> processor -> Redis/ClickHouse/S3 -> REST -> WebSocket handshake.
# 사용: bash scripts/local/smoke-market-data.sh AAPL
set -euo pipefail

SYMBOL="${1:-AAPL}"
if [[ ! "${SYMBOL}" =~ ^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$ ]]; then
  echo "Invalid smoke symbol: ${SYMBOL}" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OVERRIDE_FILE="$(mktemp "${TMPDIR:-/tmp}/alfaka-smoke-compose.XXXXXX.yml")"
COMPOSE=(docker compose -f "${ROOT_DIR}/docker-compose.yml" -f "${OVERRIDE_FILE}")

cleanup() {
  local status=$?
  if [[ "${SMOKE_CLEANUP:-0}" == "1" ]]; then
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -f "${OVERRIDE_FILE}"
  exit "${status}"
}
trap cleanup EXIT

cat > "${OVERRIDE_FILE}" <<'YAML'
services:
  s3-sink:
    environment:
      S3_FLUSH_COUNT: "1"
      S3_PROCESSED_FORMAT: "jsonl"
  local-stream-processor:
    environment:
      PROCESSOR_LOG_EVERY_N: "1"
  gops-backend:
    environment:
      REDIS_URL: "redis://redis:6379/0"
      CLICKHOUSE_HTTP_URL: "http://clickhouse:8123"
      CLICKHOUSE_DATABASE: "market_data"
      CLICKHOUSE_USER: "alfaka"
      CLICKHOUSE_PASSWORD: "alfaka"
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

redis_has_key() {
  local key="$1"
  [[ "$(docker exec alfaka-redis redis-cli --raw EXISTS "${key}")" == "1" ]]
}

clickhouse_has_candles() {
  local count
  count="$(docker exec alfaka-clickhouse clickhouse-client \
    --user alfaka \
    --password alfaka \
    --database market_data \
    --query "SELECT count() FROM chart_candles WHERE symbol = '${SYMBOL}'")"
  [[ "${count}" =~ ^[1-9][0-9]*$ ]]
}

minio_has_market_objects() {
  docker run --rm --network alfaka-data-net --entrypoint /bin/sh minio/mc:latest -c \
    "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && test -n \"\$(mc find local/alfaka-market-data --name '*.jsonl')\""
}

rest_has_candles() {
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=1m&limit=30" | grep -q "\"symbol\":\"${SYMBOL}\""
}

websocket_accepts_chart_stream() {
  python - "${SYMBOL}" <<'PY'
import base64
import os
import socket
import sys

symbol = sys.argv[1]
key = base64.b64encode(os.urandom(16)).decode("ascii")
request = (
    f"GET /ws/charts?symbol={symbol}&interval=1m HTTP/1.1\r\n"
    "Host: localhost:8000\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "\r\n"
).encode("ascii")

with socket.create_connection(("127.0.0.1", 8000), timeout=5) as sock:
    sock.sendall(request)
    response = sock.recv(1024)

if b" 101 " not in response.split(b"\r\n", 1)[0]:
    raise SystemExit(response.decode("latin1", errors="replace"))
PY
}

cd "${ROOT_DIR}"

"${COMPOSE[@]}" config --quiet

if [[ "${SMOKE_BUILD:-1}" == "1" ]]; then
  "${COMPOSE[@]}" up -d --build kafka kafka-init redis minio minio-init clickhouse local-stream-processor s3-sink clickhouse-loader gops-backend
else
  "${COMPOSE[@]}" up -d kafka kafka-init redis minio minio-init clickhouse local-stream-processor s3-sink clickhouse-loader gops-backend
fi

wait_for "Redis ready" docker exec alfaka-redis redis-cli ping
wait_for "ClickHouse ready" docker exec alfaka-clickhouse clickhouse-client --user alfaka --password alfaka --query "SELECT 1"
wait_for "backend health" curl -fsS http://localhost:8000/health

"${COMPOSE[@]}" run --rm --no-deps local-stream-processor python -m alfaka.tools.send_sample_market_data "${SYMBOL}"

wait_for "Redis recent candles" redis_has_key "candles:${SYMBOL}:1m"
wait_for "Redis live candle" redis_has_key "candle:${SYMBOL}:1m:live"
wait_for "ClickHouse chart candles" clickhouse_has_candles
wait_for "S3/MinIO market objects" minio_has_market_objects
wait_for "REST candle snapshot" rest_has_candles
wait_for "WebSocket chart handshake" websocket_accepts_chart_stream

echo "market data smoke passed: symbol=${SYMBOL}"
echo "cleanup: SMOKE_CLEANUP=1 bash scripts/local/smoke-market-data.sh ${SYMBOL}"
