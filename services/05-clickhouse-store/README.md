# 05 ClickHouse Store

역할: 과거 캔들 조회와 분석용 저장소입니다.

현재 schema:

```text
infra/clickhouse/initdb/01-market-data.sql
```

현재 loader:

```text
processed_loader.py                         Kafka Processed -> ClickHouse 실행 entrypoint
packages/alfaka/storage/clickhouse_loader.py ClickHouse 적재 logic
```

ClickHouse는 Redis보다 긴 기간의 조회를 담당합니다. 로컬에서는 Processed Kafka Topic을 직접 읽어 `trade_ticks`, `chart_candles`에 넣고, 운영에서는 Flink sink 또는 S3 Parquet 적재 job으로 교체할 수 있습니다.

읽기 후보:

```text
services/07-api-websocket/chart-api/
```
