#!/usr/bin/env bash
set -euo pipefail

# 역할: 로컬 ClickHouse 컨테이너 접속과 기본 테이블 생성을 확인합니다.
# 사용: docker compose up 뒤 과거 조회 DB가 준비됐는지 빠르게 점검합니다.
# 실행: scripts/local/check-clickhouse.sh

docker exec alfaka-clickhouse clickhouse-client \
  --user alfaka \
  --password alfaka \
  --database market_data \
  --query "SHOW TABLES"

docker exec alfaka-clickhouse clickhouse-client \
  --user alfaka \
  --password alfaka \
  --database market_data \
  --query "SELECT database, name, total_rows FROM system.tables WHERE database = 'market_data' ORDER BY name"
