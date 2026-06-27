# 07 API WebSocket

역할: 차트 엔진이 읽는 REST API와 실시간 WebSocket 서버입니다.

실제 실행 경로:

```text
gops-backend/app/routes/charts.py
gops-backend/app/routes/streams.py
gops-backend/app/services/alfaka_market_data.py
```

원칙:

```text
Frontend는 Alpaca, Kafka, S3를 직접 읽지 않는다.
과거/초기 candle은 REST API로 받는다.
실시간 candle은 WebSocket으로 받는다.
backend는 Redis active key를 갱신해 ingestor의 tick 동적 구독을 유도한다.
```
