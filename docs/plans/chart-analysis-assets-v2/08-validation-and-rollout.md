# 08. 검증, 품질 평가, 롤아웃

상태: 검토 대기 — 승인 상태는 README를 따름
대상: 00~07 문서 전체
원칙: **그림 수가 아니라, 실제 의사결정에 도움이 되는 그림의 정밀도를 우선한다.**

## 1. 완료 조건

v2는 아래 다섯 묶음이 모두 완료되고, 각 묶음의 검증과 커밋이 끝났을 때만 완료다.

### A. 계약·정규 데이터·기하

- v1 자산을 계속 읽으면서 v2 자산을 저장·서빙할 수 있다.
- 빌더와 차트 API가 동일한 정규 캔들 정의, 정렬, 집계 규칙을 사용한다.
- 모든 시간 기반 anchor는 실제 서빙 캔들의 `candleKey`에 대응한다.
- 잘못되거나 부족한 입력은 `quality.state="insufficient_data"`가 되고, eligible하지만 후보가
  없으면 `status="ready" + 빈 drawings + emptyReason`인 정상 빈 레이어가 된다.
- 같은 입력과 버전으로 반복 실행하면 byte-equivalent한 규칙 레이어와 후보 식별자가 나온다.

### B. 구조·이벤트·추세·범위

- 지지·저항, 추세선, 채널, 범위, 이벤트는 03·04의 hard gate를 먼저 통과한 뒤 순위가 매겨진다.
- 두 점만 이은 추세선, 최근 영향이 없는 화면 밖 추세선, 슬롯을 채우기 위한 레벨·범위는 생성되지 않는다.
- 추세선은 독립된 세 번 이상의 반응과 현재 관련성을 갖는다.
- 채널은 signed slope와 양쪽 경계 증거를 갖고, 수렴 구조를 평행 채널로 오인하지 않는다.
- 이벤트 flag의 backend anchor는 원인이 된 canonical 봉과 정확히 같고, 오래된 중복
  이벤트가 새 이벤트를 밀어내지 않는다. 실제 pixel 중앙 정합은 E에서 검증한다.
- 정상 빈 레이어가 성공 결과로 테스트된다.

### C. 에이전트·해설

- Layer I는 결정론적 후보 ID만 선택할 수 있고 좌표·가격·신뢰도를 창작할 수 없다.
- 한 symbol 빌드에 멀티 타임프레임 LLM 호출은 최대 1회다.
- LLM 실패·키 부재·스키마 오류 때 unit/integration 경계에서 S/T와 결정론적 해설 fallback이
  손실 없이 반환된다. 실제 저장·진행 상태는 D에서 검증한다.
- 해설은 실제 선택된 작도와 `focusItems[].drawingIds`로 연결되며,
  `주요 관찰 → 근거 → 확인 조건 → 무효화 조건`을 알려준다.
- 수치가 필요한 가격·조건·신뢰도는 서버가 검증된 후보에서 조립한다.
- Responses API 요청은 `store: false`, 엄격한 structured output, 제한된 입력·출력 예산을 사용한다.

### D. 파이프라인·저장·API

- symbol당 정규 1D 입력 쿼리 최대 1회, LLM 호출 최대 1회다.
- buildIntentDigest와 agent outcome predicate로 fast no-op을 증명하면 kernel·LLM·write를
  건너뛴다. kernel 뒤 최종 intent/outcome 동일성을 증명한 late intent no-op도 LLM·write를
  건너뛴다. curator를 실행한 뒤에만 알 수 있는 content 동일성은 write/cache invalidation만
  건너뛰며, content digest를 사전 LLM skip key로 쓰지 않는다.
- Redis에는 기존 job 상태 키 하나와 pubsub 이외의 자산 본문·후보가 저장되지 않는다.
- 새 Kafka topic이나 오케스트레이터 workflow/role/provider 변경 없이 독립 builder 경계를 유지한다.
- ClickHouse에는 compact final asset만 저장하고 원시 후보·프롬프트·응답을 저장하지 않는다.
- 부분 성공 상태와 interval별 오류가 API/job status에서 구분된다.
- LLM 장애는 eligible S/T와 deterministic commentary를 `saved_with_warning`으로 저장하고
  실패 항목/재시도 대상으로 오분류하지 않는다.
- 관련 platform README와 agent/runtime 문서가 실제 배포 단위·환경 변수를 반영한다.

