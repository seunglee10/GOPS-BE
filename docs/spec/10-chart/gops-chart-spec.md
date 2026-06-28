# GOPS Chart Feature Specification

## 목적

GOPS 차트 기능은 외부에서 수신한 실시간 시장 데이터를 차트 문서로 해석하고, 사용자가 직접 편집하거나 LLM이 제안한 차트 구성을 승인해 반영할 수 있는 분석용 차트 패널을 제공한다.

시장 데이터 공급 자체는 외부 입력으로 전제한다. 본 문서는 GOPS 내부에서 담당하는 수신 이후 처리, 차트 상태 관리, 사용자 편집, 계산, 렌더링, LLM 제안 흐름을 정의한다.

## 우선순위 태그

- `[P0]`: MVP 동작에 필수인 기능
- `[P1]`: 분석 경험 완성도를 높이는 핵심 보강 기능
- `[P2]`: 확장성과 운영 품질을 위한 후속 기능

## 현재 구현 결정

- 현재 구현 상태명은 `Chart Tool Runtime V1 core implementation baseline + validation hardening backlog`로 고정한다. 이는 차트 도구 핵심 기준선이 동작한다는 뜻이며, 검증 완결판이나 실데이터 provider 전환 완료를 뜻하지 않는다.
- 차트 렌더링은 Custom Canvas 우선으로 진행한다. `/ref`는 참고 전용이며, 코드를 직접 복사하거나 이식하지 않는다.
- 현재 Brothers 기준 구현은 FastAPI chart API가 Alpaca-backed market data provider bridge를 통해 내려주는 `/api/charts/candles` 응답과 WebSocket message shape를 사용한다. frontend는 provider unavailable 상태에서 임의 candle을 생성하지 않는다.
- chart panel은 전역 singleton이 아니라 여러 panel 중 하나다. 각 chart panel instance는 독립적인 `chartDocumentId`를 통해 `ChartDocument`를 참조한다.
- top app bar의 전역 auto toggle은 layout command와 chart command의 LLM 적용 정책을 함께 제어한다.
- chart panel 내부에는 chart state 전용 undo/redo를 둔다. top app bar의 undo/redo는 layout history 전용이다.
- LLM chart proposal 하나는 적용될 때 chart history에서 하나의 undo/redo 단위로 기록한다.
- LLM drawing/comparison proposal은 auto toggle과 무관하게 preview-first로 표시한다. preview를 적용하기 전까지 `ChartDocument.drawings`와 `comparisons`를 변경하지 않는다.
- hidden preview는 pending 상태로 남지만 apply할 수 없다. 사용자가 다시 표시한 뒤 적용한다.
- Agent 01만 현재 chart command chat/proposal 권한을 가진다. Agent 02~04와 multi-agent orchestration은 UI scaffold로 남기고 chart command 입력은 비활성으로 둔다.
- chart capability manifest는 단순 allowlist가 아니라 LLM의 도구 조합 판단을 위한 metadata로 정의한다.
- 전역 auto toggle은 layout/chart 분석 UI command에만 적용하며, 주문 생성/취소/정정 같은 거래 command에는 절대 적용하지 않는다.
- 현재 UI 기준은 desktop Bento Grid workspace다. 모바일 전용 viewport, 모바일 레이아웃, 하단 rail/overlay 같은 모바일 화면은 아직 고려하지 않는다.

## 구현 순서

### 1. 시장 데이터 수신 이후 처리 `[P0]`

목표:

- 외부에서 들어온 시장 데이터를 GOPS 차트가 사용할 수 있는 정규화된 시계열 상태로 관리한다.
- 데이터 공급자 교체와 무관하게 chart engine은 동일한 market data contract를 사용한다.

기능:

- symbol, timeframe 기준의 snapshot 적용
- live candle update 적용
- 동일 timestamp candle 교체
- 신규 timestamp candle 추가
- 오래된 이벤트 또는 역순 이벤트 무시
- 재연결 후 snapshot과 live update의 중복 처리
- active symbol과 comparison symbol의 데이터 상태 분리 관리
- 시장 데이터 수신 상태 표시: `connecting`, `live`, `stale`, `error`

완료 기준:

- chart engine은 외부 공급자 구현을 알지 못하고 정규화된 candle 배열만 사용한다.
- snapshot과 live update가 들어와도 viewport와 chart configuration이 의도 없이 초기화되지 않는다.
- pan/zoom 같은 사용자 interaction은 market subscription 또는 data state를 변경하지 않는다.

### 2. Chart Document와 Command Engine `[P0]`

목표:

- 차트 상태 변경의 단일 진입점을 Command Engine으로 고정한다.
- 사용자 조작과 LLM 제안을 같은 구조에서 처리한다.

