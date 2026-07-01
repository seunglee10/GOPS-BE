#!/usr/bin/env bash
# 역할: missing chart data -> explicit backfill -> ClickHouse -> optional S3 backfill archive -> REST ready 경로를 검증합니다.
# 사용: bash scripts/local/smoke-backfill-missing-data.sh AAPL [1m|5m|10m|1D|1W|1M]
set -euo pipefail

SYMBOL="${1:-AAPL}"
if [[ ! "${SYMBOL}" =~ ^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$ ]]; then
  echo "Invalid smoke symbol: ${SYMBOL}" >&2
  exit 2
fi

normalize_interval() {
  case "${1:-1m}" in
    1m|5m|10m|1D|1W|1M) printf '%s\n' "$1" ;;
    1d) printf '1D\n' ;;
    1w) printf '1W\n' ;;
    1mo|1MO|1month) printf '1M\n' ;;
    *) return 1 ;;
  esac
}

source_interval_for() {
  case "$1" in
    5m|10m) printf '1m\n' ;;
    1W|1M) printf '1D\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

if ! INTERVAL="$(normalize_interval "${2:-1m}")"; then
  echo "Invalid smoke interval: ${2:-1m}" >&2
  exit 2
fi
SOURCE_INTERVAL="$(source_interval_for "${INTERVAL}")"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OVERRIDE_FILE="$(mktemp "${TMPDIR:-/tmp}/alfaka-backfill-smoke-compose.XXXXXX.yml")"
COMPOSE=(docker compose -f "${ROOT_DIR}/docker-compose.yml" -f "${OVERRIDE_FILE}")
SMOKE_RESPONSE_FILE="${TMPDIR:-/tmp}/alfaka-backfill-smoke-response.json"
SMOKE_STATUS_FILE="${TMPDIR:-/tmp}/alfaka-backfill-smoke-status.json"

resolve_python() {
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/python"
    return 0
  fi
  command -v python3
}

PYTHON_BIN="${PYTHON:-$(resolve_python)}"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 or .venv/bin/python is required." >&2
  exit 2
fi

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
      S3_BACKFILL_PROCESSED_PREFIX: "market-data/dev/helixho/smoke/backfill/processed"
      S3_BACKFILL_PROCESSED_FORMAT: "jsonl"
  backfill-worker:
    environment:
      S3_ENDPOINT_URL: "http://minio:9000"
      S3_BACKFILL_PROCESSED_PREFIX: "market-data/dev/helixho/smoke/backfill/processed"
      S3_BACKFILL_PROCESSED_FORMAT: "jsonl"
YAML

choose_smoke_range() {
  if [[ -n "${SMOKE_START:-}" && -n "${SMOKE_END:-}" ]]; then
    printf '%s %s\n' "${SMOKE_START}" "${SMOKE_END}"
    return 0
  fi
  if [[ "${SOURCE_INTERVAL}" == "1D" ]]; then
    SMOKE_INTERVAL="${INTERVAL}" SMOKE_DAYS_AGO="${SMOKE_DAYS_AGO:-90}" SMOKE_RANGE_DAYS="${SMOKE_RANGE_DAYS:-}" "${PYTHON_BIN}" - <<'PY'
import os
from datetime import date, datetime, timedelta, timezone

interval = os.environ.get("SMOKE_INTERVAL", "1D")
days_ago = int(os.environ.get("SMOKE_DAYS_AGO", "90"))
default_range_days = {"1D": 7, "1W": 21, "1M": 75}.get(interval, 7)
range_days = int(os.environ.get("SMOKE_RANGE_DAYS") or default_range_days)
anchor = datetime.now(timezone.utc).date() - timedelta(days=days_ago)

if interval == "1M":
    first_this_month = date(anchor.year, anchor.month, 1)
    previous_month_last_day = first_this_month - timedelta(days=1)
    start_date = date(previous_month_last_day.year, previous_month_last_day.month, 1)
elif interval == "1W":
    start_date = anchor - timedelta(days=anchor.weekday())
else:
    start_date = anchor
    while start_date.weekday() >= 5:
        start_date -= timedelta(days=1)

start = datetime.combine(start_date, datetime.min.time(), timezone.utc)
end = start + timedelta(days=range_days)
fmt = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
print(fmt(start), fmt(end))
PY
    return 0
  fi
  SMOKE_DAYS_AGO="${SMOKE_DAYS_AGO:-14}" SMOKE_RANGE_MINUTES="${SMOKE_RANGE_MINUTES:-30}" "${PYTHON_BIN}" - <<'PY'
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

days_ago = int(os.environ.get("SMOKE_DAYS_AGO", "14"))
minutes = int(os.environ.get("SMOKE_RANGE_MINUTES", "30"))
market_tz = ZoneInfo("America/New_York")
session_date = (datetime.now(market_tz) - timedelta(days=days_ago)).date()
while session_date.weekday() >= 5:
    session_date -= timedelta(days=1)
start = datetime.combine(session_date, time(9, 30), market_tz).astimezone(timezone.utc)
end = start + timedelta(minutes=minutes)
fmt = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
print(fmt(start), fmt(end))
PY
}

