# AWS 추천 알고리즘 데이터 준비 체크리스트

이 문서는 추천 알고리즘 데이터 준비·검증 책임을 정의한다. 1절 이후의 2026년 7월
14일 cutoff 시나리오는 재현 가능한 품질 검증 기준으로 유지하고, 실제 AWS의 현재
준비 상태와 지금 해야 할 작업은 0절을 우선한다. 추천 알고리즘 데이터만 다루며
시뮬레이터, 주문 생성, 주문 실행은 범위에 포함하지 않는다.

## 0. 현재 AWS 실측 결과와 결론

2026년 7월 16일 재점검에서 SPY는 총 252개 1D candle을 보유했지만 7월 14일 replay
cutoff(7월 13일까지)에는 251개뿐이었다. 7월 13일 직전 정규장 SPY는 389개로
"약 390개" 하한 380을 충족하고, 7월 14일 SPY 정규장은 390개다. 따라서
`recommendation_v3_fixture.py extract`는 현재 의도대로 fixture 생성을 거절한다.
최소 한 개의 더 오래된 실제 SPY 완료 일봉을 복구한 뒤 extractor를 다시 실행해야 하며,
이 결과를 legacy/V3 성공 증거로 해석하면 안 된다.

복구 wrapper는 기본 dry-run이다. 검토 후에만 `APPLY=true`로 실행한다.

```bash
./scripts/aws/restore-spy-recommendation-data.sh
APPLY=true ./scripts/aws/restore-spy-recommendation-data.sh
```

이 절은 2026년 7월 16일 18:51 KST에 `gops-eks-cluster`의
`alfaka-market-data` namespace를 읽기 전용으로 점검한 결과다. 개인별 row나 Secret
값은 조회하지 않고 deployment image, ConfigMap key 존재 여부, migration 이름과 aggregate
count만 확인했다. 이후 배포나 적재가 있었다면 아래 검증 명령으로 다시 측정해야 한다.

### 0.1 지금 바로 내릴 수 있는 결론

- 머지와 push만 완료되었고 현재 EKS에는 아직 V2 image가 배포되지 않았다.
  - 확인 당시 repository HEAD: `f75f247`
  - live backend/recommendation-worker image: `edc1744-20260716090042`
- live ConfigMap에는 `RECOMMENDATION_ALGORITHM_VERSION`,
  `RECOMMENDATION_PERSONALIZATION_ENABLED`, `RECOMMENDATION_PERSONALIZATION_SHADOW`가 없다.
  현재 코드 기본값으로는 legacy scorer가 실행된다.
- PostgreSQL에는 `0004_recommendations.sql`, `0006_ai_coach.sql`만 확인되었고
  `0011_personalized_recommendations.sql`, `0012_continuous_recommendation_v2.sql`은 없다.
- SPY의 완료 1D와 직전 정규장 1m가 모두 0건이므로 professional-v1과 continuous-v2의
  전문 팩터 계산은 현재 모든 후보에서 차단된다.
- 후보군 일봉은 대체로 준비되어 있지만 직전 정규장 1m coverage는 부족하다.
- 포트폴리오 history는 존재하지만 30일 범위를 충족하지 않고 모든 row에
  `valuationBasis`가 빠져 있다. 현재 상태로는 V2 strict risk inference의 승인 근거가
  아니다.
- 소유자가 있는 KIS order는 있으나 execution이 0건이다. 현재 개인화는 실제 fill로
  학습할 수 없고 cold-start prior만 사용할 수 있다.
- SEC actual/derived 데이터는 준비되어 있지만 Yahoo estimate history는 0건이고,
  추천용 `FundamentalSnapshotProvider` production adapter/wiring이 없다. 따라서 지금은
  9팩터 fallback만 활성화할 수 있다.

### 0.2 실측 요약

| 영역 | 2026-07-16 실측 | V2 영향 | 판정 |
| --- | ---: | --- | --- |
| EKS recommendation-worker | 1/1 Ready, image `edc1744-20260716090042` | 머지된 V2 코드가 아직 없음 | 차단 |
| V2 selector key | live ConfigMap에 없음 | default legacy | 차단 |
| `0011` / `0012` | `schema_migrations`에 없음 | V2 state/ledger/table 사용 불가 | 차단 |
| 투자 프로필 | 6 rows | `0011` 적용 후 기존 row는 balanced style 기본값 사용 가능 | 부분 준비 |
| 포트폴리오 history | 825 rows, 7 users | 표본 수는 있으나 최대 span 1일 | 차단 |
| `valuationBasis` | missing 825 rows | 시장가치 여부를 증명하지 못함 | 차단 |
| owned KIS orders | 3 rows | 사용자 scope는 일부 존재 | 부분 준비 |
| executions | 0 rows | canonical fill/history seed 없음 | 개인화 학습 차단 |
| 1D candles | 504 symbols, 500 symbols가 252일 이상 | 후보 일봉은 대부분 준비 | 준비 |
| SPY 1D | 0 rows | 9팩터 계산 전체 차단 | 차단 |
| 직전 정규장 1m | 3 symbols, 390개 충족 1 symbol | 대부분 후보 계산 차단 | 차단 |
| SPY 직전 정규장 1m | 0 rows | 상대강도 계산 전체 차단 | 차단 |
| 최근 7일 뉴스 | 7,723 rows, 2,555 symbols, `received_at` 누락 0 | news factor 입력 가능 | 준비 |
| SEC facts | 1,730,992 rows, 502 symbols, 19 metrics | provider 원천으로 사용 가능 | 부분 준비 |
| SEC derived | 4,888,902 rows, 502 symbols, 14 metrics | provider 원천으로 사용 가능 | 부분 준비 |
| Yahoo estimates | 0 rows | earnings revision 생성 불가 | 차단 |
| production fundamental provider | 코드에 adapter/wiring 없음 | `fundamentalWeight=0` fallback | 선택 기능 차단 |