기능:

- `WorkspaceDocument`가 패널, 차트, 제안, command journal을 관리
- `ChartDocument`가 symbol, timeframe, viewport, pane, scale, layer, calculation graph를 관리
- 모든 chart document 변경은 command 검증 후 적용
- command 실행 결과를 journal에 기록
- 실패한 command는 document를 변경하지 않음
- LLM command는 `ChartDocument`를 직접 변경하지 않고 proposal 또는 검증된 grouped command로만 처리
- 전역 auto toggle이 꺼져 있으면 LLM command를 proposal 상태로 저장
- 전역 auto toggle이 켜져 있으면 검증된 LLM proposal을 하나의 grouped action으로 atomic 적용
- chart history는 사용자 직접 조작 1개 또는 LLM proposal 1개를 undo/redo 단위로 기록

완료 기준:

- React component가 document를 직접 수정하지 않는다.
- 잘못된 target, payload, 권한, limit 초과 command가 차단된다.
- proposal 적용 중 하나라도 실패하면 전체 적용이 취소된다.
- no-op chart command는 chart history를 오염시키지 않는다.

### 3. Chart Panel Runtime `[P0]`

목표:

- 하나의 차트 패널이 자체 chart, tools, renderer, interaction state를 소유한다.
- 장기적으로 여러 chart panel을 추가할 수 있는 구조를 유지한다.

기능:

- chart panel 내부에 chart tools 배치
- chart panel별 target chart 지정
- panel layout pin 상태는 frontend layout runtime이 관리하고, chart panel runtime은 이를 chart 내부 상태와 분리해 참조
- chart tool mode 관리
- crosshair, select, pan, drawing mode 지원
- horizontal line, trend line, vertical marker, text label, point marker, arrow, range box, measurement drawing 지원
- `horizontalLine`은 timestamp 없이 price-only anchor를 가질 수 있음
- panel-local command dispatch
- panel-local chart undo/redo

완료 기준:

- 차트 도구가 전역 UI가 아니라 chart panel에 귀속된다.
- command target에 panel과 chart가 명확히 포함된다.
- chart panel을 추가 확장할 수 있는 registry 기반 구조가 유지된다.
- top app bar undo/redo와 chart panel undo/redo의 책임이 섞이지 않는다.

### 4. 사용자 직접 차트 편집 도구 `[P0]`

목표:

- 사용자가 LLM 요청 없이도 차트를 직접 커스터마이징할 수 있다.

기능:

- symbol 변경
- timeframe 변경
- viewport reset
- chart panel의 위치/크기 pin은 layout command로 처리
- LLM chart command 적용 여부는 top app bar의 전역 auto toggle로 처리
- indicator 추가, 입력값 수정, 삭제
- comparison symbol 추가, 삭제
- horizontal line, trend line, vertical marker, text label, point marker, arrow, range box, measurement 추가와 최소 편집
- drawing 선택, 이동, anchor 편집, label/style 수정, 삭제, 전체 삭제
- layer 표시/숨김
- 제거 가능한 layer 삭제
- chart undo/redo

완료 기준:

- 모든 사용자 편집은 `actor: "user"` command로 실행된다.
- 편집 실패 시 사용자에게 command error가 표시된다.
- chart tool UI와 chart state가 일관되게 동기화된다.
- chart 내부 편집은 panel-local chart history에 기록된다.

### 5. Calculation Engine과 기본 지표 `[P0]`

목표:

- Flink/Backend가 계산해 내려준 차트 지표와 시장 요약을 결정론적으로 사용한다.
- Chart Engine은 공식 지표 계산을 담당하지 않고 렌더링과 UI 보조 계산만 수행한다.
- LLM은 계산을 직접 수행하지 않고 계산 결과 요약만 사용한다.

지원 지표:

- SMA
- EMA
- RSI
- MACD
- Bollinger Bands
- VWAP
- ATR
- Volume MA

기능:

- indicator registry 기반 입력값 검증
- warmup 구간 `null` 처리
- calculation node와 indicator layer 연결
- chart 변경 시 필요한 지표 계산 요청 또는 계산 결과 갱신 반영
- crosshair 값 표시, viewport 기준 min/max, comparison percent label, 화면 좌표 변환, proposal preview layer 표시 같은 UI 보조 계산
- market summary 생성
- market summary와 visible chart context를 LLM 도구 선택 context로 제공

완료 기준:

- 잘못된 indicator 입력값이 command validation에서 거부된다.
- 지표 layer 삭제 시 관련 calculation node도 함께 정리된다.
- 같은 market data와 같은 chart configuration에 대해 Flink/Backend에서 내려온 같은 계산 결과를 항상 같은 방식으로 표시한다.

### 6. Canvas 2D Rendering `[P0]`

