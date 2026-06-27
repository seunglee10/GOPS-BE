# 조현호 README

담당: GOPS frontend, chart engine, backend API/WebSocket

한 줄 책임:

```text
Redis/ClickHouse -> GOPS Backend API/WebSocket -> Frontend -> Chart Engine
```

## 수정해도 되는 파일

```text
apps/gops-frontend/
apps/chart-engine/
services/07-api-websocket/gops-backend/app/routes/charts.py
services/07-api-websocket/gops-backend/app/routes/streams.py
services/07-api-websocket/gops-backend/app/routes/llm.py
services/07-api-websocket/gops-backend/app/contracts/
services/07-api-websocket/gops-backend/app/core/
services/07-api-websocket/gops-backend/app/services/ai_agents.py
shared/chart-contract/
```

## 공동 수정 파일

```text
services/07-api-websocket/gops-backend/app/services/alfaka_market_data.py
packages/alfaka/serving/dto.py
packages/alfaka/serving/provider.py
packages/alfaka/serving/redis_provider.py
packages/alfaka/serving/clickhouse_provider.py
config/market-data-request.json
docker-compose.yml
docs/data-contracts.md
infra/docker/Dockerfile.gops-frontend
infra/docker/Dockerfile.gops-backend
```

공동 수정 기준:

```text
조현호: DTO shape, WebSocket event, chart rendering, UX
김희준: Redis/ClickHouse 데이터 source와 payload 의미
정범진: container build path, service/ingress/env 연결
```

## 직접 수정하지 않는 파일

```text
packages/alfaka/alpaca/
packages/alfaka/streaming/
packages/alfaka/storage/
services/01-alpaca-connector/
services/02-kafka-event-publisher/
services/03-flink-stream-processor/
services/04-redis-state-store/
services/05-clickhouse-store/
services/06-s3-store/
infra/k8s/
scripts/aws/
```

## 폴더 분리 기준

```text
apps/chart-engine/
  React 화면에 종속되지 않는 차트 상태, 명령, 캔버스 렌더러, symbol 처리

apps/gops-frontend/
  React component, layout, toolbar, API/WebSocket 호출, 화면 상태
```

frontend는 chart engine을 아래 경로로 import합니다.

```ts
import { makeChartCommand } from "@gops/chart-engine/commands";
```

## 데이터 호출 계약

초기 차트 로드:

```http
GET /api/charts/candles?symbol=NVDA&interval=1m&ma=5,20,60&limit=160
```

실시간 차트:

```text
ws://localhost:8000/ws/charts?symbol=NVDA&interval=1m
```

중요한 동작:

```text
프론트가 WebSocket에 접속하면 backend가 Redis active:charts:{symbol} TTL 키를 갱신한다.
Alpaca ingestor는 이 활성 symbol만 trades로 동적 구독한다.
검색은 고정 5개가 아니라 Alpaca symbol 형식이면 허용한다.
SUPPORTED_SYMBOLS는 추천/기본 watchlist일 뿐 검색 제한 목록이 아니다.
```

## 실행/검증

```sh
cd apps/gops-frontend
npm run test:chart
npm run build
npm run dev
```

Docker 전체 실행:

```sh
docker compose up -d --build gops-backend gops-frontend
```