### 0.3 우선순위별 필요한 추가 작업

#### P0: V2 코드와 schema 배포

dev/test 배포 workflow는 push만으로 자동 실행되지 않는다. GitHub Actions의
`Deploy dev/test to EKS`를 수동 실행하거나 다음과 같이 실행한다.

```bash
gh workflow run deploy-dev.yml \
  --ref dev \
  -f services=backend,order-worker
```

`backend`는 `gops-backend`, `recommendation-worker`, `alert-evaluator`를 같은 API image로
배포한다. `order-worker` 선택은 order migration Job을 선행시켜 `0011`과 `0012`를 app
rollout 전에 적용한다. 배포 후 다음을 확인한다.

```bash
kubectl -n alfaka-market-data get deployment recommendation-worker gops-backend -o wide

kubectl -n alfaka-market-data exec postgres-0 -- \
  psql -U gops -d gops -Atc \
  "select filename from schema_migrations
   where filename in (
     '0011_personalized_recommendations.sql',
     '0012_continuous_recommendation_v2.sql'
   ) order by filename;"
```

두 migration 이름이 모두 출력되기 전에는 V2 selector를 켜지 않는다.

#### P0: SPY와 직전 정규장 1m 복구

continuous-v2는 후보와 SPY 모두에 대해 완료 일봉과 직전 정규장 1분봉이 필요하다.
먼저 기존 operator Job을 dry-run한다.

```bash
SYMBOLS=SPY \
INTERVALS=1m,1D \
LOOKBACK_DAYS=400 \
WAIT_FOR_JOB=true \
./scripts/aws/run-session-candle-rebuild-job.sh

INTERVALS=1m \
LOOKBACK_DAYS=7 \
MAX_SYMBOLS=0 \
WAIT_FOR_JOB=true \
./scripts/aws/run-session-candle-rebuild-job.sh
```

Job image, 요청 범위, Alpaca rate limit과 예상 insert를 검토한 후 같은 명령에
`APPLY=true`를 추가한다. 첫 명령은 SPY 1D/1m를, 두 번째 명령은 전체 S&P 500 최근
정규장 1m를 보강한다. 누락된 1D 후보가 계속 있으면 `INTERVALS=1D`,
`LOOKBACK_DAYS=400`으로 별도 실행한다.

현재 `ALPACA_UNIVERSE=sp500` registry에는 SPY가 포함되지 않는다. 일회성 backfill만 하면
다음 세션에 다시 SPY가 비게 되므로 다음 중 하나를 durable contract로 추가해야 한다.

1. 권장: 시장 수집기에 UI 종목 universe와 분리된 benchmark symbol 설정을 추가하고
   `SPY`를 bars/updatedBars/dailyBars에 항상 구독한다.
2. 임시: 장 마감 후 SPY `1m,1D`를 보강하는 bounded CronJob을 운영한다.

SPY를 S&P 500 UI universe 파일에 임의로 넣어 heatmap 구성종목처럼 취급하지 않는다.

#### P0: 9팩터 V2 활성화

배포와 candle 검증이 끝난 뒤 수동 workflow가 실제로 렌더링하는
`infra/k8s/overlays/aws-incluster-app/configmap-incluster-patch.yaml`에 다음 값을 추가한다.
별도 `aws` overlay도 계속 운영한다면
`infra/k8s/overlays/aws/configmap-aws-patch.yaml`에도 같은 값을 유지한다.

```yaml
RECOMMENDATION_ALGORITHM_VERSION: "continuous-v2"
```

API와 recommendation-worker가 같은 ConfigMap을 사용해야 한다. `continuous-v2`는 shadow를
무시하고 실제 `score`와 순서를 바꾼다. 펀더멘털 provider가 없어도 9팩터 fallback으로
실행되므로 fundamental 작업 완료를 기다릴 필요는 없다.

#### P1: 시장가치 포트폴리오 history 축적

현재 history row는 모두 `valuationBasis`가 없고 시간 span도 최대 1일이다. producer가
다음 최소 payload를 명시적으로 저장하도록 보완한다.

```json
{
  "valuationBasis": "market_value",
  "sourceAsOf": "2026-07-16T09:00:00Z",
  "account": {
    "totalValueForeign": 100000,
    "cashForeign": 20000
  },
  "positions": [
    {
      "symbol": "AAPL",
      "marketValueForeign": 10000,
      "sector": "Information Technology"
    }
  ]
}
```

현재 저장은 authenticated `GET /api/account/holdings` 호출 시
`upsert_portfolio_snapshot()`으로 이루어진다. 사용자가 화면을 열었을 때만 수집하는
방식으로는 30일 coverage를 보장하지 못하므로, 사용자별 KIS holdings를 최소 일 1회
수집하는 scheduler를 추가하는 것이 권장된다. 변동성·drawdown·turnover V2 추론에는
90일 window 안에서 30일 이상에 걸친 신뢰 가능한 시장가치 관측 20개가 필요하다.

기존 `valuationBasis` 누락 row를 일괄 `market_value`로 덮어쓰지 않는다. 원본 KIS 응답과
가격 기준시각으로 시장가치였음을 증명할 수 있는 row만 별도 backfill한다. 준비 전에는
V2가 기존 V1 portfolio-fit/risk blend fallback을 사용한다.

#### P1: 실제 KIS fill 학습 데이터 축적

`0012` 적용 후 KIS reconciliation이 양의 누적 체결량과 실제 체결가를 확인하면
`order_coach_fill_history`에 자동으로 append한다. 필요한 전제는 다음과 같다.