목표:

- `ChartDocument`, market data, calculation output에서 파생된 render scene만 사용해 차트를 그린다.
- GOPS 전용 Custom Canvas renderer를 단계적으로 구현한다.

기능:

- `MarketDataAdapter -> CandleStore -> ChartDocument -> RenderScene -> CanvasLayerRenderer` 파이프라인
- candle 렌더링
- volume 렌더링
- indicator 렌더링
- comparison line 렌더링
- horizontal line, trend line, vertical marker, text label, point marker, arrow, range box, measurement 렌더링
- proposal preview 렌더링
- selected/hovered drawing affordance 렌더링
- crosshair와 axis label
- device pixel ratio 대응
- hidden layer 렌더링 제외
- `/ref`의 render scene, scale model, Canvas layer 분리 아이디어는 참고하되 직접 이식하지 않음
- `/ref/references`의 실제 차트/지표 라이브러리는 scale, pane, candle, volume, interaction, indicator behavior를 검증하기 위한 참고 자료로 사용

완료 기준:

- renderer가 document 또는 market data를 수정하지 않는다.
- draw order가 안정적으로 유지된다.
- pan/zoom 이후에도 candle, volume, indicator, drawing 위치가 일관된다.
- Bento Grid resize 후에도 Canvas가 nonblank 상태로 다시 렌더링된다.
- screenshot 또는 canvas pixel 기반 확인으로 candle, volume, axis, MA가 실제 위치에 그려졌는지 검증한다.

### 7. Viewport, Scale, Comparison 처리 `[P1]`

목표:

- 실시간 업데이트와 사용자 viewport 조작이 서로 간섭하지 않도록 한다.
- comparison series가 main price scale을 왜곡하지 않게 한다.

기능:

- logical range와 visible data range 분리
- follow realtime mode
- fixed logical range mode
- price scale, volume scale, percent scale 분리
- comparison 기준값 정책 적용
- comparison data timestamp 정렬 및 gap 처리
- header percent label의 기준 명확화

완료 기준:

- comparison line이 price candle scale을 찌그러뜨리지 않는다.
- 최신 candle 업데이트가 전체 comparison path를 불필요하게 흔들지 않는다.
- 사용자는 표시된 퍼센트가 visible range 기준인지 live candle 기준인지 구분할 수 있다.

### 8. LLM Chat과 차트 제안 `[P1]`

목표:

- 사용자가 자연어로 시장 상황을 질문하고, LLM이 분석과 차트 제안을 반환한다.

기능:

- frontend가 현재 chart context와 market summary를 backend에 전달
- backend가 OpenAI API 호출
- 기본 응답 형식 검증
- 사용자용 message와 insights 반환
- chart proposal 반환
- 현재 구현 기준에서는 Agent 01만 chart operator로 OpenAI chat/proposal scaffold를 사용한다.
- shared canonical chart command schema는 runtime이 이해하는 전체 command set이며, backend OpenAI generation schema는 LLM이 직접 생성할 수 있는 안전 subset으로 제한한다.
- LLM이 명시적 사용자 요청뿐 아니라 market summary와 visible chart context를 바탕으로 여러 chart command를 조합할 수 있음
- LLM proposal은 조합 rationale을 포함
- 전역 auto toggle 기준으로 pending 또는 즉시 grouped apply 처리
- drawing/comparison proposal은 auto on 상태에서도 preview-first로 처리
- invalid proposal 또는 command 거부
- OpenAI API key 누락 시 명확한 `503` 반환

완료 기준:

- OpenAI 호출 코드는 backend에만 존재한다.
- LLM 응답은 chart document를 직접 변경하지 않는다.
- frontend는 insights와 proposal을 분리해 표시한다.
- auto on 상태에서 적용된 proposal도 chart panel 내부 undo/redo로 되돌릴 수 있다.

### 9. Proposal Preview, Auto 적용과 승인 흐름 `[P1]`

목표:

- 사용자는 auto off 상태에서 LLM 제안을 적용하기 전에 차트상에서 미리 확인할 수 있다.
- auto on 상태에서는 검증된 LLM proposal 중 즉시 적용 가능한 command를 적용하되, 하나의 grouped chart history entry로 기록한다.

기능:

- pending proposal 목록 표시
- proposal title, rationale, summary, command count 표시
- render 가능한 proposal preview layer 생성
- preview toggle과 apply preview action 제공
- preview hidden 상태에서는 apply disabled
- apply preview 시 grouped command 적용
- auto on 시에도 drawing/comparison proposal은 preview-first 유지
- viewport/layer처럼 preview가 필요 없는 command만 auto on에서 grouped command 즉시 적용
- reject 시 preview 제거
- failed accept 시 document 변경 없음
- proposal 단위 chart undo/redo

