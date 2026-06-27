# WebSocket Gateway

차트 엔진에 실시간 업데이트를 push하는 서비스가 들어올 자리입니다.

입력 계약:

```text
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
Redis latest/live/series keys
```

출력 계약:

```text
LIVE_CANDLE_UPDATE
CANDLE_CLOSED
CANDLE_CORRECTED
TRADE_TICK
```
