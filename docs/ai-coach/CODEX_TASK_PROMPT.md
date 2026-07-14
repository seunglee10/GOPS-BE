# GOPS AI 투자 코치 1페이지 Codex 작업 프롬프트

GOPS AI 투자 코치 1페이지 작업을 기존 저장소와 완전히 분리된 Git worktree에서 진행해라.

## 1. 작업 목표

이번 작업은 `POST MARKET > AI 투자 코치`의 1페이지인 `당일 거래 회고`를 실제 AWS 데이터 흐름에 연결하고 완성하는 작업이다.

단순히 오늘의 체결 내역을 요약하는 화면이 아니다.

오늘 사용자가 내린 매수·매도 판단을 과거 유사 거래와 비교하고, 손익과 별개로 어떤 확인 절차를 놓쳤는지 분석하며, 현재 포트폴리오에 미친 영향과 이후 매도·관찰 조건까지 제공해야 한다.

다른 AI 코치 페이지는 이번 범위에서 재설계하지 않는다. 페이지 전환 구조가 이미 있다면 유지하되 1페이지만 구현한다.

명시적으로 요청하기 전에는 commit과 push를 하지 마라.

---

## 2. 저장소와 작업 폴더

원본 저장소:

`/Users/heejunkim/Desktop/kim hee jun/gops`

새 worktree:

`/Users/heejunkim/Desktop/kim hee jun/gops-ai-coach`

새 브랜치:

`codex/ai-coach-page1`

시각 프로토타입:

`/Users/heejunkim/.codex/visualizations/2026/07/04/019f2ad9-49e1-79f0-b9ea-8bcf46444703/ai-coach-page1-prototype.html`

원본 저장소의 기존 변경사항을 절대 reset, checkout, restore, revert하지 마라. 원본 저장소에서 파일을 수정하지 말고 새 worktree에서만 작업한다.

먼저 다음을 확인한다.

1. 원본 저장소의 현재 브랜치와 HEAD
2. 기존 worktree 목록
3. `codex/ai-coach-page1` 브랜치 존재 여부
4. `/Users/heejunkim/Desktop/kim hee jun/gops-ai-coach` 존재 여부

대상 branch/worktree가 없다면 현재 HEAD를 기준으로 생성한다.

예상 명령:

```sh
git -C "/Users/heejunkim/Desktop/kim hee jun/gops" worktree add \
  -b codex/ai-coach-page1 \
  "/Users/heejunkim/Desktop/kim hee jun/gops-ai-coach" \
  HEAD
```

이미 존재한다면 삭제하거나 덮어쓰지 말고 상태를 확인한 후 재사용한다.

셸 출력이 너무 커져 `Output exceeded the available model context`가 발생하지 않도록 다음 방식을 사용한다.

- `git status --short`
- 범위를 제한한 `rg`
- `sed -n`
- `git diff --stat`
- 파일별 `git diff -- <path>`

---

## 3. 반드시 먼저 읽을 문서

새 worktree에서 다음 문서를 먼저 읽어라.

- `AGENTS.md`
- `docs/README.md`
- `docs/AGENT_ARCHITECTURE.md`
- `docs/AGENT_BACKEND_INTEGRATION.md`
- `docs/AGENT_FRONTEND_INTEGRATION.md`
- `docs/AGENT_AWS_BUILD.md`

현재 코드와 위 문서를 최우선 source of truth로 사용한다.

기존 대화나 오래된 설계가 현재 코드 또는 문서와 충돌하면 임의로 구조를 변경하지 말고 `docs/ai-coach/HANDOFF.md`에 충돌 내용을 기록한다.

---

## 4. 프로토타입 보존

시각 프로토타입 HTML을 production 코드로 직접 import하거나 실행하지 마라.

다음 위치에 참고 자료로 보존한다.

`docs/ai-coach/reference/ai-coach-page1-prototype.html`

프로토타입은 다음 사항만 참고한다.

- 정보 위계
- 화면 밀도
- 차트 배치
- 유사 사례 전환 방식
- 판단 누락 표시
- 포트폴리오 영향
- 매도·관찰 조건

