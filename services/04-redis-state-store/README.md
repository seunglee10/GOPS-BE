# 04 Redis State Store

역할: 차트 렌더링에 필요한 최신 상태와 최근 시리즈를 저장합니다.

Redis는 애플리케이션 코드가 아니라 인프라 서비스입니다. 이 디렉터리는 어떤 key를 어떤 서비스가 쓰는지 고정하는 계약 자리입니다.

현재 write 위치:

```text
packages/alfaka/streaming/processor.py
```

Key 계약:

```text
price:{symbol}:latest
candle:{symbol}:1m:live
candle:{symbol}:{interval}:latest
candles:{symbol}:{interval}
```

읽기 후보:

```text
services/07-api-websocket/chart-api/
services/07-api-websocket/websocket-gateway/
```