read -r SMOKE_START SMOKE_END < <(choose_smoke_range)

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
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=${INTERVAL}&limit=30" \
    | "${PYTHON_BIN}" -c 'import json,sys; p=json.load(sys.stdin); assert p["symbol"]; assert p["dataStatus"] in {"empty","partial","ready"}'
}

range_snapshot_empty_or_ready() {
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=${INTERVAL}&from=${SMOKE_START}&to=${SMOKE_END}&limit=5000" \
    | "${PYTHON_BIN}" -c 'import json,sys; p=json.load(sys.stdin); assert p["symbol"]; assert p["dataStatus"] in {"empty","partial","ready"}'
}

request_backfill() {
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "{\"symbol\":\"${SYMBOL}\",\"interval\":\"${INTERVAL}\",\"start\":\"${SMOKE_START}\",\"end\":\"${SMOKE_END}\",\"force\":true}" \
    http://localhost:8000/api/charts/backfill
}

status_succeeded() {
  curl -fsS "http://localhost:8000/api/charts/backfill/status?symbol=${SYMBOL}&interval=${INTERVAL}&requestId=${REQUEST_ID}" > "${SMOKE_STATUS_FILE}"
  "${PYTHON_BIN}" -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["status"] == "succeeded", p' "${SMOKE_STATUS_FILE}"
}

backfill_result_materialized_or_covered() {
  "${PYTHON_BIN}" -c '
import json, sys
p = json.load(open(sys.argv[1]))
result = p.get("result") or {}
source = result.get("source")
if source == "alpaca":
    assert int(result.get("materializedRowCount") or 0) > 0, p
elif source == "clickhouse":
    assert result.get("skipped") is True, p
elif source in {"calendar-empty", "alpaca-empty"}:
    raise AssertionError(p)
else:
    raise AssertionError(p)
' "${SMOKE_STATUS_FILE}"
}

rest_ready() {
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=${INTERVAL}&limit=30" \
    | "${PYTHON_BIN}" -c 'import json,sys; p=json.load(sys.stdin); assert p["dataStatus"] in {"partial","ready"}, p; assert len(p["candles"]) > 0, p'
}

range_rest_ready() {
  curl -fsS "http://localhost:8000/api/charts/candles?symbol=${SYMBOL}&interval=${INTERVAL}&from=${SMOKE_START}&to=${SMOKE_END}&limit=5000" \
    | "${PYTHON_BIN}" -c 'import json,sys; p=json.load(sys.stdin); assert p["dataStatus"] in {"partial","ready"}, p; assert len(p["candles"]) > 0, p'
}

backfill_queue_healthy() {
  curl -fsS "http://localhost:8000/api/charts/backfill/queue" \
    | "${PYTHON_BIN}" -c 'import json,sys; p=json.load(sys.stdin); assert (p.get("deadLetter") or {}).get("length", 0) == 0, p'
}

redis_recent_accessible() {
  docker exec alfaka-redis redis-cli ZCARD "candle:${SYMBOL}:${SOURCE_INTERVAL}:recent" | grep -Eq '^[0-9]+$'
}

redis_live_accessible() {
  docker exec alfaka-redis redis-cli GET "candle:${SYMBOL}:${SOURCE_INTERVAL}:live" >/dev/null
}

