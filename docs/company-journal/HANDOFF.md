# AI 기업저널 Handoff

## 목적

기업별 기존 뉴스·주가·재무·실적 예상·관계 데이터를 장 마감 뒤 가볍게 해석해, 키워드와
자연어로 보여 주는 독립 panel이다. AI 투자 코치의 사용자 거래 snapshot과는 다른 기능이다.

## 데이터 흐름

```text
ClickHouse 원천 serving tables
-> deterministic source bundle/digest/metrics
-> OpenAI natural-language writer
-> server validation
-> append-only ClickHouse verified report
-> GET /api/company-journal/{symbol}
-> AI 기업저널 panel
```

- Redis, PostgreSQL, Kafka, S3를 사용하지 않는다.
- 원문 snapshot을 저장하지 않고 news id, SEC accession, Yahoo 실적 예상 기준시각, 가격 기준일,
  graph relation version만 남긴다.
- API GET은 OpenAI를 호출하지 않는다. stale이면 생성 event만 추가한다.
- 수익률·시장 대비·재무 지표는 서버가 계산하고 OpenAI는 문장만 작성한다.
- production에서 결과가 없으면 추정하지 않고 pending을 표시한다.

## 화면과 v2 계약

- 탭은 `매출·수익 / 실적 / 안정성 / 가치`이며 기업저널 안의 독립 뉴스 탭은 없다.
- `company-journal.v2`의 자연어 탭은 `current`, `growth`, `profitability`, `earnings`,
  `stability`, `valuation`이다. 이전 v1 저장 행은 그대로 읽으며 없는 `earnings` 문장은
  `current`로 안전하게 대체한다.
- 실적 탭은 SEC 실제치와 Yahoo 예상치를 비교하고, 기존 캔들 API의 최대 2년 일봉으로
  종목·S&P 500·섹터 ETF 상대수익률 및 거래량을 표시한다.
- AWS replay simulation이 일반 `/api/market/*`를 point-in-time 안전 때문에 차단해도 기업저널은
  전용 읽기 전용 `/api/company-journal/{symbol}/evidence`에서 저장된 SEC/Yahoo/일봉 근거를
  한 번에 읽는다. 주문·추천·agent·일반 차트의 simulation guard는 그대로 유지한다.
- 오른쪽 설명에 focus/hover하면 관련 차트 계열이 강조된다. 용어는 공통 사전 tooltip을 사용한다.
- 로컬 고정 fixture는 `DEV + companyJournalPreview=1`에서만 활성화되고 production fallback은 없다.

## AWS 원천 준비

- SEC companyfacts: 매일 20:30 UTC, ClickHouse 분기 재무 시계열과 안정성 차트 원천
- Yahoo estimates: 매일 21:15 UTC, ClickHouse `yahoo_earnings_estimates`의 EPS·매출 예상치 원천
- 기업저널: 평일 23:30 UTC enqueue, 10분 worker가 verified v2 report 추가
- 비교 일봉: 배포 시 멱등 benchmark bootstrap Job이 SPY·8개 섹터 ETF의 2년 `1D` 누락분만 추가
- 기업저널 source bundle은 최대 520개 일봉과 최근 42개월 실적 근거를 읽는다. UI는 같은 기간을
  `/api/charts/candles`에서 읽으며 공급자가 짧은 이력만 반환하면 실제 구간만 표시하고 부족 상태를 알린다.

## 저장과 배포

DDL은 `infra/clickhouse/initdb/03-company-journal.sql`이며 기존 table을 변경하지 않는다.
AWS에서는 backend rollout 전에 `company-journal-migrations` Job을 실행한다. 10분 worker는
panel 요청을 처리하고 평일 post-market CronJob은 최대 100개 최근 종목을 예약한다.

## 확인 명령

```sh
/Users/heejunkim/Desktop/kim\ hee\ jun/gops/.venv/bin/python -m pytest -q systems/api-server/tests/test_company_journal.py systems/api-server/tests/test_market_data_query.py
pnpm --dir apps/gops-frontend build
git diff --check
kubectl kustomize infra/k8s/overlays/aws-incluster-app-ci
```