기존 GOPS 디자인 시스템, 색상, 글꼴, 패널 구조를 우선한다.

---

## 5. 1페이지의 정확한 목적

1페이지는 `오늘 체결 요약`이 아니라 다음 질문에 답해야 한다.

1. 오늘 어떤 거래를 했는가?
2. 거래 당시 무엇을 확인했고 무엇을 놓쳤는가?
3. 과거에 비슷한 판단을 한 적이 언제 있었는가?
4. 과거 유사 사례에서는 어떤 결과와 실수가 발생했는가?
5. 오늘 손익과 별개로 판단 과정이 적절했는가?
6. 오늘 거래가 포트폴리오 집중도와 현금 비중에 어떤 영향을 줬는가?
7. 이후 어떤 가격·거래량·실적 조건에서 매도하거나 다시 관찰해야 하는가?

수익이 났다는 이유만으로 좋은 판단으로 평가하면 안 된다. 손실이 났다는 이유만으로 나쁜 판단으로 평가해서도 안 된다.

`판단 과정`과 `결과 손익`을 명확히 분리한다.

---

## 6. 화면 구성

모든 내용은 하나의 AI 투자 코치 패널 안에 표시한다.

금지 사항:

- 페이지 내부에 또 다른 거대한 카드형 패널 중첩
- 별도 라우트 생성
- 각 섹션별 독립 API 호출
- 섹션별 Kafka 작업 생성
- 의미 없는 설명 카드 반복
- 화면을 채우기 위한 가짜 데이터

권장 화면 순서:

### A. 오늘 거래 헤더

기본 선택 종목은 오늘 체결이 발생한 종목이다. 오늘 여러 종목을 거래했다면 상단에서 종목을 전환할 수 있어야 한다.

표시 항목:

- 회사 로고 또는 기존 GOPS 기업 아이콘
- 회사명
- 심볼
- 매수 또는 매도
- 체결 시각
- 평균 체결가
- 체결 수량
- 현재가
- 현재 수익률
- 거래 전 종목 비중
- 거래 후 종목 비중
- 다음 실적 일정
- 실적일까지 남은 기간

실적 일정이 없으면 추정하지 말고 `일정 확인 불가`로 표시한다.

### B. 오늘 판단

한 문장으로 핵심 판단을 제공한다.

예: “현재 수익은 발생했지만 RSI 과열과 실적 D-3을 확인하지 않은 추격 매수였습니다.”

다음 정보를 함께 표시한다.

- 판단 등급: 양호 / 주의 / 위험 / 데이터 부족
- 결과 손익
- 과정 평가
- 판단 근거
- 판단 근거의 데이터 기준시각

### C. 오늘 거래와 유사 사례 차트

오늘 거래를 기본으로 표시한다. 과거 유사 거래는 최대 6건까지 제공한다. 좌우 버튼 또는 사례 선택 컨트롤로 전환한다.

사례를 전환하면 다음이 함께 바뀌어야 한다.

- 차트
- 진입 시점
- 당시 확인하지 않은 조건
- 당시 실수
- 당시 결과
- 오늘 판단과의 차이
- MFE
- MAE
- 진입 후 수익률
- 보유 기간

차트는 실제 시장 데이터 또는 검증된 저장 데이터만 사용한다. 가짜 시장 캔들 또는 임의 랜덤 라인을 만들지 마라.

차트 기준:

- 진입 전 구간
- 진입 시점
- 진입 후 구간
- 진입 시점을 명확한 수직선 또는 마커로 표시
- 오늘 경로와 유사 사례 경로를 구분 가능한 라인으로 렌더링
- 비교가 목적이면 진입 시점을 0%로 정규화
- 원본 가격과 정규화 수익률을 혼동하지 않도록 계약에 명시

오늘 거래 차트와 유사 사례 차트의 시간축이 다르면 진입 시점을 기준으로 상대 시간축을 사용한다.

예: `T-30`, `T-15`, `Entry`, `T+15`, `T+30`, `T+60`

