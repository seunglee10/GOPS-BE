# 3. Market Data / News / Fundamentals / Storage Pipeline

## Mission

외부 데이터 공급자에서 데이터를 가져오고, Kafka/Redis/ClickHouse/S3를 통해 GOPS가 사용할 수 있는 데이터 projection을 만든다.

이 역할은 UI 차트를 직접 그리지 않는다. 대신 UI와 Agent가 읽을 수 있는 차트용 데이터, 뉴스 이벤트, 펀더멘탈 요약, S3 artifact를 만든다.

## Owns

- Alpaca market data 수집
- Alpaca news 수집
- Alpaca news 원문 payload S3 저장
- SEC EDGAR filing 수집
- SEC fundamentals 정규화와 artifact 저장
- trades/quotes/bars/tick 처리
- Kafka topic produce/consume
- Redis latest quote/live candle/summary cache
- ClickHouse market/fundamentals time series
- S3 processed tick artifact
- S3 raw/derived artifact
- Market Processor
- News Ingestor
- Fundamentals Worker
- Backfill Worker
- S3 Sink
- ClickHouse Loader

Alpaca는 미국 주식 시장 데이터와 뉴스 데이터를 제공하는 외부 API 공급자다. GOPS에서는 가격, 체결, quote, bar, 뉴스 수집에 사용하고, 주문에는 사용하지 않는다.

SEC EDGAR는 미국 증권거래위원회가 제공하는 기업 공시 데이터 시스템이다. GOPS에서는 재무제표, filing, valuation 지표를 만들기 위한 원천 데이터로 사용한다.

## Does Not Own

- Agent 분석 문장 생성
- 차트 화면 렌더링
- Google OAuth2 로그인
- 주문 API와 KIS adapter
- GitHub Actions workflow
- Kubernetes manifest 최종 배포 반영

## Main Paths

- `systems/market-data/`
- `systems/market-data/pods/market-ingestor/`
- `systems/market-data/pods/news-ingestor/`
- `systems/market-data/pods/market-processor/`
- `systems/market-data/pods/s3-sink/`
- `systems/market-data/pods/clickhouse-loader/`
- `systems/market-data/pods/backfill-worker/`
- `systems/market-data/shared/alfaka/`
- `systems/fundamentals/` 후보
- `platform/kafka/`
- `platform/s3/`
- `platform/clickhouse/`
- `platform/redis/`

## Source Sections

`docs/v2/gops-v2-architecture.md`에서 먼저 볼 섹션:

- `5.3 Alpaca Marketdata Worker`
- `5.4 Alpaca News Worker`
- `5.5 Fundamentals Worker`
- `5.6 Market Processor`
- `10. Chart Data And Indicator Scope`
- `11.1 Indicator Calculation`
- `13. Alpaca Market Data`
- `14. News and Fundamentals`
- `15. S3 Storage`
- `16. Redis Cache Design`
- `18. ClickHouse Data Model`
- `19. Kafka Topics`
- `23.3 Market Data Reliability And Cache`
- `25. Testing Strategy`
- `29.3 New System Criteria`
- `29.4 Pod And Job Map`
- `29.5 Platform Contracts To Add Or Keep Current`

## Pipeline Responsibilities

### Market Data

- Alpaca REST API 또는 streaming API에서 trades, quotes, bars를 수집한다.
- 외부 payload를 내부 event shape으로 정규화한다.
- Kafka topic에 발행한다.
- 중복 제거, pagination, retry/backoff를 처리한다.
- exchange code, condition code reference data를 갱신한다.

### News

- 관심 종목별 Alpaca news를 수집한다.
- 뉴스 원문 payload를 S3 raw bucket/prefix에 저장한다.
- 정규화된 `NewsEvent`를 만든다.
- `news.alpaca` topic에 발행한다.
- Redis 최신 뉴스 summary warm cache를 갱신한다.

### Fundamentals

- SEC filing 원문을 수집한다.
- XBRL/재무제표 데이터를 정규화한다.
- 매출, 영업이익, 순이익, EPS, 부채비율, margin, valuation 지표를 계산한다.
- 원문 filing과 파싱 artifact를 S3 raw/derived에 저장한다.
- ClickHouse fundamentals time series를 갱신한다.
- Agent가 사용할 `EvidenceItem`을 만들 수 있게 데이터 출처를 보존한다.

XBRL은 기업 재무제표를 기계가 읽을 수 있게 표현하는 표준 형식이다. SEC filing 안의 재무 수치를 안정적으로 추출하고 비교하는 데 필요하다.

## Storage Rules

S3는 AWS 객체 저장소다. 큰 파일이나 artifact를 `bucket/key` 형태로 저장한다. GOPS에서는 replay, audit, 재처리, evidence 보존에 사용한다.

ClickHouse는 대량의 시계열/분석 데이터를 빠르게 조회하기 위한 컬럼형 데이터베이스다. candle, trade, quote, indicator, fundamentals처럼 시간 기준으로 많이 조회하는 데이터를 저장한다.

Redis는 메모리 기반 key-value 저장소다. 최신 quote, live candle, session, component health처럼 빠른 조회가 필요한 데이터에 사용한다.

규칙:

- S3 tick 데이터는 원본 Alpaca tick payload가 아니라 가공된 tick 데이터다.
- 조회 API는 S3를 직접 serving source로 쓰지 않고 Redis/ClickHouse projection을 우선 사용한다.
- processed output 기본 format은 `S3_PROCESSED_FORMAT=parquet`이다.
- canonical historical candle은 `priceAdjustment=split`, `canonicalVersion=v2` metadata를 요구한다.
- S3 PUT은 retry와 checksum을 고려한다.

## Kafka Topics

초기 topic 범위:

- `market.alpaca.trades`
- `market.alpaca.quotes`
- `market.alpaca.bars`
- `news.alpaca`
- `fundamentals.sec`
- `market.ticks.v1`
- `market.candles.live.1m.v1`
- `market.candles.closed.v1`
- `market.status.v1`
- `market.volume-profile-bins.1m.v1`

처리 규칙:

- at-least-once 처리를 전제로 한다.
- 중복은 `eventId` 또는 natural key로 제거한다.
- schemaVersion 변경은 backward-compatible을 우선한다.
- 처리 실패 이벤트는 DLQ로 보낸다.

DLQ는 Dead Letter Queue의 줄임말이다. 정상 처리에 실패한 메시지를 격리해 나중에 원인을 분석하거나 재처리할 수 있게 하는 큐다.

## First Implementation Checklist

- Kafka topic contract를 `platform/kafka/topics.txt`와 맞춘다.
- S3 prefix contract를 `platform/s3/README.md`, `docs/ENVIRONMENT.md`와 맞춘다.
- ClickHouse schema를 `infra/clickhouse/initdb`와 맞춘다.
- Redis key TTL과 cache invalidation 규칙을 정한다.
- market/news/fundamentals event에 `asOf`, `artifactUri`, `schemaVersion`을 포함한다.
- Alpaca 401/403/429/500 retry/backoff 테스트를 만든다.
- S3 PUT 실패, ClickHouse unavailable, Kafka consumer lag failure test를 만든다.

## Handoffs

- 1번 AI: `EvidenceItem`, `NewsEvent`, fundamentals summary, staleness 의미를 맞춘다.
- 2번 Frontend: candle/indicator/event projection shape을 맞춘다.
- 4번 Infra: pod/job/image/topic/S3 prefix/env/secret 변경을 배포 경로에 반영한다.
- 5번 Backend: Chart API와 market-data query facade가 읽을 projection contract를 맞춘다.
