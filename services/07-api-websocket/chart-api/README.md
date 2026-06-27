# Chart API

팀원 Chart API 코드가 들어올 자리입니다.

권장 책임:

- `GET /api/charts/candles` 초기 차트 응답 제공
- Redis의 최근 캔들 우선 조회
- Redis에 없는 구간은 ClickHouse 조회
- ClickHouse/S3에 누락된 구간은 Alpaca Historical REST 백필로 보완
- 차트 엔진에 줄 candle/volume/MA 응답 DTO 고정

현재 데이터 소스:

- Redis latest/live/series: `packages/alfaka/streaming/processor.py`
- Processed S3 sink: `packages/alfaka/storage/processed_s3_sink.py`
- Raw historical backfill: `packages/alfaka/alpaca/historical_backfill.py`
- ClickHouse schema: `infra/clickhouse/initdb/01-market-data.sql`