### D. 차트 위 판단 누락 마커

RSI, MACD, 거래량, 지지·저항 등 사용자가 확인하지 않은 조건을 진입 시점 주변에 빨간 마커로 표시한다.

각 마커는 다음 정보를 가진다.

- 누락 조건명
- 당시 값
- 기준값
- 왜 확인이 필요했는지
- 근거 데이터 출처
- 기준시각

마커에 hover 또는 focus하면 tooltip으로 세부 근거를 표시한다.

예:

- `RSI 72`: 과열 구간 진입
- `MACD 약화`: 상승 모멘텀 둔화
- `상대 거래량 0.7`: 거래량 확인 부족
- `저항선 근접`: 직전 고점까지 1.2%
- `실적 D-3`: 실적 발표 직전 변동성 위험

### E. 확인 항목

다음 네 영역을 기본 축으로 사용한다.

- 차트
- 뉴스
- 재무
- 시장

필요하면 각 영역 아래 세부 항목을 둔다.

차트: RSI, MACD, 거래량, 추세, 지지·저항

뉴스: 기업 뉴스, 산업 뉴스, 실적 관련 뉴스, 규제 또는 지정학 이슈

재무: 다음 실적일, 최근 실적, 매출·이익 추세, 밸류에이션 변화

시장: 지수 방향, 섹터 방향, 상대 거래량, 금리·환율 또는 주요 매크로 변수

각 항목은 다음 상태 중 하나를 사용한다.

- 확인
- 미확인
- 데이터 부족
- 해당 없음

`뉴스`, `실적 D-3` 같은 태그에 마우스를 올리면 실제 제목, 일정, 수치, 출처, 기준시각을 표시한다.

### F. 과거 유사 거래 회고

선택한 과거 사례마다 다음을 표시한다.

- 거래일
- 심볼
- 매수 또는 매도
- 유사도 점수
- 진입 후 수익률
- MFE
- MAE
- 보유 기간
- 당시 놓친 판단 조건
- 당시 가장 큰 실수
- 오늘 거래와 같은 점
- 오늘 거래와 다른 점

단순히 “비슷한 차트”만 찾지 않는다.

### G. 포트폴리오 영향

오늘 거래 전후를 비교한다.

표시 항목:

- 해당 종목 비중
- 해당 섹터 비중
- 현금 비중
- 상위 종목 집중도
- 상관 종목 합산 비중
- 변동성 기여도
- 단일 종목 위험
- 섹터 집중 위험

예:

```text
NVDA 비중: 12% → 18%
반도체 비중: 54% → 61%
현금 비중: 14% → 9%
```

결과 설명:

- 단일 종목 위험 증가
- 반도체 섹터 집중도 상승
- 현금 완충력 감소

정보가 없으면 추정하지 말고 `계산되지 않음`으로 표시한다.

### H. 매도 및 관찰 기준

일반적인 조언이 아니라 현재 포지션에 연결된 조건을 제공한다.

예:

- 일봉 종가가 190.80달러 아래에서 마감
- 상대 거래량이 1.2 이상으로 회복
- 다음 저항 203~206달러 접근
- 실적 발표 전 비중 재검토
- RSI가 과열 구간에서 이탈
- MACD 하락 전환
- 섹터 상대강도 약화

각 조건에는 다음을 포함한다.

- 조건 종류
- 현재값
- 임계값
- 판단 이유
- 추천 행동
- 알람 생성 가능 여부

알람은 사용자가 명시적으로 추가할 때만 생성한다. 자동 주문 또는 자동 청산을 실행하지 않는다.

---

## 7. 과거 유사 거래 선정 기준

유사 사례는 LLM이 임의로 고르지 않는다. 결정론적 Similarity Engine이 계산한다.

유사도 계산 후보:

- 매수·매도 방향
- 동일 종목
- 동일 산업 또는 섹터
- 시장 추세
- 종목 추세
- RSI 구간
- MACD 상태
- 거래량 상태
- 실적일까지 남은 기간
- 뉴스 이벤트 상태
- 진입 당시 포트폴리오 집중도
- 현금 비중
- 변동성 구간
- 진입 위치와 지지·저항 거리