### E. 프런트·운영 UX

- v1/v2 자산 모두 적용되고, 재스냅 실패 anchor는 그리지 않으며 콘솔·개발 패널에서 사유를 확인할 수 있다.
- 자동 H-Line label에는 anchor 가격이 중복되지 않고 사용자 수동 label은 바뀌지 않는다.
- Flag가 pan/zoom과 1D/1W/1M 모두에서 실제 원인 봉의 pixel center에 놓인다.
- 구조/추세/인사이트 3개 토글, 자동 지표, 자산 적용·해제의 기존 행동이 회귀하지 않는다.
- 해설 패널에서 주요 관찰 항목을 선택하면 대응 작도가 강조되며, 작도 없는 상태도 자연스럽다.
- 개발 패널 문구는 정확히 `갱신 스킵(시간)`이다.
- `전체 S&P500`과 같은 줄 오른쪽에 `콤마로 구분` 안내가 있고, 공백 유무와 관계없이 콤마 입력이 동작한다.
- 로컬 `font-size` 선언이나 새 프런트 테스트 프레임워크를 추가하지 않는다.

## 2. 검증 데이터 원칙

### 2.1 실데이터만 사용

- runtime, smoke test, 시각 검증에서 가짜 시장 캔들을 생성하지 않는다.
- 재현 가능한 테스트 fixture가 필요하면 실제 provider/ClickHouse에서 가져온 캔들을 비식별·고정 snapshot으로 저장한다.
- fixture에는 source, symbol, interval, 시작/종료 시각, 캔들 수, 수집일, corporate-action 처리 여부를 manifest에 기록한다.
- 저장소 크기를 낮추기 위해 symbol별 candle series를 한 번만 저장하고 manifest의 서로
  다른 asOf/window가 이를 재사용한다. 전체 fixture 예산은 압축 전 2 MB 이내를 목표로
  하며 이미 존재하는 candle fixture와 중복 저장하지 않는다.
- OPENAI API key, prompt 전문, 모델 원응답, 계정 식별자는 fixture나 결과 파일에 포함하지 않는다.

### 2.2 평가 corpus

먼저 최소 24개 실제 market episode로 development/tuning corpus를 만든다. 한 종목 전체
기간을 하나로 세지 않고, 평가하려는 구조가 분명한 asOf 구간을 하나의 episode로 센다.
각 episode는 결과를 보기 전에 `must_draw|may_draw|must_not_draw`와 기대 semantic type을
독립 reviewer 합의로 manifest에 고정한다.

| 범주 | 최소 수 | 확인 대상 |
|---|---:|---|
| 강한 상승/하락 추세 | 4 | 추세선 반응, 현재 관련성, 돌파 후 상태 |
| 횡보/박스권 | 4 | 범위와 수평 zone, 억지 추세선 억제 |
| 갭·실적·고변동 | 4 | ATR 국소성, 이벤트 중복 제거, flag 봉 정합 |
| 약한 방향성/노이즈 | 4 | 정상 빈 레이어, 거짓 양성 억제 |
| 구조 전환/역할 반전 | 4 | breakout/retest, support/resistance role state |
| 알려진 회귀 사례 | 4 | NVDA 화면 밖 무의미 추세선 등 실제 실패 사례 |

여러 sector와 최소 세 가지 가격·변동성 규모를 포함한다. NVDA는 필수지만 NVDA에 맞춰 threshold를 조정하지 않는다.

출하용 holdout은 아래 최소 denominator를 만족할 때까지 같은 실제 series의 다른 asOf와
추가 symbol로 확장한다.

- `must_draw` 20 episodes 이상
- `must_not_draw` 20 episodes 이상
- 평가 가능한 최종 drawing 40개 이상
- 품질 gate를 통과한 화면 밖 anchor drawing 20개 이상

표본이 부족한 비율은 통과로 기록하지 않는다. 가짜 candle이나 결과를 맞추기 위한 episode
복제는 금지한다.

### 2.3 튜닝과 최종 평가 분리

- corpus manifest에 `development`, `tuning`, `holdout`과 사전 라벨을 고정한다.
- hard gate와 threshold 조정은 development/tuning만 보고 수행한다.
- holdout 결과를 본 뒤 threshold를 바꾸면 새로운 holdout episode를 확보하고 변경 사유를 기록한다.
- 시간 순서를 보존한 walk-forward 검증을 최소 한 번 수행한다. 미래 봉은 과거 시점 작도 후보 생성에 절대 사용하지 않는다.