- authenticated order의 `orders.user_sub`가 null이 아닐 것
- payload에 양의 cumulative filled quantity와 실제 average/fill price가 있을 것
- `order-reconciler`가 실행될 것
- paper/simulator order가 아닐 것

현재 execution이 0건이므로 historical seed는 없다. V2 첫 run이 candidate feature를
저장한 뒤 발생한 KIS buy fill부터 preference가 갱신된다. 테스트용 가짜 fill을 AWS DB에
직접 insert하지 않는다. KIS demo 주문도 paper/simulator table이 아닌 기존 KIS order와
reconciliation 경로를 통과하면 canonical ledger 검증에 사용할 수 있다.

#### P2: 펀더멘털 overlay 연결

현재 SEC facts/derived는 충분한 원천 coverage가 있지만 다음 두 항목은 없다.

1. `yahoo_earnings_estimates` append-only history와 30일 earnings revision
2. API와 recommendation-worker에 설치되는 production
   `FundamentalSnapshotProvider` adapter

provider는 원시 SEC row를 추천 서비스 안에서 다시 계산하지 않고, 외부 producer가 만든
4개 0–100 합성점수를 한 번의 cutoff-safe batch로 반환해야 한다.

```json
{
  "snapshotId": "fundamental-20260716T090000Z",
  "schemaVersion": "fundamentals.v1",
  "featureVersion": "fundamental-factors.v1",
  "digest": "sha256:...",
  "sourceAsOf": "2026-07-16T09:00:00Z",
  "snapshots": {
    "AAPL": {
      "value": 72.1,
      "quality": 83.4,
      "growth": 61.0,
      "earningsRevision": 55.2,
      "coverage": 0.95,
      "freshness": 0.90,
      "sourceQuality": 1.0,
      "sourceAsOf": "2026-07-16T09:00:00Z"
    }
  }
}
```

현재 SEC schema의 `filed_at`은 날짜 정밀도이므로 같은 날 장중/장후 cutoff를 증명하지
못한다. `accepted_at` 또는 검증된 `available_at`이 추가되기 전에는 cutoff 당일 공시를
보수적으로 제외한다. provider adapter가 준비되기 전에도 V2는 정상 동작하지만
`fundamentalStatus`가 fallback이고 `fundamentalWeight=0`이다.

### 0.4 재점검 순서

배포 또는 데이터 보강 뒤에는 다음 순서로 승인한다. 사용자별 raw row나 Secret 값을
운영 증빙에 남기지 않고 aggregate count와 version/digest만 기록한다.

1. backend와 recommendation-worker가 같은 새 image인지 확인한다.
2. `schema_migrations`에서 `0011`, `0012` 적용을 확인한다.
3. ClickHouse에서 SPY 1D와 직전 정규장 SPY/후보 1m coverage를 확인한다.
4. API와 worker의 selector가 모두 `continuous-v2`인지 확인한다.
5. 첫 run의 `algorithm_version`, 전체 후보 feature 수, Top 15, input digest를 확인한다.
6. 이후 실제 KIS buy fill이 있을 때 canonical fill/event/state가 한 번만 증가하는지
   확인한다.

초기 활성화 승인선은 **새 image + `0011`/`0012` + SPY/후보 candle + selector + 첫 V2
run**이다. 포트폴리오 30일 history, 실제 fill history, fundamental provider는 V2 품질을
높이는 후속 데이터이며, 없을 때 각각 기존 risk fallback, cold-start prior, 9팩터
fallback을 사용한다.

## 기준과 범위

| 항목 | 기준 |
| --- | --- |
| 추천 기준 시각 | 2026-07-14 16:00 ET |
| 추천 대상 세션 | 2026-07-15 정규장 |
| 입력 데이터 마감 | 추천 기준 시각 이후 이용 가능해진 시장·뉴스·공시·컨센서스 데이터는 입력에서 제외 |
| 기본 캔들 | 미국 주식 정규장 1분봉 |
| 기준 종목 | 추천 대상 전체 종목과 SPY |
| canonical 가격 정책 | `price_adjustment=split`, `canonical_version=v2` |
| 완료 판단 | 이 문서의 완료 기준과 최종 승인 체크리스트를 모두 충족 |

완료 여부는 `미착수`, `진행 중`, `완료`, `차단` 중 하나로 기록한다. 실제 API 키,
DB 비밀번호, AWS Access Key, Secret 값은 이 문서나 완료 증빙에 기록하지 않는다.

# 1. 사용자가 생성하여 전달할 데이터

사용자는 7월 13일 데이터를 CSV로 재가공하지 않고 기존 DB 스키마와 데이터 타입을
유지한 추출본으로 전달한다. AWS 담당자는 전달된 원본을 변경하지 않고 staging에서
검증한 뒤 canonical 적재 여부를 결정한다.

## 1.1 사용자 전달 체크리스트

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | 사용자 | 기존 DB | 2026년 7월 13일 정규장 1분봉을 기존 스키마 그대로 추출한다. | 추출 파일 또는 DB dump 위치 |
| 미착수 | 사용자 | 추천 대상 목록 | 추천 대상 전체 종목과 SPY가 추출 범위에 포함되었는지 확인한다. | 종목 목록과 종목 수 |
| 미착수 | 사용자 | 원본 테이블 | 원본 테이블 DDL 또는 전체 컬럼·타입 정의를 제공한다. | DDL 파일 또는 schema 문서 |
| 미착수 | 사용자 | 추출 작업 | 데이터 추출 쿼리 또는 dump 생성 명령과 적용 조건을 제공한다. | 실행 기록 또는 query 파일 |
| 미착수 | 사용자 | 추출 메타데이터 | 데이터 생성 시각, 데이터 기준 시각, 원본 DB 및 테이블 이름을 기록한다. | 전달 manifest |
| 미착수 | 사용자 | 추출 파일 | 파일별 체크섬과 파일 크기를 생성한다. | SHA-256 체크섬 목록 |
| 미착수 | 사용자 | 압축 파일 | 압축 형식, 문자 인코딩, 시간대 표현 방식을 기록한다. | 전달 manifest |
| 미착수 | 사용자 | 품질 예외 | 누락, 수정, 거래정지 또는 데이터 이상이 알려진 종목을 별도 목록으로 작성한다. | 예외 종목 목록 |
| 미착수 | 사용자 | 전달 패키지 | 데이터와 문서에 자격 증명 또는 Secret이 포함되지 않았는지 검사한다. | 보안 확인 기록 |