clickhouse_interval_filter() {
  if [[ "${SOURCE_INTERVAL}" == "1D" ]]; then
    printf "interval IN ('1D', '1d')"
  else
    printf "interval = '%s'" "${SOURCE_INTERVAL}"
  fi
}

clickhouse_range_count() {
  local interval_filter
  interval_filter="$(clickhouse_interval_filter)"
  docker exec alfaka-clickhouse clickhouse-client \
    --user alfaka \
    --password alfaka \
    --database market_data \
    --query "SELECT count() FROM chart_candles WHERE symbol = '${SYMBOL}' AND ${interval_filter} AND event_time >= parseDateTimeBestEffort('${SMOKE_START}') AND event_time < parseDateTimeBestEffort('${SMOKE_END}')"
}

clickhouse_has_materialized_candles() {
  local count
  local interval_filter
  interval_filter="$(clickhouse_interval_filter)"
  count="$(docker exec alfaka-clickhouse clickhouse-client \
    --user alfaka \
    --password alfaka \
    --database market_data \
    --query "SELECT count() FROM chart_candles WHERE symbol = '${SYMBOL}' AND ${interval_filter}")"
  [[ "${count}" =~ ^[1-9][0-9]*$ ]]
}

clickhouse_range_has_materialized_candles() {
  local count
  count="$(clickhouse_range_count)"
  [[ "${count}" =~ ^[1-9][0-9]*$ ]]
}

minio_has_backfill_objects() {
  "${PYTHON_BIN}" -c '
import json, sys
p = json.load(open(sys.argv[1]))
result = p.get("result") or {}
archive_status = result.get("archiveStatus")
sys.exit(0 if archive_status == "archived" else 42)
' "${SMOKE_STATUS_FILE}" || {
    local status=$?
    [[ "${status}" == "42" ]] && return 0
    return "${status}"
  }
  docker run --rm --network alfaka-data-net --entrypoint /bin/sh minio/mc:latest -c \
    "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && \
     test -n \"\$(mc find local/${S3_BUCKET:-gops-market-data-<aws-account-id>-ap-northeast-2-an}/market-data/dev/helixho/smoke/backfill/processed --name '*.jsonl')\""
}

cd "${ROOT_DIR}"

"${COMPOSE[@]}" config --quiet

if [[ "${SMOKE_BUILD:-1}" == "1" ]]; then
  "${COMPOSE[@]}" up -d --build redis minio minio-init clickhouse gops-backend backfill-worker
else
  "${COMPOSE[@]}" up -d redis minio minio-init clickhouse gops-backend backfill-worker
fi

wait_for "backend health" backend_health
wait_for "initial REST snapshot status" snapshot_empty_or_ready
wait_for "initial range REST snapshot status" range_snapshot_empty_or_ready
wait_for "backfill queue healthy before request" backfill_queue_healthy
wait_for "Redis recent key accessible" redis_recent_accessible
wait_for "Redis live key accessible" redis_live_accessible

echo "smoke range: ${SMOKE_START} -> ${SMOKE_END}"
echo "smoke interval: requested=${INTERVAL} source=${SOURCE_INTERVAL}"
echo "ClickHouse range rows before: $(clickhouse_range_count)"

request_backfill > "${SMOKE_RESPONSE_FILE}"
cat "${SMOKE_RESPONSE_FILE}"
echo
REQUEST_ID="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["requestId"])' "${SMOKE_RESPONSE_FILE}")"

wait_for "backfill succeeded" status_succeeded
wait_for "backfill materialized or was already covered" backfill_result_materialized_or_covered
wait_for "ClickHouse materialized candles" clickhouse_has_materialized_candles
wait_for "ClickHouse range materialized candles" clickhouse_range_has_materialized_candles
wait_for "optional S3 backfill archive objects" minio_has_backfill_objects
wait_for "backfill queue healthy after request" backfill_queue_healthy
wait_for "REST snapshot ready" rest_ready
wait_for "range REST snapshot ready" range_rest_ready

echo "backfill smoke passed: symbol=${SYMBOL} interval=${INTERVAL} sourceInterval=${SOURCE_INTERVAL}"
