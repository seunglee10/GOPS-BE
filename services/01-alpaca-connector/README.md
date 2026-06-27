# 01 Alpaca Connector

역할: Alpaca 시장 데이터 수집의 외부 접점입니다.

현재 구현:

```text
market_stream.py          Alpaca WebSocket bars/updatedBars -> Kafka Raw
historical_backfill.py    Alpaca Historical REST bars -> S3 raw
```

실시간 tick 정책:

```text
기본 구독은 bars, updatedBars만 사용한다.
GOPS chart WebSocket에 접속한 symbol은 Redis active key로 표시된다.
market_stream.py는 active key가 살아 있는 symbol만 trades를 동적으로 구독한다.
```

이 Pod는 외부 Alpaca API와 직접 통신합니다. 다른 서비스는 Kafka, Redis, S3, ClickHouse 계약을 통해 데이터를 받습니다.