## 3. 자동 불변식

다음 항목은 통계 목표가 아니라 **100% 통과해야 하는 계약**이다.

1. 모든 timed anchor는 해당 asset의 canonical candle manifest에 존재한다.
2. asset을 현재 candle API 응답에 적용한 뒤 모든 timed drawing은 실제 봉으로 재해석된다. 허용 오차 interpolation은 없다.
3. v2 자동 H-Line label에 anchor 가격과 동일한 숫자 문자열이 없다.
4. Layer I 출력의 모든 candidate/fact/condition/relation ID가 입력 allowlist에 존재하고,
   최종 geometry는 서버 후보와 동일하다. 사용자-facing factual clause는 서버 code-to-text가
   조립하며 LLM 자유 문장을 그대로 표시하지 않는다.
5. `quality.state != eligible`인 interval에는 새 S/T drawing이 없다. top-level `degraded`가
   LLM 장애만 뜻하는 경우에는 eligible S/T를 유지할 수 있다.
6. trend line은 세 개 이상의 독립 touch episode와 현재 관련성 hard gate를 충족한다.
7. channel은 두 경계의 증거와 slope 방향·평행성 조건을 충족한다.
8. event flag timestamp는 원인 event candle key와 같다.
9. 모든 최종 displayed drawing ID가 최소 한 `focusItems[].drawingIds`에 있고, 반대로 모든
   focus drawing ID가 최종 accepted drawing에 존재한다. 삭제된 후보는 어느 쪽에도 없다.
10. 동일 입력·설정·버전의 규칙 결과는 결정론적이다.
11. schema v1 fixture는 계속 parse되며, v2 fixture는 shared schema와 프런트 parser를 모두 통과한다.
12. API key나 provider 원응답이 log, ClickHouse asset, Redis, snapshot에 남지 않는다.

## 4. 품질 기준

### 4.1 전문가 검토 rubric

구현/canary-ready 평가는 두 번의 독립 blind review를 필수로 하고 reviewer가 사람인지
assisted/automated인지와 역할·버전을 기록한다. 이름을 가린 asset ID와 순서를 사용하며
서로의 점수를 보기 전에 각 drawing을 1~5점으로 채점한다. 축별 2점 이상 차이 또는
유의미/무의미 판정 불일치는 근거를 남기고 adjudication한다.

사람 검토가 없으면 결과 이름은 `automated reviewer estimate`이며 “전문가 precision”이라고
부르지 않는다. 공유 dev canary 이후 실제 v2 serving 승격, 특히 100 symbols/S&P500 전에
시장 차트 해석이 가능한 사람 reviewer 최소 1명이 holdout을 blind 평가해야 한다. 두 번째
reviewer는 독립 human 또는 assisted reviewer여도 된다. 이 human gate가 없으면 구현은
`canary-ready`로 보고할 수 있지만 production rollout은 pending이다.

| 축 | 1점 | 3점 | 5점 |
|---|---|---|---|
| 구조 근거 | 임의의 두 점/노이즈 | 일부 반응은 있으나 약함 | 독립 반응과 명확한 가격 행동 |
| 현재 관련성 | 현재 의사결정과 무관 | 조건부 참고 가능 | 현 가격/최근 사건에 직접 연결 |
| 기하 정확성 | 봉·가격에서 이탈 | 대체로 맞음 | anchor와 반응이 정확함 |
| 비중복성 | 같은 메시지 반복 | 일부 겹침 | 각 그림의 역할이 고유함 |
| 해설 유용성 | 일반론/환각 | 근거는 있으나 행동 조건이 약함 | 무엇을 보고 언제 무효인지 분명함 |

`유의미`는 구조 근거와 현재 관련성이 각각 4점 이상인 drawing이다.
`명백히 무의미`는 adjudication 뒤 구조 근거, 현재 관련성, 기하 정확성 중 하나라도 1점인
drawing이다.

### 4.2 출하 목표

