# GOPS Backend

조현호 GOPS backend를 김희준 `alfaka` 시장 데이터 파이프라인에 붙인 FastAPI 서버입니다.

```text
과거/최근 조회: ClickHouse + Redis -> GET /api/charts/candles
실시간 차트: Redis Pub/Sub/live key -> WS /ws/charts
심볼 목록: ALPACA_SYMBOLS 또는 config/market-data-request.json -> GET /api/charts/symbols
활성 tick 제어: WS 접속 중 Redis active:charts:{symbol} TTL 갱신
```

## Run

```bash
cd services/07-api-websocket/gops-backend
PYTHONPATH=/Users/heejunkim/Documents/alfaka/gops/packages:. uvicorn app.main:app --reload
```

## Endpoint

```text
GET /health
GET /api/charts/candles?symbol=NVDA&interval=1m&limit=160
GET /api/charts/symbols
WS  /ws/charts?symbol=NVDA&interval=1m
```

## Required Env

```text
REDIS_URL=redis://localhost:6379/0
CLICKHOUSE_HTTP_URL=http://localhost:8123
CLICKHOUSE_DATABASE=market_data
CLICKHOUSE_USER=alfaka
CLICKHOUSE_PASSWORD=alfaka
```

`ALPACA_SYMBOLS`가 없으면 `config/market-data-request.json`의 semiconductor-100 universe를 사용합니다.