## 1.2 필수 캔들 필드

사용자 추출본에는 다음 필드가 존재해야 한다. 원본 컬럼명이 다르면 원본명과 canonical
필드의 매핑표를 함께 전달한다.

```text
symbol
event_time
interval
open
high
low
close
volume
trade_count
vwap
is_closed
market_session
price_adjustment
canonical_version
source
inserted_at
```

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | 사용자 | 원본 스키마 | 필수 필드 존재 여부와 데이터 타입을 확인한다. | 필드 매핑표 |
| 미착수 | 사용자 | `event_time` | UTC 또는 명시적인 offset을 보존하고 원본 시간대를 기록한다. | 시간대 정의 |
| 미착수 | 사용자 | `interval` | 전달 대상 행이 `1m`인지 확인한다. | interval별 행 수 |
| 미착수 | 사용자 | `market_session` | 전달 대상 행이 `regular`인지 확인한다. | session별 행 수 |
| 미착수 | 사용자 | `is_closed` | 완료된 캔들만 전달 범위에 포함한다. | 미완료 캔들 수 0건 |
| 미착수 | 사용자 | 가격 조정 필드 | 조정 여부를 추정하거나 임의 변경하지 않고 원본 값을 보존한다. | adjustment별 행 수 |

## 1.3 사용자 전달 기준

- 정규장 범위는 09:30–16:00 ET로 한다.
- SPY를 반드시 포함한다.
- 가격과 거래량은 동일한 adjustment 계열에서 추출한다.
- 원본 데이터의 결측치, 중복, 수정 이력을 숨기지 않는다.
- 추천 기준 시각 이후에 발생한 데이터 수정이 있으면 수정 시각을 함께 전달한다.
- 원본 추출본, DDL, 추출 기준, 체크섬, 예외 목록을 하나의 전달 단위로 관리한다.

# 2. AWS 담당자가 준비·검증할 작업

## 2.1 데이터 수신과 원본 보관

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | AWS 담당자 | 사용자 전달 패키지 | 사용자 업로드 전용 S3 수신 경로와 최소 권한 접근 정책을 준비한다. | S3 경로와 권한 검토 기록 |
| 미착수 | AWS 담당자 | 추출 파일 | 수신 파일을 변경하지 않고 원본 영역에 보존한다. | 원본 object key와 version ID |
| 미착수 | AWS 담당자 | 체크섬 목록 | 수신 파일의 SHA-256과 파일 크기를 사용자 제공 값과 비교한다. | 무결성 검증 결과 |
| 미착수 | AWS 담당자 | DDL·manifest | 스키마, 필드 타입, 인코딩, 압축 형식과 시간대 정의를 검토한다. | 스키마 검토 결과 |
| 미착수 | AWS 담당자 | 검증 대상 파일 | 운영 ClickHouse와 분리된 staging에서만 초기 검증과 변환을 수행한다. | staging 작업 ID |
| 미착수 | AWS 담당자 | 수신·검증 기록 | 적재 작업 ID, 수신 시각, 원본 위치, 담당자와 검증 결과를 기록한다. | 적재 manifest 또는 작업 보고서 |

## 2.2 7월 13일 데이터 품질 검증

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | AWS 담당자 | 종목 목록·캔들 | 추천 대상 전체 종목과 SPY 포함 여부를 검증한다. | 포함·누락 종목 목록 |
| 미착수 | AWS 담당자 | 정규장 캔들 | 종목별 예상 캔들 수와 실제 캔들 수를 비교한다. | 종목별 coverage 보고서 |
| 미착수 | AWS 담당자 | `event_time` | 09:30–16:00 ET 범위와 UTC 변환 결과를 검증한다. | 세션 경계 검증 결과 |
| 미착수 | AWS 담당자 | candle key | `symbol + interval + event_time` 중복을 검사한다. | 중복 0건 또는 해소 목록 |
| 미착수 | AWS 담당자 | OHLCV | `low <= open/close <= high`, `low <= high` 관계를 검사한다. | OHLC 오류 목록 |
| 미착수 | AWS 담당자 | 가격·거래량 | 0 이하 가격과 음수 거래량을 검사한다. | 유효성 검사 결과 |
| 미착수 | AWS 담당자 | `is_closed` | 미완료 캔들을 canonical 후보에서 제외한다. | 제외 행 수 |
| 미착수 | AWS 담당자 | `market_session` | 정규장 데이터의 세션 오분류를 검사한다. | 세션 오류 목록 |
| 미착수 | AWS 담당자 | 7월 13·14일 데이터 | 7월 14일 AWS 종목 유니버스와 7월 13일 전달 데이터를 대조한다. | 공통·누락·추가 종목 목록 |
| 미착수 | AWS 담당자 | SPY 캔들 | SPY timestamp가 추천 후보 캔들과 동일한 기준으로 정렬되는지 확인한다. | benchmark 정합성 결과 |

## 2.3 가격 조정과 기업행동 검증

AWS canonical 역사 데이터는 다음 계약을 유지한다.

