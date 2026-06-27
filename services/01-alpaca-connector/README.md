# 01 Alpaca Connector

역할: Alpaca 시세와 주문 연동의 외부 접점입니다.

현재 구현:

```text
market_stream.py          Alpaca WebSocket bars/updatedBars/trades -> Kafka Raw
historical_backfill.py    Alpaca Historical REST -> S3 Raw
```

아직 구현하지 않은 자리:

```text
orders/                   Alpaca 주문/체결 연동 placeholder
```

이 Pod는 외부 Alpaca API와 직접 통신합니다. 다른 서비스는 Alpaca 원본 API를 직접 호출하지 않고 Kafka, Redis, S3, ClickHouse 계약을 통해 데이터를 받는 것을 원칙으로 합니다.
