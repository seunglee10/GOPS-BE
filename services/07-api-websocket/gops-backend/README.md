# GOPS Backend

조현호 GOPS `Helix` backend를 김희준 `alfaka` 시장 데이터 파이프라인에 붙인 병합본입니다.

이 backend는 더 이상 더미 캔들을 생성하지 않습니다.

```text
과거/최근 조회: ClickHouse + Redis -> GET /api/charts/candles
실시간 차트: Redis Pub/Sub/live key -> WS /ws/charts
심볼 목록: ALPACA_SYMBOLS -> GET /api/charts/symbols
```

## Run

```bash
cd services/07-api-websocket/gops-backend
PYTHONPATH=/Users/heejunkim/Desktop/alfaka/packages:. uvicorn app.main:app --reload
```

## Endpoint

- `GET /health`: returns a JSON health response.
- `GET /api/charts/candles?symbol=AAPL&interval=1m&limit=160`
- `GET /api/charts/symbols`
- `WS /ws/charts?symbol=AAPL&interval=1m`

## Required Env

```text
ALPACA_SYMBOLS=AAPL,TSLA,NVDA
REDIS_URL=redis://localhost:6379/0
CLICKHOUSE_HTTP_URL=http://localhost:8123
CLICKHOUSE_DATABASE=market_data
CLICKHOUSE_USER=alfaka
CLICKHOUSE_PASSWORD=alfaka
```