```text
price_adjustment=split
canonical_version=v2
```

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | AWS 담당자 | 사용자 adjustment 값 | 사용자 데이터의 실제 가격 조정 정책을 확인한다. | adjustment 분포와 원본 설명 |
| 미착수 | AWS 담당자 | raw/unknown 행 | raw 또는 unknown 데이터를 canonical로 직접 승인하지 않는다. | 격리·재처리 결과 |
| 미착수 | AWS 담당자 | Alpaca 역사 데이터 | `adjustment=split` 재조회 결과와 표본 또는 전체 데이터를 비교한다. | 비교 대상·오차 보고서 |
| 미착수 | AWS 담당자 | 가격·거래량 | split-adjusted 가격과 raw 거래량이 혼합되지 않았는지 확인한다. | adjustment pair 검증 결과 |
| 미착수 | AWS 담당자 | 기업행동 데이터 | 분할, 역분할, 배당, 합병, spin-off와 symbol 변경 이력을 확보한다. | 기업행동 목록과 수집 시각 |
| 미착수 | AWS 담당자 | 기업행동 전후 캔들 | effective/ex-date 전후 가격 단절과 비정상 수익률을 검사한다. | 경계 검증 결과 |
| 미착수 | AWS 담당자 | 배당 데이터 | 배당 조정 또는 total-return 계열은 canonical split 캔들과 분리한다. | 별도 정책·저장 위치 |

7월 15일 open-to-close 추천 목표에는 현금배당 total-return 조정이 필수 입력이 아니다.
향후 close-to-close 또는 다일 보유 수익률을 사용할 때만 별도 total-return 정책을
승인한다.

## 2.4 모델 입력 데이터 준비

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | AWS 담당자 | 7월 13·14일 캔들 | eligible universe 전체의 정규장 캔들 가용성을 증명한다. | 날짜·종목별 coverage |
| 미착수 | AWS 담당자 | 20거래일 OHLCV | 거래대금 기준선과 변동성 산출이 가능한지 확인한다. | 20일 feature readiness |
| 미착수 | AWS 담당자 | 252거래일 일봉 | 52주 최고가 feature 산출이 가능한지 확인한다. | 252일 coverage |
| 미착수 | AWS 담당자 | 최소 2년 feature history | 월별 학습과 walk-forward OOS 검증 구간이 point-in-time 기준으로 완전한지 확인한다. | 날짜·feature별 completeness 보고서 |
| 미착수 | AWS 담당자 | SPY 캔들 | 후보 종목과 동일 세션·adjustment의 benchmark를 준비한다. | SPY coverage |
| 미착수 | AWS 담당자 | 종목 이력 | 추천 기준 시각별 point-in-time universe를 준비한다. | universe snapshot 위치 |
| 미착수 | AWS 담당자 | GraphDB/종목 metadata | canonical 섹터와 산업 분류를 검증한다. | 미분류 종목 목록 |
| 미착수 | AWS 담당자 | 뉴스 데이터 | `available_at <= cutoff`를 검증할 수 있는 뉴스와 수집 시각을 준비한다. | 뉴스 coverage·중복 보고서 |
| 미착수 | AWS 담당자 | quote 데이터 | 과거 bid/ask 또는 spread 기반 비용 입력의 가용성을 확인한다. | quote/spread coverage |
| 미착수 | AWS 담당자 | feature 정의 | 버전이 부여된 feature/label 데이터셋 저장 위치를 준비한다. | dataset URI와 schema version |
| 미착수 | AWS 담당자 | model metadata | 학습 cutoff, feature 정의, 계수와 검증 보고서를 관리할 registry를 준비한다. | model registry 위치 |
| 미착수 | AWS 담당자 | 추천 결과 | 추천 결과 측정용 outcome 테이블과 데이터 계약을 준비한다. | table DDL과 ownership |

## 2.5 개인화 추천 추가 데이터와 현재 준비 상태

코드에 구현된 `professional-personalization-v1`과 `continuous-personalization-v2`는
아래 입력을 요구한다. 이 표의
`구현 준비`는 저장·계산 경로가 코드에 존재한다는 뜻이며, AWS 운영 데이터의 실제
coverage가 승인되었다는 뜻은 아니다.

| 데이터/계약 | 코드 준비 | AWS 데이터 준비 | AWS 완료 증빙 |
| --- | --- | --- | --- |
| 사용자 `recommendationStyle` | 준비 | live `0011` 미적용; profile 6 rows | `0011_personalized_recommendations.sql` 적용 기록 |
| 추천 시점 이전 portfolio snapshot history | 준비 | 825 rows지만 `valuationBasis` 전부 누락, 최대 span 1일 | 사용자별 90일 표본 수·span·시장가치 basis |
| 후보·SPY 완료 1D 260개 | 조회 경로 준비 | 후보 500/504는 252일 이상, SPY 0 rows | 후보별 252일과 SPY coverage |
| 직전 정규장 1분봉 | 조회 경로 준비 | 3 symbols만 존재, 390개 충족 1 symbol, SPY 0 rows | 종목별 390개 및 SPY coverage |
| 20일 거래대금·변동성 | 계산 준비 | 원천 coverage 미검증 | feature null/제외 보고서 |
| 52주 고가 | 계산 준비 | 252일 일봉 coverage 미검증 | 종목별 high52 산출 결과 |
| cutoff-safe 뉴스 | 시간 필터·중복 제거 준비 | `published_at/received_at` 정확성 미검증 | cutoff 위반 0건, 중복률 |
| 섹터 분산 | 계산 준비 | canonical 섹터 누락률 검증 필요 | `Unclassified` 종목 목록 |
| 상관관계·한계 변동성 | 계산 준비 | 보유종목 60일 일봉 coverage 미검증 | 사용자별 계산 가능 비율 |
| 현금·총자산 | freshness별 사용 준비 | 계좌 필드 단위/통화 검증 필요 | 필드 mapping과 표본 reconciliation |
| 가중치/model version | v1 prior·registry schema 준비 | registry 승인 운영 절차 미준비 | 승인자, cutoff, OOS 보고서 |
| 추천 outcome/label | outcome schema 준비 | label 산출 job 미준비 | next-session excess label job 실행 기록 |
| canonical KIS fill | `order_coach_fill_history` projection·멱등 처리 준비 | live `0012` 미적용, executions 0 rows | partial/final fill의 증가 qty·price·시각과 replay 결과 |
| V2 후보 feature snapshot | 저장·24시간 매칭 준비 | live `0012` 미적용 | run별 전체 후보 수와 feature digest |
| 연속 preference state | buy-only decay·softmax 준비 | live `0012` 미적용, 학습할 fill 없음 | 사용자별 state version·event 처리 결과 |
| 연속 risk state | snapshot·buy/sell 추론 준비 | 시장가치 30일 coverage 없음 | cost-basis 제외 및 preset cap 검증 |

