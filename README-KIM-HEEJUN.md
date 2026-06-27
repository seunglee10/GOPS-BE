# 김희준 README

담당: Alpaca 시장 데이터 파이프라인

한 줄 책임:

```text
Alpaca -> Kafka Raw -> Stream/Flink Processor -> Kafka Processed -> Redis/S3/ClickHouse
```

## 수정해도 되는 파일

```text
packages/alfaka/alpaca/
packages/alfaka/common/
packages/alfaka/streaming/
packages/alfaka/storage/
services/01-alpaca-connector/
services/02-kafka-event-publisher/
services/03-flink-stream-processor/
services/04-redis-state-store/
services/05-clickhouse-store/
services/06-s3-store/
flink-jobs/
infra/clickhouse/
scripts/local/
```

## 공동 수정 파일

```text
config/market-data-request.json
packages/alfaka/serving/
services/07-api-websocket/gops-backend/app/services/alfaka_market_data.py
services/07-api-websocket/gops-backend/app/routes/charts.py
services/07-api-websocket/gops-backend/app/routes/streams.py
docker-compose.yml
docs/data-contracts.md
```

공동 수정 기준:

```text
김희준: 데이터 payload, Redis key, ClickHouse query, S3 path 계약
조현호: frontend/API가 기대하는 DTO와 화면 동작
정범진: 운영 endpoint, IAM, ConfigMap, Secret, 리소스 스펙
```

## 직접 수정하지 않는 파일

```text
apps/gops-frontend/
apps/chart-engine/
infra/k8s/
infra/docker/
scripts/aws/
```

## 현재 데이터 정책

```text
상시 Alpaca 구독: bars, updatedBars
차트 진입 후 구독: Redis active key가 있는 symbol만 trades
초기 차트 로드: /api/charts/candles -> Redis recent + ClickHouse chart_candles
오늘 tick 전체 초기 로드: 금지
S3 확정 저장: 전날까지 market-data/final/candles
S3 live 저장: 오늘 live candle/tick은 market-data/live
ClickHouse 기본 적재: market_data.chart_candles
ClickHouse tick 적재: CLICKHOUSE_LOAD_TRADES=true일 때만
```

## S3 스펙

```text
bucket: alfaka-market-data-{env}
region: ap-northeast-2
encryption: SSE-S3 또는 SSE-KMS
public access: block all
format: parquet snappy
raw format: jsonl
```

경로:

```text
market-data/raw/alpaca/source=alpaca/channel={bars|trades}/symbol={SYMBOL}/year=YYYY/month=MM/day=DD/hour=HH/
market-data/final/candles/interval={1m|5m|10m}/symbol={SYMBOL}/year=YYYY/month=MM/day=DD/
market-data/live/candles/interval=1m/symbol={SYMBOL}/year=YYYY/month=MM/day=DD/
market-data/live/trades/symbol={SYMBOL}/year=YYYY/month=MM/day=DD/hour=HH/
```

운영 규칙:

```text
전날까지 확정된 candle은 final에 둔다.
오늘 live candle과 trades는 live에 둔다.
장마감 이후 live/trades는 필요하면 batch compaction으로 final 또는 별도 archive prefix에 합친다.
프론트 첫 진입 때 오늘 tick 전체를 S3에서 읽지 않는다.
```

## ClickHouse 스펙

```text
database: market_data
primary table: chart_candles
optional table: trade_ticks
load audit table: load_audit
HTTP endpoint env: CLICKHOUSE_HTTP_URL
default loader topic: market.candles.closed.v1
```

조회 기준:

```text
chart_candles ORDER BY (symbol, interval, event_time)
프론트 과거 조회는 ClickHouse chart_candles만 기본 사용
trade_ticks는 분석/감사용 옵션이며 기본 loader에서는 끈다
```

## Redis 스펙

```text
price:{symbol}:latest                  TTL 1일
candle:{symbol}:1m:live                TTL 1일
candle:{symbol}:{interval}:latest      TTL 1일
candles:{symbol}:{interval}            TTL 7일
market.events                          Pub/Sub 전체 이벤트
market.events:{symbol}                 Pub/Sub symbol 이벤트
active:charts:symbols                  현재 차트 활성 symbol set
active:charts:{symbol}                 TTL 45초, tick 동적 구독 기준
```

## 실행/검증

```sh
docker compose up -d --build
docker compose --profile alpaca up -d --build
docker compose --profile backfill run --rm historical-backfill
PYTHONPATH=packages python scripts/local/preview-subscription.py NVDA
PYTHONPATH=packages python scripts/local/check-redis.py NVDA --interval 1m
PYTHONPATH=packages python scripts/local/check-s3.py NVDA --interval 1m
scripts/local/check-clickhouse.sh
```