- hard-gate 통과 drawing의 blind-review precision: **85% 이상**
- 명백히 무의미한 drawing 비율: **5% 이하**
- 화면 밖에서 시작하는 accepted drawing 중 현재 관련성 rubric 4점 이상 비율: **95% 이상**
- `must_not_draw` episode에서 불필요한 drawing 생성률: **10% 이하**
- `must_draw` episode에서 유의미한 drawing을 하나 이상 찾는 episode recall: **60% 이상**
- 최종 drawing 수: episode 중앙값 **3 이하**, p95 **5 이하**
- timed anchor 정확도와 LLM 후보 provenance: **100%**
- commentary focus item 중 실제 drawing/지표 근거가 연결된 비율: **100%**

품질 목표가 수량 목표와 충돌하면 drawing을 줄인다. drawing 수를 맞추기 위해 hard gate를
낮추지 않는다. 단, 모든 episode에서 0개를 내는 결과는 precision 통과가 아니라 측정 불가/
recall 실패다. 각 비율은 numerator/denominator와 Wilson interval을 함께 기록하되, release
판정은 사전 정의한 point threshold와 최소 denominator를 모두 사용한다.
화면 밖 현재 관련성 95%는 **accepted offscreen drawing 20개 이상**에서만 판정한다. 부족하면
gate를 낮추거나 무의미한 선을 채택하지 말고 real holdout episode를 확장한다.

### 4.3 필수 NVDA 회귀

현재 저장된 실패 사례를 고정 회귀로 남긴다.

- `2025-09-17 → 2025-09-25` 두 점만으로 만들어지고 현재 영향이 없는 ray는 생성되지 않는다.
- 과거 52주 신고가·breakout flag가 최신 사건을 밀어내지 않는다.
- 현재 가격에서 지나치게 먼 한 번짜리 수평선은 독립된 현재 근거 없이 채택되지 않는다.
- 최종 anchor는 현재 chart candle timestamp/key에 맞는다.
- 화면 밖에서 시작하는 선이 남는 경우에는 세 번째 반응과 현재 접촉/근접/최근 돌파 같은 명시적 근거가 해설에 연결된다.

## 5. 성능·비용·저장 기준

동일한 로컬 개발 환경과 corpus로 v1 baseline과 v2를 측정한다. pure kernel은 single process/
single thread로 warm-up 5회 뒤 30회, serving API는 warm request 30회 이상을 사용한다.
머신, CPU, 데이터 수, ClickHouse 상태를 결과에 기록한다.

| 지표 | 기준 | 분류 |
|---|---:|---|
| canonical market-data query | symbol당 1회 이하 | hard |
| LLM 호출 | symbol당 1회 이하 | hard |
| Responses `max_output_tokens` | 1,200 이하 | hard |
| 저장 asset hard cap | 20 KB | hard |
| 증명 가능한 fast no-op | kernel·LLM·INSERT 100% 생략 | hard |
| kernel 후 동일한 late intent no-op | LLM·INSERT 100% 생략 | hard |
| LLM 후 동일한 content | INSERT·cache invalidation 100% 생략 | hard |
| 규칙 kernel p95 | interval당 500 bars 기준 75 ms 이하 | benchmark 목표 |
| 규칙 kernel 상대 p95 | v1의 1.25배 이하 | regression gate |
| LLM 입력 p95 | 1,500 tokens 이하 | canary 비용 gate |
| LLM 실제 출력 p95 | 650 tokens 이하 | canary 비용 gate |
| 저장 asset p95 | 12 KB 이하 | canary 저장 gate |
| serving API p95 | 기존 baseline 대비 10% 초과 악화 없음 | regression gate |
| serving API 절대 p95 | 100 ms 이하 | 환경 의존 목표 |

hard, regression, canary 비용/저장 gate는 완료 조건이다. 넘으면 bounded candidate,
serialization, prompt를 먼저 줄인다. 75 ms와 절대 API 100 ms는 측정 목표이며 미달 시
profile과 병목을 `IMPLEMENTATION_NOTES.md`에 기록한다. 이 두 절대 목표를 맞추려고 품질 hard
gate를 완화하지 않는다. 품질을 유지하면서 v2 kernel이 v1의 1.25배를 지속적으로 넘으면
목표 예외가 아니라 성능 결함으로 본다.

## 6. 검증 명령

모든 Python 명령은 저장소 루트 `.venv`의 Python 3.12로 실행한다. 실제 파일명이나 기존 script entry point가 다르면 현재 저장소 관례에 맞게 최소 조정하고 구현 노트에 적는다.

### 6.1 각 묶음 공통