AWS 담당자는 15분 이내 스냅샷은 전체 포트폴리오 적합도에, 15분 초과 24시간 이내
스냅샷은 보유·상관·집중도에만 사용할 수 있음을 검증한다. 24시간 초과는
`portfolioDataStale`, 스냅샷 없음은 style-only로 기록되어야 한다. 추천 run에는 전체
계좌 payload를 복제하지 않고 사용한 history ID와 파생 context만 저장한다.

## 2.6 회사 펀더멘털 데이터 handoff와 준비 상태

회사정보 담당자는 화면 표시용 최신 EPS·재무정보만 전달하는 것이 아니라, 추천 시점에
실제로 이용 가능했던 정보만 재현하는 batch snapshot 계약을 제공해야 한다. AWS 담당자는
이를 수신·보관·검증하고 추천 feature dataset과 연결한다.

### 2.6.1 현재 준비·부분 준비·미준비

| 데이터/계약 | 현재 상태 | 판정 | 필요한 작업 |
| --- | --- | --- | --- |
| SEC Companyfacts 원천 | ClickHouse 1,730,992 rows, 502 symbols, 19 metrics | 부분 준비 | cutoff별 symbol·period coverage 검증 |
| EPS, 매출, 순이익, 자본, 자산, 부채, 현금흐름 | canonical fact mapping 존재 | 부분 준비 | cutoff snapshot과 단위·통화 검증 |
| ROE, margin, growth, FCF, 부채·유동성 비율 | ClickHouse 4,888,902 rows, 502 symbols, 14 metrics | 부분 준비 | TTM·period version 고정 및 coverage 검증 |
| Yahoo EPS·매출 consensus | table/collector 경로는 있으나 live 0 rows | 미준비 | append-only 수집과 cutoff 검증 |
| BPS | 명시적 추천 derived metric 없음 | 미준비 | common equity/기간말 보통주 수 계산 |
| TTM EPS·순이익·FCF | 추천용 immutable TTM snapshot 없음 | 미준비 | quarterly/FY 중복 없는 TTM builder |
| `snapshots_as_of(symbols, cutoff)` | 검증 interface와 fallback 구현됨 | 부분 준비 | production adapter를 API·worker에 동일하게 wiring |
| 당일 공시 시각 | 주요 fact의 `filed_at`이 날짜 정밀도 | 미준비 | `accepted_at` 또는 증명 가능한 `available_at` 보존 |
| 과거 consensus revision | latest replacement 구조 | 미준비 | append-only consensus snapshot |
| sector-neutral fundamental feature | 추천 dataset 없음 | 미준비 | Value·Quality·Growth·Revision feature 생성 |
| model/OOS report | 펀더멘털 overlay 승인 보고서 없음 | 미준비 | walk-forward shadow 검증 |

여기서 `부분 준비`는 코드와 저장 경로가 있다는 뜻이며, 2026년 7월 14일 cutoff 기준 AWS
데이터가 실제로 완전하다는 의미가 아니다.

### 2.6.2 회사정보 담당자 전달 체크리스트

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 미착수 | 회사정보 담당자 | SEC fact/derived schema | 원천·파생 테이블 DDL과 metric 정의를 제공한다. | DDL과 data dictionary |
| 미착수 | 회사정보 담당자 | Batch reader | 전체 추천 후보를 cutoff 한 번으로 조회하는 계약을 제공한다. | 함수/API schema와 표본 payload |
| 미착수 | 회사정보 담당자 | 공시 metadata | `period_end`, `filed_at`, `accepted_at/available_at`, `inserted_at`, accession을 제공한다. | 시각 필드 정의와 표본 |
| 미착수 | 회사정보 담당자 | TTM builder | quarterly/FY 중복 없이 EPS·순이익·매출·FCF TTM을 계산한다. | 계산식·fixture·재현 테스트 |
| 미착수 | 회사정보 담당자 | Equity/shares | common equity와 기간말 보통주 수로 BPS를 계산한다. | basic/diluted·NCI·우선주 정책 |
| 미착수 | 회사정보 담당자 | Consensus history | cutoff 이전 consensus와 30일 전 snapshot을 조회할 수 있게 한다. | append-only history 위치 |
| 미착수 | 회사정보 담당자 | Quality metadata | restatement, synthetic Q4, NCI 포함, split basis를 표시한다. | quality code 목록 |
| 미착수 | 회사정보 담당자 | Coverage output | cutoff 기준 누락 symbol·metric·period를 출력한다. | coverage 보고서 |
| 미착수 | AWS 담당자 | 전달 schema·표본 | staging에서 schema, 단위, 통화, 시간대와 key uniqueness를 검증한다. | staging 검증 결과 |
| 미착수 | AWS 담당자 | Cutoff 표본 | 7월 14일 16:00 ET 이후 공시·수집값이 제외되는지 검증한다. | cutoff 위반 0건 또는 격리 목록 |
| 미착수 | AWS 담당자 | Snapshot output | immutable snapshot ID, feature version과 digest를 저장한다. | dataset URI와 digest manifest |
| 미착수 | 추천 알고리즘 담당자 | Fundamental features | 원시 EPS/BPS가 아닌 4개 합성 팩터 정의와 결측 fallback을 승인한다. | feature specification version |

