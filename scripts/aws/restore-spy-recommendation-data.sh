#!/usr/bin/env bash
# Restores only the real SPY 1m/1D benchmark inputs used by V3. Defaults to dry-run.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
APPLY="${APPLY:-false}"

SYMBOLS=SPY \
INTERVALS=1m,1D \
LOOKBACK_DAYS="${LOOKBACK_DAYS:-400}" \
APPLY="${APPLY}" \
WAIT_FOR_JOB="${WAIT_FOR_JOB:-true}" \
"${ROOT_DIR}/scripts/aws/run-session-candle-rebuild-job.sh"

if [[ "${APPLY}" != "true" ]]; then
  echo "dry-run complete; rerun with APPLY=true only after reviewing the job"
  exit 0
fi

kubectl -n "${NAMESPACE}" exec clickhouse-0 -- clickhouse-client --query "
SELECT 'daily_through_2026_07_13', count()
FROM market_data.chart_candles FINAL
WHERE symbol='SPY' AND interval='1D'
  AND event_time < toDateTime64('2026-07-14 04:00:00',3,'UTC');
SELECT 'previous_regular_session', count()
FROM market_data.chart_candles FINAL
WHERE symbol='SPY' AND interval='1m'
  AND event_time >= toDateTime64('2026-07-13 13:30:00',3,'UTC')
  AND event_time < toDateTime64('2026-07-13 20:00:00',3,'UTC');
SELECT 'july_14_regular_session', count()
FROM market_data.chart_candles FINAL
WHERE symbol='SPY' AND interval='1m'
  AND event_time >= toDateTime64('2026-07-14 13:30:00',3,'UTC')
  AND event_time < toDateTime64('2026-07-14 20:00:00',3,'UTC');
"