가중치는 한 곳에서 관리하고 결과에 구성 점수를 남긴다.

```text
directionScore
symbolIndustryScore
marketRegimeScore
trendMomentumScore
volumeScore
eventScore
portfolioStateScore
indicatorScore
totalSimilarityScore
```

미래 데이터 사용을 금지한다.

유사도 계산에는 해당 거래의 진입 시점까지 알 수 있었던 정보만 사용한다. 진입 후 가격은 유사도 선정 이후 성과 평가에만 사용한다.

동일 거래가 자기 자신과 비교되지 않도록 한다. 최대 6건까지만 반환한다. 조건을 만족하는 사례가 없으면 숫자를 채우지 말고 `유사 사례 부족`으로 표시한다.

---

## 8. Snapshot 정책

분석 요청 하나마다 불변 입력 스냅샷 하나만 만든다. 여러 개의 중복 snapshot 저장 구조를 만들지 마라.

단일 `CoachInputSnapshot` 내부에 다음 섹션을 둔다.

```text
CoachInputSnapshot
- request
- user
- fills
- positionsBefore
- positionsAfter
- portfolioBefore
- portfolioAfter
- marketContext
- chartContext
- indicatorContext
- newsContext
- fundamentalsContext
- earningsContext
- ontologyContext
- sourceAsOf
- missingData
```

각 원천의 기준시각은 `sourceAsOf`에 기록한다.

분석 재현과 감사를 위해 필요한 경우에만 완성된 입력 스냅샷을 S3에 하나 저장한다. 각 에이전트가 원천 데이터를 다시 조회하거나 독립 snapshot을 따로 만들면 안 된다.

---

## 9. 멀티 에이전트와 결정론적 엔진

한 번의 분석 요청에서 다음 역할을 조합한다.

### Snapshot Builder

체결, 포지션, 시장, 뉴스, 재무, 실적, 온톨로지를 동일 기준시각의 단일 입력으로 정규화한다.

### Current Trade Reviewer

오늘 거래, 현재 포지션, 손익, 판단 절차를 평가한다.

### Similar Trade Analyzer

과거 유사 거래를 결정론적으로 선정하고 MFE, MAE, 진입 후 수익률, 보유 기간을 계산한다.

### Portfolio Impact Analyzer

거래 전후 포트폴리오 비중과 집중도 변화를 계산한다.

### Condition Planner

매도 및 관찰 조건 후보를 계산한다.

### Narrative Synthesizer

이미 계산된 결과를 사용자에게 이해하기 쉬운 문장으로 설명한다.

LLM은 다음을 계산하면 안 된다.

- 수익률
- MFE
- MAE
- 유사도
- 표본 수
- 포트폴리오 비중
- 집중도
- 임계값 충족 여부
- 신뢰도
- 우선순위

LLM은 설명, 요약, 문장 표현만 담당한다.

---

## 10. AWS 백엔드 흐름

현재 AWS Agent 계약을 보존한다.

```text
Frontend
-> POST /api/agents/analyze
-> 202 + analysisId
-> Kafka agents.analysis-requests.v1
-> agent-analysis-worker
-> Snapshot Builder
-> deterministic Coach Analytics Engine
-> Narrative Synthesizer
-> AnalysisReport + CoachReport
-> Redis report store
-> polling or SSE
-> Frontend page 1
```

request handler 안에서 `AgentOrchestrator.analyze()`를 직접 호출하지 않는다.

기존 idempotency, polling, SSE 계약을 깨지 않는다.

페이지 내부 컴포넌트가 개별 API를 직접 호출하지 않게 한다. 상위 컨테이너가 단일 `CoachReport`를 받아 props로 전달한다.

---

## 11. CoachReport 1페이지 계약

기존 `AnalysisReport`에 버전이 있는 `coachReport`를 추가하거나, 현재 코드에 이미 계약이 있다면 이를 확장한다.

최소 구조:

```ts
type CoachReport = {
  contractVersion: string
  analysisId: string
  generatedAt: string
  sourceAsOf: Record<string, string | null>
  page1: DailyTradeReview | null
  missingData: MissingDataItem[]
  warnings: string[]
}

type DailyTradeReview = {
  selectedFillId: string | null
  trades: TodayTradeSummary[]
  decisionAssessment: DecisionAssessment
  currentCase: TradeCase
  similarCases: SimilarTradeCase[]
  checklist: DecisionChecklist
  portfolioImpact: PortfolioImpact
  watchConditions: WatchCondition[]
  proposedAlerts: ProposedAlert[]
  confidence: ConfidenceSummary
}
```

`TodayTradeSummary` 최소 필드:

```text
fillId
symbol
companyName
side
filledAt
averageFillPrice
quantity
currentPrice
currentReturnPercent
weightBefore
weightAfter
earningsAt
earningsDaysRemaining
```

`SimilarTradeCase` 최소 필드:

```text
caseId
tradeDate
symbol
side
similarityScore
similarityComponents
entryPrice
exitPrice
returnPercent
mfePercent
maePercent
holdingDuration
normalizedSeries
missedChecks
mistakeSummary
sameAsToday
differentFromToday
```

`DecisionChecklist`:

```text
chart
news
fundamentals
market
```

각 항목은:

```text
status
label
checkedAt
evidence
source
sourceAsOf
```

`PortfolioImpact`:

```text
symbolWeightBefore
symbolWeightAfter
sectorWeightBefore
sectorWeightAfter
cashWeightBefore
cashWeightAfter
topHoldingsConcentrationBefore
topHoldingsConcentrationAfter
riskFlags
```

`WatchCondition`:

```text
id
type
label
currentValue
threshold
operator
reason
recommendedAction
alertSupported
```

---

## 12. 데이터 부족 정책

production에서 데이터가 없을 때 가짜 값을 넣지 않는다.

다음 상태를 사용한다.

- 데이터 부족
- 표본 부족
- 확인 기록 없음
- 계산되지 않음
- 일정 확인 불가
- 유사 사례 부족
- 신뢰도 낮음
- 데이터 연결 대기

dev fixture는 production 경로와 완전히 분리한다.

권장 조건:

```ts
import.meta.env.DEV
&& VITE_AI_COACH_DEV_FIXTURE === "true"
```

production build에서는 fixture가 활성화되지 않아야 한다.

fixture는 UI 계약 확인용으로만 사용한다. Redis, ClickHouse, Kafka, 시장 데이터 API에 저장하지 않는다.

차트 fixture가 필요하면 무작위 캔들을 생성하지 말고 고정된 테스트용 정규화 시계열로 분리하고 화면에 `DEV FIXTURE`임을 표시한다.

---

## 13. 프론트 구현 기준

기존 AI 투자 코치 컴포넌트와 라우팅을 먼저 조사한다.

기존 구조를 재사용하되 다음을 지킨다.

- 1페이지 상태를 다른 페이지 상태와 분리
- report 데이터를 props로 전달
- 유사 사례 index는 로컬 UI 상태로 관리
- 사례 전환 시 차트와 설명을 원자적으로 변경
- hover뿐 아니라 keyboard focus에서도 tooltip 접근 가능
- 좁은 패널과 넓은 패널 모두 대응
- 텍스트가 잘리거나 겹치지 않도록 함
- 차트는 container resize를 감지해 자동으로 크기 조정
- 패널 내부에 고정 pixel 높이를 남발하지 않음
- reduced-motion 환경을 지원
- 애니메이션은 사례 전환과 차트 갱신을 이해시키는 수준으로 제한

기존 GOPS 아이콘 시스템이 있으면 이를 사용한다. 임의 SVG 아이콘을 새로 만들지 않는다.

---

## 14. 문서 산출물

다음을 작성한다.

### `docs/ai-coach/HANDOFF.md`

포함 내용:

