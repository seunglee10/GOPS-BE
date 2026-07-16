# AWS 추천 알고리즘 데이터 준비 체크리스트

이 문서는 2026년 7월 14일 정규장 마감 시점까지 이용 가능한 데이터로
7월 15일 정규장 추천을 생성하기 위한 데이터 준비·검증 책임을 정의한다.
추천 알고리즘 데이터만 다루며 시뮬레이터, 주문 생성, 주문 실행은 범위에 포함하지
않는다.

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
| 사용자 `recommendationStyle` | 준비 | migration 적용 필요 | `0011_personalized_recommendations.sql` 적용 기록 |
| 추천 시점 이전 portfolio snapshot history | 준비 | history coverage 검증 필요 | 사용자별 최신 `source_as_of`, 누락률 |
| 후보·SPY 완료 1D 260개 | 조회 경로 준비 | 미검증 | 종목별 22/252 거래일 coverage |
| 직전 정규장 1분봉 | 조회 경로 준비 | 7월 13·14일 검증 필요 | 종목별 390개 및 SPY coverage |
| 20일 거래대금·변동성 | 계산 준비 | 원천 coverage 미검증 | feature null/제외 보고서 |
| 52주 고가 | 계산 준비 | 252일 일봉 coverage 미검증 | 종목별 high52 산출 결과 |
| cutoff-safe 뉴스 | 시간 필터·중복 제거 준비 | `published_at/received_at` 정확성 미검증 | cutoff 위반 0건, 중복률 |
| 섹터 분산 | 계산 준비 | canonical 섹터 누락률 검증 필요 | `Unclassified` 종목 목록 |
| 상관관계·한계 변동성 | 계산 준비 | 보유종목 60일 일봉 coverage 미검증 | 사용자별 계산 가능 비율 |
| 현금·총자산 | freshness별 사용 준비 | 계좌 필드 단위/통화 검증 필요 | 필드 mapping과 표본 reconciliation |
| 가중치/model version | v1 prior·registry schema 준비 | registry 승인 운영 절차 미준비 | 승인자, cutoff, OOS 보고서 |
| 추천 outcome/label | outcome schema 준비 | label 산출 job 미준비 | next-session excess label job 실행 기록 |
| 실제 order execution | V2 조회·멱등 처리 준비 | `orders.user_sub`·`executions.payload` coverage 검증 필요 | filled/partial fill 표본과 qty·price mapping |
| V2 후보 feature snapshot | 저장·24시간 매칭 준비 | `0012` migration 적용 필요 | run별 후보 수와 feature digest |
| 연속 preference state | buy-only decay·softmax 준비 | `0012` migration 적용 필요 | 사용자별 state version·event 처리 결과 |
| 연속 risk state | snapshot·buy/sell 추론 준비 | 시장가치 snapshot coverage 검증 필요 | cost-basis 제외 및 preset cap 검증 |

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
| SEC Companyfacts 원천 | ClickHouse `sec_financial_facts` 적재 경로 존재 | 부분 준비 | AWS 실제 symbol·period coverage 검증 |
| EPS, 매출, 순이익, 자본, 자산, 부채, 현금흐름 | canonical fact mapping 존재 | 부분 준비 | cutoff snapshot과 단위·통화 검증 |
| ROE, margin, growth, FCF, 부채·유동성 비율 | `sec_derived_metrics` 계산 경로 존재 | 부분 준비 | TTM·period version 고정 및 coverage 검증 |
| Yahoo EPS·매출 consensus | 별도 latest collector/table 존재 | 부분 준비 | 수집 cutoff 검증 |
| BPS | 명시적 추천 derived metric 없음 | 미준비 | common equity/기간말 보통주 수 계산 |
| TTM EPS·순이익·FCF | 추천용 immutable TTM snapshot 없음 | 미준비 | quarterly/FY 중복 없는 TTM builder |
| `fundamentals_as_of(symbols, cutoff)` | batch 계약 없음 | 미준비 | 추천 전용 batch reader/projection |
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
| 미착수 | AWS 담당자 | `0012_continuous_recommendation_v2.sql` | order DB migration job으로 적용한다. | `schema_migrations` 적용 행 |
| 미착수 | AWS 담당자 | orders/executions | 실제 filled/partial fill의 user, symbol, side, qty, price, 시각을 검증한다. | 익명화 표본과 누락률 |
| 미착수 | AWS 담당자 | portfolio history | 최근 90일 시장가치 snapshot과 `valuationBasis`를 검증한다. | 사용자별 reliable snapshot 수 |
| 미착수 | AWS 담당자 | company provider | `snapshots_as_of(symbols, cutoff)`와 snapshot/version/digest를 검증한다. | cutoff 위반 0건·coverage 보고서 |
| 미착수 | AWS 담당자 | API·worker runtime | 두 workload에 동일한 `RECOMMENDATION_ALGORITHM_VERSION=continuous-v2`를 적용한다. | 배포 revision과 env 확인 |
| 미착수 | AWS 담당자 | 첫 V2 run | actual rank, preference/risk digest, 후보 feature 저장을 확인한다. | run ID와 SQL 검증 결과 |
| 미착수 | AWS 담당자 | 실제 매수 체결 | 다음 refresh에서 event 1회 처리와 state version 증가를 확인한다. | execution/event/state 연결 결과 |
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
- [ ] production 반영 전 shadow/OOS 승인 보고서가 존재한다.
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