```bash
git diff --check
.venv/bin/python --version
.venv/bin/python scripts/local/check-chart-data-contracts.py
```

변경 범위의 단위 테스트를 먼저 실행한 뒤 아래 전체 회귀를 실행한다.

```bash
.venv/bin/python -m unittest discover -s systems/market-data/tests
.venv/bin/python -m unittest discover -s systems/agent-orchestration/tests
npm run test:chart --prefix apps/gops-frontend
npm run test:layout --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
npm run test:bundle-size --prefix apps/gops-frontend
```

명령이 현재 `package.json`/테스트 구조에 없으면 새 테스트 프레임워크를 만들지 말고 동등한 기존 명령을 사용한다.

### 6.2 계약·kernel

```bash
.venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_analysis_*.py'
.venv/bin/python scripts/local/eval-chart-assets-v2.py \
  --manifest systems/market-data/tests/fixtures/chart_assets_v2/manifest.json \
  --mode rules \
  --output /tmp/chart-assets-v2-rules.json
```

평가 script는 invariant 실패 시 non-zero로 종료하고, 후보와 최종 drawing의 수·탈락 사유·성능 통계만 출력한다.

### 6.3 API·worker·저장

로컬 실데이터 stack에서 작은 allowlist로 build → ClickHouse 저장 → API serving을 관통한다.

```bash
.venv/bin/python scripts/local/eval-chart-assets-v2.py \
  --symbols NVDA,AAPL,MSFT,SPY \
  --intervals 1M,1W,1D \
  --mode integration \
  --output /tmp/chart-assets-v2-integration.json
```

같은 명령을 다시 실행해 `unchanged`와 query/LLM/INSERT 생략을 확인한다. Redis key 목록과 ClickHouse payload 크기도 자동 점검한다.

### 6.4 프런트 시각 검증

```bash
npm run test:chart-visual --prefix apps/gops-frontend
```

기존 visual 명령이 다르면 해당 명령을 사용한다. NVDA 1D, 좁은 viewport, 넓은 viewport,
older-range fetch, 정상 빈 레이어, v1 asset, v2 asset을 확인한다. 다음을 screenshot 또는
trace로 남긴다.

- flag가 실제 봉 중앙을 가리킨다.
- H-Line 가격은 가격축에만 나타나고 자동 label에 중복되지 않는다.
- 화면 밖 시작 추세선은 현재 관련 근거가 있을 때만 보인다.
- focus item과 강조 drawing이 일치한다.
- `전체 S&P500` 행의 `콤마로 구분`, `갱신 스킵(시간)` 문구가 레이아웃을 깨지 않는다.

### 6.5 실제 LLM canary

`.env`의 `OPENAI_API_KEY`가 있을 때만 실제 provider canary를 실행한다. 값을 출력하거나 shell history에 펼치지 않는다.

```bash
.venv/bin/python scripts/local/eval-chart-assets-v2.py \
  --manifest systems/market-data/tests/fixtures/chart_assets_v2/manifest.json \
  --intervals 1M,1W,1D \
  --mode llm-canary \
  --stratified-limit 1 \
  --env-file .env \
  --output /tmp/chart-assets-v2-llm-smoke.json

.venv/bin/python scripts/local/eval-chart-assets-v2.py \
  --manifest systems/market-data/tests/fixtures/chart_assets_v2/manifest.json \
  --intervals 1M,1W,1D \
  --mode llm-canary \
  --stratified-limit 12 \
  --env-file .env \
  --output /tmp/chart-assets-v2-llm-canary.json
```

1건 smoke가 secret/endpoint/schema를 통과한 뒤에만 12건 품질 pilot을 실행한다. canary는
symbol episode당 1회 호출, `store: false`, schema 준수, candidate provenance, 수치 재조립,
토큰/지연/비용 추정치를 확인한다. 반복 안정성 검사는 worker 저장/no-op을 우회하는 eval
harness가 같은 bundle을 3회 직접 호출하며 어떤 결과도 asset으로 저장하지 않는다.
`/tmp` 결과는 비밀과 원문을 제거한 summary만 담고 커밋하지 않는다.
12개는 2.2의 범주를 골고루 포함한다. 전문가 판정이 경계에 있거나 새로운 reject reason이
계속 나오면 같은 고정 manifest의 나머지 episode로 최대 24개까지 확장한다.