- 작업 목적
- 1페이지 사용자 질문
- 화면 구조
- 데이터 원천
- 단일 snapshot 정책
- 결정론적 계산 항목
- LLM 역할
- CoachReport 계약
- AWS 흐름
- 데이터 부족 정책
- dev fixture 정책
- 미구현 또는 외부 의존 사항
- 테스트와 실행 방법
- 기존 코드·문서와 충돌한 내용

### `docs/ai-coach/reference/ai-coach-page1-prototype.html`

제공된 프로토타입을 시각 참고 자료로 보존한다.

관련 계약이 실제로 변경되면 다음 문서도 함께 갱신한다.

- `docs/AGENT_ARCHITECTURE.md`
- `docs/AGENT_BACKEND_INTEGRATION.md`
- `docs/AGENT_FRONTEND_INTEGRATION.md`
- `docs/AGENT_AWS_BUILD.md`

계약이 바뀌지 않았다면 불필요한 문서 수정을 만들지 않는다.

---

## 15. 테스트

최소한 다음을 검증한다.

### 결정론적 계산 테스트

1. 동일 입력은 항상 동일한 유사도 결과를 생성한다.
2. 미래 데이터가 유사도 계산에 포함되지 않는다.
3. 자기 거래가 유사 사례에 포함되지 않는다.
4. 최대 6건까지만 반환한다.
5. MFE 계산이 올바르다.
6. MAE 계산이 올바르다.
7. 진입 후 수익률 계산이 올바르다.
8. 포트폴리오 거래 전후 비중 계산이 올바르다.
9. 손익과 판단 과정 평가가 분리된다.
10. 데이터 부족 시 숫자를 임의 생성하지 않는다.

### 프론트 테스트

1. 오늘 체결 종목이 기본 선택된다.
2. 오늘 여러 거래가 있을 때 종목을 전환할 수 있다.
3. 유사 사례를 좌우로 전환할 수 있다.
4. 사례 전환 시 차트와 실수 설명이 함께 변경된다.
5. RSI/MACD 누락 마커가 표시된다.
6. 뉴스·실적 태그 tooltip이 표시된다.
7. production에서 dev fixture가 사용되지 않는다.
8. 좁은 패널에서 텍스트와 차트가 잘리지 않는다.
9. 데이터 부족 상태가 정상 표시된다.

### 기존 계약 회귀 테스트

- `POST /api/agents/analyze`
- report polling
- SSE
- idempotency
- AnalysisReport parsing

---

## 16. 검증 명령

저장소의 실제 package script를 먼저 확인하고 존재하는 명령만 사용한다.

최소 검증:

```text
git diff --check
frontend TypeScript typecheck
frontend test:ai-coach 또는 관련 테스트
frontend production build
API agent route tests
agent worker/analytics tests
```

Python 검증은 저장소 루트의 `.venv`와 Python 3.12만 사용한다. 별도 가상환경을 만들지 않는다.

로컬 UI 확인 시 Alpaca 실시간 ingestor를 실행하지 않는다. dev fixture 또는 이미 존재하는 API 데이터만 사용한다.

가능하면 로컬 서버를 실행하고 다음을 직접 확인한다.

- 1페이지 기본 렌더링
- 유사 사례 6건 전환
- 차트 resize
- tooltip
- 데이터 부족 상태
- 좁은 패널
- 넓은 패널

스크린샷으로 시각 검증까지 수행한다.

---

## 17. 완료 보고 형식

작업 완료 시 다음만 간결하게 보고한다.

1. 생성한 worktree와 branch
2. 수정한 주요 파일
3. 구현된 기능
4. 결정론적 계산 범위
5. fixture와 production 분리 방식
6. 실행한 테스트와 결과
7. 아직 실제 AWS 데이터가 필요한 항목
8. 로컬 확인 URL
9. commit과 push를 하지 않았다는 사실

중간 분석에서 멈추지 말고 문서, 계약, UI, fixture, 테스트, 빌드 검증까지 완료한다.

단, 원본 저장소의 기존 변경사항은 절대 수정하거나 되돌리지 마라. 명시적으로 요청하기 전에는 commit과 push를 하지 마라.