완료 기준:

- auto off proposal은 승인 전 `ChartDocument`를 변경하지 않는다.
- auto off에서는 accept 후에만 실제 layer, indicator, drawing, viewport 변경이 반영된다.
- auto on에서는 validation을 통과한 즉시 적용 가능 proposal만 반영된다. drawing/comparison은 preview로 남긴다.
- reject 또는 validation 실패 후 preview가 남지 않는다.
- proposal 적용은 한 번의 chart undo로 되돌릴 수 있다.

### 10. Registry 기반 확장 구조 `[P2]`

목표:

- 기능 추가와 제거가 특정 React component에 하드코딩되지 않도록 한다.

기능:

- command registry
- indicator registry
- renderer registry
- chart tool registry
- chart capability manifest
- panel registry
- scale registry
- market provider registry

완료 기준:

- 기능 활성화/비활성화가 registry 구성을 통해 가능하다.
- LLM에 노출되는 command 목록은 command registry에서 생성된다.
- chart capability manifest는 command type, description, payload schema, required context, preview 가능 여부, auto 적용 가능 여부, undo scope, conflictsWith, recommendedWith, validation rule을 포함한다.
- 새로운 지표나 layer renderer 추가 시 기존 핵심 흐름을 수정하지 않는다.

### 11. 검증과 테스트 `[P2]`

목표:

- market data 적용, chart 조작, LLM proposal 흐름의 회귀를 방지한다.

테스트 대상:

- snapshot과 live update 적용
- stale event 무시
- pan/zoom이 data subscription 또는 data state를 변경하지 않는지
- indicator 계산 결과
- command validation
- layer visibility
- chart undo/redo
- layout pin과 chart state 분리
- proposal atomic accept
- global auto toggle에 따른 pending/apply 분기
- invalid LLM output rejection
- missing OpenAI key `503`
- renderer scene 생성
- browser screenshot과 canvas pixel 기반 렌더링 검증
- reference library behavior 비교

완료 기준:

```bash
npm run build
npm run test
npm run backend:test
```

위 명령이 모두 통과해야 한다.

## GOPS 내부 책임

- 수신된 market data를 정규화된 chart data state로 관리한다.
- chart configuration을 document model로 관리한다.
- 사용자 편집을 command로 변환하고 검증한다.
- LLM 제안을 command proposal로 변환하고 검증한다.
- 전역 auto toggle 정책에 따라 LLM proposal을 pending 또는 grouped apply로 처리한다.
- LLM에 노출 가능한 chart tool과 command 조합 기준을 capability manifest로 관리한다.
- chart panel 내부 undo/redo history를 관리한다.
- calculation output과 market summary를 생성한다.
- render scene을 만들고 Canvas 2D로 렌더링한다.
- feature registry를 통해 기능 추가와 제거 가능성을 유지한다.

## 외부 전제

- 시장 데이터는 외부 provider 또는 adapter를 통해 GOPS backend/frontend가 사용할 수 있는 형태로 수신된다.
- 실제 provider 연결 방식은 chart engine의 관심사가 아니다.
- chart engine은 provider별 원본 포맷이 아니라 정규화된 candle, snapshot, live update contract만 사용한다.

## MVP 제외 범위

- 차트 엔진 내부의 주문 기능
- 실전 주문
- 계좌, 잔고, 체결, 포트폴리오 관리
- provider별 원본 연결 구현
- 뉴스 수집
- 온톨로지와 GraphRAG
- 사용자 정의 스크립팅 언어
- 백테스팅
- 인증
- 배포
- ChartDocument 영속 저장소
- WebGL 렌더러
- advanced drawing suite
- layout command와 chart command가 섞인 복합 proposal의 Workspace-level grouped history

KIS 모의투자 주문은 차트 엔진이 아니라 별도 주문 시스템 MVP 범위에서 다룬다.

## 최종 MVP 성공 기준

- 사용자는 수신된 market data 기반의 실시간 candle chart를 볼 수 있다.
- 사용자는 chart panel 안에서 직접 차트를 편집할 수 있다.
- 사용자는 LLM과 대화하며 시장 인사이트를 받을 수 있다.
- LLM은 차트 제안을 생성하되 `ChartDocument`를 직접 변경하지 않는다.
- auto off 상태에서는 사용자가 LLM 제안을 미리 보고 승인 또는 거절할 수 있다.
- auto on 상태에서는 검증된 LLM 제안이 Command Engine을 통해 grouped apply된다.
- 사용자는 chart panel 내부 undo/redo로 사용자 조작과 LLM 적용을 되돌릴 수 있다.
- market data 적용, chart state, calculation, rendering, LLM proposal이 서로 분리된 책임으로 유지된다.
