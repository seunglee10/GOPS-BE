#!/usr/bin/env bash
# Restore only the local ClickHouse tables required by the fixed replay simulator.
# The private backup stays outside Git; this script never reads encrypted secrets.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
backup_root="${GOPS_PORTABLE_BACKUP_ROOT:-${repo_root}/.local-artifacts/aws-portable-backup/20260727T030132Z}"
archive="${backup_root}/data/clickhouse/gops-market-data-20260727T030132Z.zip"
archive_sums="${backup_root}/data/clickhouse/SHA256SUMS.txt"
container_name="${CLICKHOUSE_CONTAINER_NAME:-alfaka-clickhouse}"
archive_name="gops-market-data-20260727T030132Z.zip"
archive_in_container="/var/lib/clickhouse/backups/${archive_name}"
execute=false

usage() {
  cat <<'USAGE'
Usage: scripts/local/restore-simulator-backup.sh --execute

Restores these tables into the local Docker ClickHouse instance:
  market_data.simulation_replay_datasets
  market_data.simulation_replay_events
  market_data.simulation_replay_candles_1m
  market_data.chart_candles

The command drops and replaces only those local tables after verifying the
private backup ZIP checksum. It never contacts AWS and does not restore secrets.
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--execute" && "$#" == "1" ]]; then
  execute=true
fi
if [[ "${execute}" != true ]]; then
  usage >&2
  exit 2
fi

for command in docker shasum; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "Missing required command: ${command}" >&2
    exit 1
  }
done
[[ -f "${archive}" && ! -L "${archive}" ]] || {
  echo "Backup archive is missing or unsafe: ${archive}" >&2
  exit 1
}
[[ -f "${archive_sums}" && ! -L "${archive_sums}" ]] || {
  echo "Backup checksum manifest is missing or unsafe: ${archive_sums}" >&2
  exit 1
}

expected_sum="$(awk -v name="${archive_name}" '$2 == name { print $1 }' "${archive_sums}")"
[[ "${expected_sum}" =~ ^[0-9a-f]{64}$ ]] || {
  echo "No valid checksum for ${archive_name}" >&2
  exit 1
}
actual_sum="$(shasum -a 256 "${archive}" | awk '{ print $1 }')"
[[ "${actual_sum}" == "${expected_sum}" ]] || {
  echo "Backup checksum mismatch; refusing restore." >&2
  exit 1
}

docker container inspect "${container_name}" >/dev/null
docker exec "${container_name}" mkdir -p /var/lib/clickhouse/backups
remote_sum="$(docker exec "${container_name}" sh -c "
  test -f '${archive_in_container}' && sha256sum '${archive_in_container}' | awk '{ print \$1 }'
" || true)"
if [[ "${remote_sum}" != "${expected_sum}" ]]; then
  docker cp "${archive}" "${container_name}:${archive_in_container}"
  remote_sum="$(docker exec "${container_name}" sha256sum "${archive_in_container}" | awk '{ print $1 }')"
fi
[[ "${remote_sum}" == "${expected_sum}" ]] || {
  echo "Container copy checksum mismatch; refusing restore." >&2
  exit 1
}

tables=(
  simulation_replay_datasets
  simulation_replay_events
  simulation_replay_candles_1m
  chart_candles
)

for table in "${tables[@]}"; do
  docker exec "${container_name}" clickhouse-client --user alfaka --password alfaka \
    --query "DROP TABLE IF EXISTS market_data.${table} SYNC"
  docker exec "${container_name}" clickhouse-client --user alfaka --password alfaka \
    --query "RESTORE TABLE market_data.${table} FROM Disk('backups', '${archive_name}')"
done

verify_count() {
  local table="$1"
  local expected="$2"
  local actual
  actual="$(docker exec "${container_name}" clickhouse-client --user alfaka --password alfaka \
    --query "SELECT count() FROM market_data.${table}")"
  [[ "${actual}" == "${expected}" ]] || {
    echo "Restore count mismatch for ${table}: expected=${expected} actual=${actual}" >&2
    exit 1
  }
}

verify_count simulation_replay_datasets 4
verify_count simulation_replay_events 132526246
verify_count simulation_replay_candles_1m 327578

baseline="$(docker exec "${container_name}" clickhouse-client --user alfaka --password alfaka --query "
  SELECT count()
  FROM market_data.chart_candles FINAL
  WHERE interval IN ('1D', '1d')
    AND event_time >= toDateTime('2026-07-13 00:00:00', 'UTC')
    AND event_time < toDateTime('2026-07-14 00:00:00', 'UTC')
    AND is_closed = 1
    AND market_session = 'regular'
    AND canonical_version = 'v2'
    AND price_adjustment = 'split'
")"
[[ "${baseline}" -ge 502 ]] || {
  echo "Replay previous-close baseline is incomplete: ${baseline}/502 rows" >&2
  exit 1
}

echo "Simulator backup restore complete: replay data and chart baseline are ready."