### 2.6.3 추천용 batch snapshot 계약

```text
snapshots_as_of(
    symbols,
    cutoff_timestamp
)
```

필수 cutoff 조건은 다음과 같다.

```text
available_at <= cutoff_timestamp
accepted_at <= cutoff_timestamp                 # 값이 있을 때
inserted_at <= immutable_recommendation_cutoff
consensus_collected_at <= cutoff_timestamp
```

필수 필드군은 다음과 같다.

| 필드군 | 필수 내용 |
| --- | --- |
| 식별 | `symbol`, CIK, fiscal year/period, period end, form, accession |
| 이용 가능 시각 | `filed_at`, `accepted_at`, `available_at`, `inserted_at`, `version_filed_at` |
| 원천값 | TTM 순이익·매출·영업이익·영업현금흐름·FCF, 자본, 기간말 보통주 수 |
| 표시용 per-share | 희석 EPS TTM, BPS, basic/diluted fallback |
| 비교용 비율 | earnings yield, book-to-market, FCF yield, ROE, margin, leverage |
| 성장 | 매출·순이익·EPS YoY |
| Consensus | EPS consensus, 수집 시각, 30일 revision, surprise, analyst count |
| 품질 | currency, source, restatement, synthetic Q4, NCI, negative EPS/equity, split basis |
| 재현성 | snapshot ID, schema version, feature version, source digest |

### 2.6.4 추천 feature 계산 정책

원시 EPS와 BPS 자체를 횡단면 점수로 사용하지 않는다.

```text
EarningsYield = 보통주 귀속 TTM 순이익 / cutoff 시가총액
BookToMarket  = 보통주 자본 / cutoff 시가총액
FCFYield      = TTM FCF / cutoff 시가총액
BPS           = 보통주 자본 / 기간말 보통주 수      # 표시·검증용
```

추천 feature는 다음 네 합성축으로 준비한다.

```text
fundamentalValue
fundamentalQuality
fundamentalGrowth
earningsRevision
```

- 업종 또는 섹터 내 percentile로 정규화한다.
- 극단값은 승인된 winsorization 정책을 적용한다.
- 음수 EPS는 무한 PER로 변환하지 않고 loss flag로 분리한다.
- 음수 자본은 PBR/Book-to-Market 점수에서 제외하고 risk flag로 남긴다.
- 누락값을 0점으로 대체하지 않고 이용 가능한 구성요소 가중치를 재정규화한다.
- coverage가 임계값 미만이면 펀더멘털 overlay를 0으로 만들고 기존 9개 시장 팩터로 복귀한다.
- 은행·보험·REIT·pre-revenue 기업은 별도 sector template을 사용하거나 부적절한 metric을
  제외한다.

### 2.6.5 역사 보존 요구사항

`ReplacingMergeTree`의 latest row만으로 과거 추천을 재현한다고 가정하지 않는다.

- SEC 정정 전 값과 정정 후 값을 `available_at/version_filed_at` 기준으로 구분한다.
- 원본 Companyfacts와 accession은 S3 immutable object로 보존한다.
- Yahoo consensus는 `collected_at`을 key에 포함한 append-only snapshot을 보존한다.
- 30일 revision은 추천 cutoff 이전에 저장된 두 snapshot만으로 계산한다.
- 같은 날 장후 공시의 정확한 이용 가능 시각을 증명할 수 없으면 7월 14일 cutoff 입력에서
  제외한다.
- feature dataset은 source row ID와 digest를 역추적할 수 있어야 한다.

### 2.6.6 펀더멘털 overlay 활성화 조건

V2 자체는 실제 순위를 만들지만, 다음 증빙이 승인되기 전에는 provider를 연결하지 않는다.
Provider가 없으면 V2는 `fundamentalWeight=0`으로 9개 시장 팩터 순위를 만든다.

- eligible universe의 symbol·metric·period coverage 보고서
- cutoff violation 0건 또는 자동 격리 증빙
- sector별 peer count와 결측률
- split·restatement·negative EPS/equity 경계 테스트
- 기존 `newsImpact`와 earnings revision의 중복/증분 성과 분석
- walk-forward OOS next-session excess return, turnover, 비용, sector exposure 보고서
- 누락 시 기존 추천으로 결정론적으로 복귀하는 테스트
- 승인된 fundamental model version과 최대 overlay 비중

## 2.7 V2 AWS 관리자 적용 체크리스트

이 절차는 AWS manifest나 Terraform을 이 저장소에서 수정하라는 요청이 아니다. AWS
담당자가 현재 배포 방식에 맞춰 migration과 런타임 값을 적용하고 증빙을 남기는 절차다.

