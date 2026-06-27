# 04 Redis State Store

역할: 차트 렌더링에 필요한 최신 상태, 최근 시리즈, 활성 차트 symbol을 저장합니다.

Redis는 애플리케이션 코드가 아니라 인프라 서비스입니다. 이 디렉터리는 어떤 key를 어떤 서비스가 쓰는지 고정하는 계약 자리입니다.

write 위치:

```text
packages/alfaka/streaming/processor.py
services/07-api-websocket/gops-backend/app/routes/streams.py
```

read 위치:

```text
packages/alfaka/serving/redis_provider.py
packages/alfaka/alpaca/websocket_collector.py
services/07-api-websocket/gops-backend/app/routes/streams.py
```

Key 계약:

```text
price:{symbol}:latest
candle:{symbol}:1m:live
candle:{symbol}:{interval}:latest
candles:{symbol}:{interval}
active:charts:symbols
active:charts:{symbol}
```
