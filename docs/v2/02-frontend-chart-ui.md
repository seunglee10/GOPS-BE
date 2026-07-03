# 2. Frontend / UI Chart

## Mission

사용자가 보는 GOPS 화면과 차트 경험을 구현한다.

이 역할은 "차트를 그리는 코드"만 담당하지 않는다. 사용자가 종목, 뉴스, Agent 분석, 주문 상태를 한 화면에서 이해할 수 있도록 UI 상태와 상호작용을 설계한다.

## Owns

- React frontend
- Chart rendering
- Workspace UI
- Candlestick, line, area, OHLC bar 표시
- 거래량, RSI, MACD, Bollinger Bands, VWAP 같은 지표 표시
- WebSocket market update 표시
- SSE Agent event 표시
- Agent finding, warning, consensus UI
- 주문 ticket prefill UI

React는 브라우저 화면을 컴포넌트 단위로 만드는 JavaScript UI 라이브러리다. 컴포넌트는 버튼, 차트 패널, 주문 티켓처럼 재사용 가능한 화면 조각이다. GOPS에서는 `apps/gops-frontend`가 사용자가 직접 보는 앱이다.

WebSocket은 브라우저와 서버가 연결을 계속 유지하면서 양방향으로 메시지를 주고받는 방식이다. 차트 구독 symbol/timeframe을 바꾸거나 실시간 가격 업데이트를 받는 데 적합하다.

SSE는 Server-Sent Events의 줄임말이다. 서버가 브라우저로 이벤트를 계속 보내는 단방향 스트림이다. Agent 진행 상태처럼 클라이언트가 자주 메시지를 보낼 필요가 없는 흐름에 적합하다.

## Does Not Own

- Alpaca/SEC 데이터 수집
- Kafka 처리
- Redis/ClickHouse/S3 저장
- 지표 계산 backend worker
- 주문 API 검증
- KIS adapter
- GitHub Actions 배포

## Main Paths

- `apps/gops-frontend/`
- `apps/chart-engine/`
- `shared/chart-contract/` 후보

## Source Sections

`docs/v2/gops-v2-architecture.md`에서 먼저 볼 섹션:

- `5.1 Frontend`
- `8. Backend API Design`
- `10. Chart Data And Indicator Scope`
- `11. Chart Calculation And Rendering Decision`
- `12. Chart Agent Output`
- `20. Agent Event Model`
- `22. UI Workspace Design`
- `29.1 Repository Shape`
- `29.2 Ownership Rules`

## UI Scope

초기 차트 범위:

- 캔들스틱 OHLC
- 라인 차트
- 영역 차트
- OHLC bar 차트
- 거래량 histogram
- RSI
- MACD
- Stochastic
- SMA/EMA/WMA
- Bollinger Bands
- VWAP

초기 이벤트 표시:

- Alpaca 뉴스
- SEC filing
- 실적 발표
- 주문 체결/거부/대기
- 포트폴리오 리스크 이벤트
- Agent finding/warning/consensus

## Chart Rendering Decision

차트 그리는 방식은 아직 확정하지 않는다.

결정할 때 확인할 기준:

- 실시간 업데이트 성능
- 여러 지표 overlay 가능 여부
- 이벤트 marker 표시 가능 여부
- Agent finding을 가격대/시간 구간에 연결할 수 있는지
- 모바일/작은 화면에서 읽기 쉬운지
- backend와 공유할 chart contract가 단순한지

## API Dependencies

Frontend는 5번 담당자가 제공하는 API contract를 사용한다.

- `GET /api/charts/candles`
- `POST /api/charts/backfill`
- `GET /api/charts/backfill/status`
- `GET /api/charts/symbols`
- `WS /ws/charts`
- `POST /api/agents/analyze`
- `GET /api/agents/reports/{analysis_id}`
- `WS /ws/agent-alerts`
- `GET /api/order-contract`
- `POST /api/orders`
- `WS /ws/orders/{order_id}`

API 응답의 `meta.asOf`, `meta.cacheStatus`, `meta.stalenessMs`, `meta.isStale`은 UI에서 데이터 신선도를 표현하는 데 사용한다.

## First Implementation Checklist

- chart data shape을 3번, 5번 담당자와 맞춘다.
- candle/indicator/event layer를 분리한다.
- WebSocket reconnect와 구독 변경 동작을 정리한다.
- Agent finding을 차트 marker 또는 side panel에 표시하는 최소 UI를 만든다.
- 주문 버튼은 Agent가 자동 생성한 submit이 아니라 사용자 확인 액션으로만 동작하게 한다.
- 긴 텍스트와 작은 화면에서 UI가 깨지지 않는지 확인한다.

## Handoffs

- 1번 AI: finding, warning, consensus를 어떤 화면 요소로 보여줄지 맞춘다.
- 3번 Data Pipeline: candle/indicator/event 데이터 shape과 staleness 의미를 맞춘다.
- 4번 Infra: frontend image build와 배포 smoke test 경로를 맞춘다.
- 5번 Backend: API response, WebSocket message, SSE message contract를 맞춘다.