| 완료 여부 | 담당자 | 입력물 | 작업 내용 | 완료 증빙 |
| --- | --- | --- | --- | --- |
| 차단 | AWS 담당자 | merged API/order image와 `0011`/`0012` | 수동 deploy workflow로 image를 배포하고 migration job을 완료한다. | image tag와 두 `schema_migrations` 행 |
| 차단 | AWS 담당자 | SPY·후보 candle | SPY 1D와 직전 정규장 1m를 backfill하고 다음 세션 수집을 보장한다. | SPY/후보별 daily·390분 coverage |
| 차단 | AWS 담당자 | canonical KIS fill | `order_coach_fill_history`에서 실제 partial/final fill과 replay 멱등성을 검증한다. 현재 source execution은 0 rows다. | 익명화 aggregate와 qty·price mapping |
| 차단 | AWS 담당자 | portfolio history | 최소 일 1회 시장가치 snapshot을 30일 이상 축적하고 `valuationBasis`를 검증한다. | 사용자별 reliable snapshot 수·span |
| 미착수 | AWS 담당자·회사정보 담당자 | company provider | consensus history와 `snapshots_as_of(symbols, cutoff)` adapter/version/digest를 준비한다. | cutoff 위반 0건·coverage 보고서 |
| 차단 | AWS 담당자 | API·worker runtime | 두 workload에 동일한 `RECOMMENDATION_ALGORITHM_VERSION=continuous-v2`를 적용한다. | 배포 revision과 ConfigMap key 확인 |
| 차단 | AWS 담당자 | 첫 V2 run | actual rank, preference/risk digest, 전체 후보 feature 저장을 확인한다. | run ID와 SQL 검증 결과 |
| 미착수 | AWS 담당자 | 실제 KIS 매수 체결 | 다음 refresh에서 event 1회 처리와 state version 증가를 확인한다. | fill/event/state 연결 결과 |
| 미착수 | AWS 담당자 | rollback 절차 | 값을 `professional-v1` 또는 `legacy`로 되돌리는 절차를 검증한다. | rollback 실행 기록 |

API와 recommendation worker의 알고리즘 버전이 다르면 같은 slot의 digest와 추천 순위가
달라질 수 있으므로 반드시 동일하게 적용한다. V2는 shadow가 아니며 `continuous-v2`를
선택한 시점부터 `personalScore`가 실제 순서를 결정한다.

# 3. 책임 분리

| 항목 | 사용자 | AWS 담당자 |
| --- | --- | --- |
| 7월 13일 기존 DB 추출본 생성 | 책임 | 지원 |
| 원본 스키마와 추출 기준 제공 | 책임 | 검토 |
| S3 수신 경로와 접근 권한 준비 | 전달 | 책임 |
| 체크섬 및 파일 무결성 확인 | 제공 | 책임 |
| 캔들 완전성 검증 | 참고 | 책임 |
| 가격 조정 정책 확인 | 원본 정보 제공 | 책임 |
| Alpaca split-adjusted 데이터 비교 | 해당 없음 | 책임 |
| ClickHouse staging 적재 | 해당 없음 | 책임 |
| 운영 canonical 데이터 승인 | 해당 없음 | 책임 |
| 피처·레이블·모델 레지스트리 준비 | 요구사항 확인 | 책임 |
| 회사정보 cutoff batch 계약 제공 | 해당 없음 | 회사정보 담당자와 공동 책임 |
| 공시·컨센서스 point-in-time 검증 | 해당 없음 | 책임 |
| 펀더멘털 snapshot·feature digest 보관 | 해당 없음 | 책임 |
| 펀더멘털 shadow/OOS 보고서 | 요구사항 확인 | 작성·보관 |
| 최종 데이터 품질 보고서 승인 | 확인 | 작성 |

# 4. 완료 기준

다음 조건을 모두 만족해야 AWS 추천 데이터 준비가 완료된 것으로 판단한다.

- [ ] live backend와 recommendation-worker가 merged V2 image를 사용한다.
- [ ] `0011_personalized_recommendations.sql`과 `0012_continuous_recommendation_v2.sql`이 적용되었다.
- [ ] API와 recommendation-worker의 selector가 모두 `continuous-v2`다.
- [ ] 첫 V2 run이 전체 후보 feature, Top 15, algorithm/model version과 input digest를 원자적으로 저장한다.
- [ ] 7월 13일과 14일 모두 추천 대상 종목의 95% 이상이 완전한 정규장 데이터를 보유한다.
- [ ] SPY 정규장 데이터가 100% 존재한다.
- [ ] 중복 canonical candle이 없다.
- [ ] 시간대와 세션 분류 오류가 없다.
- [ ] 모든 추천 입력 가격과 거래량이 동일한 split-adjustment 정책을 사용한다.
- [ ] critical feature가 누락된 종목을 추천 대상에서 자동 제외할 수 있다.
- [ ] 펀더멘털이 누락되거나 cutoff를 증명할 수 없을 때 기존 시장 팩터로 복귀할 수 있다.
- [ ] 7월 14일 16:00 ET 이후 이용 가능해진 공시·정정·컨센서스가 제외되었다.
- [ ] TTM, BPS, earnings/book/FCF yield 계산식과 split·negative-value 정책이 versioned되어 있다.
- [ ] 펀더멘털 snapshot ID, feature version과 input digest가 재현 가능하다.
- [ ] 펀더멘털 provider를 production에 연결하기 전 overlay shadow/OOS 승인 보고서가 존재한다.
- [ ] 적재·변환·검증 결과가 재현 가능한 보고서로 남아 있다.
- [ ] 7월 15일 데이터가 추천 입력에 포함되지 않았음이 확인되었다.
- [ ] 데이터 품질 보고서가 `준비 완료`, `부분 준비`, `미준비`를 구분한다.
- [ ] 사용자와 AWS 담당자가 최종 데이터 품질 보고서를 확인했다.

## 최종 승인 기록

| 항목 | 기록 |
| --- | --- |
| 데이터 패키지 ID |  |
| 원본 S3 위치 |  |
| staging 작업 ID |  |
| 검증 보고서 위치 |  |
| 펀더멘털 snapshot ID |  |
| 펀더멘털 feature/model version |  |
| 펀더멘털 coverage·cutoff 보고서 |  |
| 추천 입력 cutoff | 2026-07-14 16:00 ET |
| 사용자 확인자·일시 |  |
| AWS 확인자·일시 |  |
| 최종 상태 | 미착수 |