이번 환경처럼 key가 제공된 구현 완료에서는 real smoke와 pilot이 필수다. key가 실제로
없거나 DNS/provider 장애가 재현되어 호출 자체가 불가능한 경우에만 mock structured-output과
degraded path로 대체하고 명령·오류 범주·재시도 결과를 구현 노트에 남긴다. 품질 또는 안정성
gate 실패는 “외부 장애”가 아니며 Layer I를 비활성화해야 한다.

### 6.6 전체 보호 회귀

마지막 묶음에서 현재 저장소에 존재하는 chart/order/agent 보호 회귀를 확인해 실행한다.
이 저장소 루트에는 `package.json`이 없으므로 사용자 완료 조건의 `npm run build`는 프런트
workspace build로 실행한다. 최소한 다음을 포함한다.

```bash
.venv/bin/python scripts/local/check-chart-data-contracts.py
npm run build --prefix apps/gops-frontend
git diff --check
```

기존 chart, order, agent API route와 동작이 바뀌지 않았음을 smoke test한다.

## 7. 수동 품질 검토 기록

`scripts/local/eval-chart-assets-v2.py`는 사람 검토용 정적 HTML 또는 JSON index를 만들 수 있어야 한다. 별도 웹 프레임워크는 추가하지 않는다. 각 episode에 다음만 표시한다.

- candle 범위와 최종 drawings
- 후보별 선택/탈락 사유 코드
- 현재 관련성 근거
- 연결된 focus item과 확인/무효화 조건
- v1과 v2를 이름을 가린 A/B 순서로 비교할 수 있는 식별자

검토 결과는 총점만 남기지 말고 drawing별 rubric과 reject reason을 남긴다. threshold 변경은 어떤 false positive/false negative를 줄였는지 구현 노트에 기록한다.

## 8. 단계적 롤아웃

자동 갱신 기능을 새로 만들지 않는다. 기존 수동/작업 trigger에서 allowlist를 늘리는 방식으로 진행한다.

1. **shadow**: 5 symbols, S/T만 저장 전 비교. 기존 serving은 v1 유지.
2. **canary**: 5 symbols에 v2 serving, 실제 LLM 포함. 최소 2회 수동 build와 no-op 확인.
3. **확장 평가**: 2.2의 holdout 최소 denominator까지 corpus 확장. 품질 rubric과 운영 기준 통과.
4. **100 symbols**: sector/변동성 분산 allowlist. 부분 실패와 payload/비용 관찰.
5. **S&P500 수동 build**: 앞 단계가 모두 통과한 뒤 개발 패널에서 명시적으로 실행.

각 단계에서 3·4·5절 기준을 통과해야 다음 단계로 간다. 시간이 지났다는 이유만으로 자동 승격하지 않는다.

## 9. 중단·롤백 기준

아래 중 하나면 즉시 v2 serving을 중단하고 v1 자산 또는 rule-only degraded 경로로 되돌린다.

- anchor 불변식 실패 또는 봉과 다른 위치의 flag 재현
- LLM이 palette 밖 후보·geometry·검증되지 않은 수치를 통과시킴
- 무의미 drawing 비율 5% 초과 또는 blind-review precision 85% 미만
- 기존 chart/order/agent 보호 회귀 실패
- secret/provider 원응답 저장 발견
- asset hard cap 초과, Redis 자산 본문 저장, symbol당 다중 LLM 호출

rollback은 schema v1 reader와 이전 engine version을 유지해 가능하게 한다. DDL destructive rollback, 기존 row 삭제, 오케스트레이터 변경은 하지 않는다.

## 10. 묶음별 커밋

각 묶음은 완료 조건 확인 → 해당 검증 → `git diff --check` 통과 후 한 번 커밋한다. 사용자 작업과 무관한 dirty file은 stage하지 않는다.

1. `feat(chart-assets): add v2 quality and canonical candle contract`
2. `feat(chart-assets): rebuild structure trend and event engines`
3. `feat(chart-assets): curate visual candidates and grounded commentary`
4. `feat(chart-assets): optimize builder storage and serving pipeline`
5. `feat(chart-assets): align asset geometry commentary and ops ux`

푸시하지 않는다. 마지막에 `IMPLEMENTATION_NOTES.md`, 평가 summary, 실행한 명령과 결과, 미해결 한계를 함께 보고한다.
