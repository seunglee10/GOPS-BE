# 07 API WebSocket

역할: 차트 엔진이 읽는 REST API와 실시간 WebSocket 서버입니다.

하위 자리:

```text
gops-backend/        GOPS FastAPI backend 실제 병합본
chart-api/           과거 캔들 REST API adapter 예시
websocket-gateway/   LIVE_CANDLE_UPDATE, CANDLE_CLOSED, CANDLE_CORRECTED push 예시
```

이 계층은 Alpaca 원본 API나 Kafka Raw Topic을 직접 노출하지 않습니다. Redis, ClickHouse, S3, Processed Topic을 조합해 차트 엔진용 계약으로 변환합니다.

현재 실제 실행 경로:

```text
gops-backend/app/routes/charts.py
gops-backend/app/routes/streams.py
gops-backend/app/services/alfaka_market_data.py
```

참고용 adapter 예시:

```text
chart-api/gops_provider_example.py
websocket-gateway/gops_stream_example.py
packages/alfaka/serving/
```

GOPS backend는 위 예시를 참고해 `packages/alfaka/serving/`을 직접 import하도록 병합되어 있습니다.
