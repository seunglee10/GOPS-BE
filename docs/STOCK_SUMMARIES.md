# 종목 AI 요약·오늘 시장 요약

목업의 종목 AI 요약(D14)과 홈 '오늘 시장 요약'(D15)을 미리 만들어 두는 방식이다.
API 는 저장된 값만 읽고, 생성은 agent-orchestration 의 주기 잡이 맡는다. 응답 형태는 `MOCK_API_CONTRACT.md` D14·D15가 기준이다.

## 흐름

```text
stock-summary-refresh 잡 (매시 5분, CronJob)
  ├─ 대상 선택: 최근 24시간 뉴스가 있는 종목 + 시총 상위 50 (히트맵 스냅샷, 없으면 시드 파일)
  ├─ 종목마다: 최신 뉴스 8건 + 일봉 20개 + 다가올 실적일 → StockSummaryGenerator
  │    → Redis gops:stocks:ai-summary:{SYMBOL}  (TTL 24시간)
  └─ 미국 장 마감(16:15 ET, 조기 폐장일 13:15 ET) 뒤 첫 실행: 지수·업종·등락 상위·오늘 뉴스 → MarketBriefGenerator
       → Redis gops:market-brief:{YYYY-MM-DD}  (TTL 3일, 날짜는 미국 동부 기준 거래일)
       → S3 market-brief/v1/date={YYYY-MM-DD}/brief.json  (MARKET_BRIEF_S3_BUCKET 이 있을 때만)

GET /api/stocks/{symbol}/ai-summary   캐시가 없으면 404 {"detail":{"code":"summary_not_ready"}}
GET /api/market-brief/today           오늘 → 없으면 3일 안 가장 최근 → 없으면 404 {"detail":{"code":"brief_not_ready"}}
GET /api/stocks/movers                같은 종목 요약 캐시의 head·news[0].newsId·chartNote 를 issue·issueNewsId·chartNote 로 쓴다(D5)
```

## Redis 키

| 키 | 값 | TTL | 쓰는 곳 | 읽는 곳 |
| --- | --- | --- | --- | --- |
| `gops:stocks:ai-summary:{SYMBOL}` | D14 JSON | 24시간 | 갱신 잡 | D14 API, D5 movers |
| `gops:market-brief:{YYYY-MM-DD}` | D15 JSON | 3일 | 갱신 잡 | D15 API |

키에는 `REDIS_KEY_PREFIX` 를 붙이지 않는다. 코드 기준은 `gops_agents/summaries/keys.py`.
잡은 API 서버 워커가 써 둔 `{REDIS_KEY_PREFIX}:indices:snapshot|last`, `{REDIS_KEY_PREFIX}:heatmap:sp500[:last]` 을 재료로 읽는다.

## 생성 규칙

- 종목마다 1시간에 한 번만 다시 만든다(저장된 `generatedAt` 기준, `--force` 로 무시).
- 뉴스도 일봉도 없는 종목은 쓰지 않는다(API 는 404).
- LLM 은 OpenAI Responses API 의 strict JSON schema 로 부르고, 서버에서 다시 검증한다:
  해요체 문장, 입력에 있는 `newsId`·일정 날짜·참조 값만 허용, 가격 예측·매수/매도 권유 문구 금지.
- LLM 이 꺼져 있거나 실패·검증 실패하면 결정론적 템플릿으로 만든다.
  종목: 전일 대비 등락·20일 평균 대비 거래량·업종 방향 + 최근 기사 제목, `chartNote` 는 20일선 위/아래·거래량 배수·등락 방향.
  시장: 지수 등락, 10년물 금리·VIX 수준, 가장 강한/약한 업종, 등락 상위 종목, 많이 다룬 기사.

## 실행

```bash
# 로컬(템플릿만, API 키 불필요)
docker compose --profile agent-refresh run --rm stock-summary-refresh
# 지정 종목만 다시
docker compose --profile agent-refresh run --rm stock-summary-refresh python -u /app/systems/agent-orchestration/jobs/stock-summary-refresh/main.py --symbols NVDA,AMD --force
# 시장 요약만 지금(가장 최근 마감일 기준)
docker compose --profile agent-refresh run --rm stock-summary-refresh python -u /app/systems/agent-orchestration/jobs/stock-summary-refresh/main.py --brief
```

EKS 는 `infra/k8s/overlays/aws/scheduled/cronjob-stock-summary-refresh.yaml` (이미지 `gops-agent-orchestrator`, `schedule: "5 * * * *"` UTC).

## 환경 변수

| 이름 | 기본값 | 뜻 |
| --- | --- | --- |
| `STOCK_SUMMARY_LLM_ENABLED` | `false` | `true` 이고 `OPENAI_API_KEY` 가 있을 때만 LLM 사용. 로컬 기본은 템플릿만. CronJob 은 `true`. |
| `STOCK_SUMMARY_MODEL` | `OPENAI_MODEL` → `gpt-5.2` | 요약 모델 |
| `STOCK_SUMMARY_TIMEOUT_SECONDS` | `30` | OpenAI 요청 제한 시간 |
| `STOCK_SUMMARY_SYMBOLS` | 비움 | 지정하면 자동 선택 대신 이 종목만 |
| `STOCK_SUMMARY_TOP_MARKET_CAP` | `50` | 시총 상위 몇 개를 넣을지 |
| `STOCK_SUMMARY_NEWS_HOURS` | `24` | "최근 뉴스" 기준 시간 |
| `STOCK_SUMMARY_NEWS_LIMIT` | `8` | 종목당 넣는 기사 수 |
| `STOCK_SUMMARY_CANDLE_LIMIT` | `20` | 일봉 개수 |
| `STOCK_SUMMARY_MIN_INTERVAL_SECONDS` | `3600` | 같은 종목 재생성 최소 간격 |
| `STOCK_SUMMARY_MAX_SYMBOLS` | `300` | 한 번 실행에서 다루는 최대 종목 수 |
| `MARKET_BRIEF_S3_BUCKET` | 비움 | 있으면 시장 요약을 S3 에도 저장. CronJob 은 configmap `S3_BUCKET` |
| `MARKET_BRIEF_S3_PREFIX` | `market-brief/v1` | S3 경로 앞부분 |
| `REDIS_URL`, `REDIS_KEY_PREFIX`, `CLICKHOUSE_*` | 공통 | 저장소 접속 |
