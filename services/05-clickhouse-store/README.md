# 05 ClickHouse Store

역할: 과거 캔들 조회와 분석용 저장소입니다.

Schema:

```text
infra/clickhouse/initdb/01-market-data.sql
```

Loader:

```text
processed_loader.py                          Kafka Processed -> ClickHouse 실행 entrypoint
packages/alfaka/storage/clickhouse_loader.py ClickHouse 적재 logic
```

기본 정책:

```text
market_data.chart_candles를 기본 조회 테이블로 사용한다.
KAFKA_CLICKHOUSE_TOPICS 기본값은 market.candles.closed.v1이다.
market_data.trade_ticks는 옵션 테이블이다.
CLICKHOUSE_LOAD_TRADES=false이면 TRADE event는 적재하지 않는다.
```

읽기 위치:

```text
packages/alfaka/serving/clickhouse_provider.py
services/07-api-websocket/gops-backend/app/services/alfaka_market_data.py
```
