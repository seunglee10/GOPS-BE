# GOPS Chart Rendering Milestones

## 현재 기준선 상태

현재 구현 상태는 다음 이름으로 다룬다.

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

이 표현은 기존 M1-M5 chart rendering runtime이 V1 chart tool runtime의 core baseline 안으로 흡수됐다는 뜻이다. Playwright/browser screenshot regression, multi-chart browser scenario, `/ref/references` behavior comparison, real provider 전환 정책까지 완료된 “검증 완결판”이라는 뜻은 아니다.

다음 구현은 [chart-tool-runtime-v1.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-tool-runtime-v1.md)와 [chart-tool-runtime-milestones.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-tool-runtime-milestones.md)를 기준으로 진행한다. 본 문서의 기존 M1-M5 항목은 현재 V1 baseline의 렌더링 하위 이력으로 참고한다.

## 목적

이 문서는 GOPS chart rendering runtime을 Goal 모드에서 안정적으로 구현하기 위한 마일스톤이다. 각 마일스톤은 이전 단계의 구조를 유지하면서 다음 기능을 얹는 방식으로 진행한다.

구현 중 `/ref`는 참고 전용이다. 마일스톤 산출물은 현재 GOPS `frontend/`, `backend/`, `docs/` 구조에 새로 작성한다.

## 공통 검증 루프

차트 렌더링 마일스톤은 매번 다음 루프를 통과해야 한다.

```text
구현 -> 단위 테스트 -> 브라우저 렌더 확인 -> canvas pixel/screenshot 확인 -> reference 동작 비교 -> 보완 -> 재검증
```

공통 기준:

- `npm run build`와 관련 unit test를 통과한다.
- backend가 포함된 마일스톤은 `python -m compileall backend/app`을 통과한다.
- Playwright 또는 동등한 브라우저 검증으로 chart panel을 실제로 렌더링한다.
- Canvas가 nonblank인지 확인하고, candle/volume/MA/axis/crosshair가 기대 위치와 draw order로 보이는지 확인한다.
- desktop viewport와 compact/standard/wide/large panel 크기에서 겹침, 흐림, 잘림, 비정상 scaling이 없는지 확인한다.
- 모바일 전용 viewport와 모바일 UI 레이아웃은 현재 마일스톤 범위에서 제외한다.
- 필요한 경우 `/ref/references`의 `lightweight-charts`, `klinecharts`, `uplot`, `technicalindicators`를 읽고 scale, pane, interaction, indicator behavior를 비교한다.
- 검증 실패 시 보완 후 같은 검증을 다시 수행한다. 한 번 확인하고 끝내지 않는다.

## M1. Dummy Candle API와 Custom Canvas 첫 렌더링

목표:

- FastAPI dummy candle API에서 시장 데이터 스펙과 호환되는 candle snapshot을 제공한다.
- chart panel이 `chartDocumentId`로 `ChartDocument`를 찾아 Custom Canvas로 candlestick, volume, MA를 그린다.
- Bento Grid panel resize에 맞춰 Canvas가 다시 그려진다.

포함 범위:

- `/api/charts/candles` compatible dummy response
- `CandleData` adapter와 `CandleStore`
- chart document seed
- `RenderScene` builder MVP
- candlestick, volume, `ma5`, `ma20`, `ma60` Canvas renderer
- loading, empty, error state

완료 기준:

- 기본 chart panel에서 dummy candle chart가 표시된다.
- panel 크기를 바꿔도 Canvas가 nonblank 상태로 다시 렌더링된다.
- chart panel 2개를 만들 수 있는 구조가 있고 각 panel이 독립 `chartDocumentId`를 가진다.
- renderer는 document/data를 직접 수정하지 않는다.

검증 시나리오:

- `npm run build`
- `python -m compileall backend/app`
- dummy candle API response shape 확인
- desktop browser의 작은/큰 panel viewport에서 Canvas nonblank 확인
- screenshot 또는 canvas pixel check로 candle, volume, MA, axis가 실제로 그려졌는지 확인
- `/ref/references`의 financial chart 구현을 참고해 candle width, wick, y-scale, volume scale의 기본 동작을 비교

## M2. Viewport, Crosshair, Size Variant

목표:

- chart panel 안에서 pan/zoom, crosshair, visible range summary를 제공한다.
- panel size variant에 따라 chart UI 밀도를 바꾼다.

포함 범위:

- `chart.viewport.set`
- wheel zoom
- drag pan
- crosshair readout
- visible range high/low/change summary
- compact/standard/wide/large chart rendering policy

완료 기준:

- pan/zoom은 market subscription 또는 candle state를 변경하지 않는다.
- resize와 variant 변경이 chart identity나 viewport를 불필요하게 초기화하지 않는다.
- compact panel에서도 텍스트와 chart가 겹치지 않는다.

검증 시나리오:

- viewport command unit test
- pan/zoom 후 data state 불변 확인
- size variant별 screenshot 또는 canvas nonblank 확인
- crosshair와 axis readout이 panel resize와 device pixel ratio 변화 후에도 좌표와 값이 어긋나지 않는지 확인
- reference chart의 zoom anchor, cursor, time scale 동작을 필요한 범위에서 비교

## M3. Chart Command Engine과 Panel-local Undo/Redo

목표:

- 사용자 차트 조작을 chart command로만 적용한다.
- chart panel 내부 undo/redo 버튼이 chart history를 되돌린다.

포함 범위:

- chart command envelope
- command validation
- `chart.symbol.set`
- `chart.timeframe.set`
- `chart.viewport.set`
- `chart.layer.visibility.set`
- `chart.undo`
- `chart.redo`
- no-op chart command history 제외

완료 기준:

- React component가 `ChartDocument`를 직접 수정하지 않는다.
- 잘못된 target/payload command는 document를 변경하지 않는다.
- 사용자 직접 조작 1개는 chart history entry 1개로 기록된다.
- no-op command는 chart history와 journal을 오염시키지 않는다.

검증 시나리오:

- command validation unit test
- undo/redo reducer test
- no-op command test
- multiple chart panel history isolation test

## M4. LLM Chart Proposal과 Global Auto Policy

목표:

- LLM이 chart command 묶음을 제안하고, 전역 auto toggle에 따라 pending 또는 즉시 적용된다.
- LLM proposal 하나는 chart undo/redo에서 하나의 수정 단위로 취급된다.

포함 범위:

- `ChartProposal` document
- chart command capability manifest metadata
- auto off pending proposal flow
- auto on grouped apply flow
- proposal accept/reject
- grouped chart history entry
- OpenAI key missing 시 backend `503` 정책 문서/테스트 연결

완료 기준:

- LLM 응답은 chart document를 직접 변경하지 않는다.
- auto off에서는 pending proposal만 생기고 chart state는 바뀌지 않는다.
- auto on에서는 validation 통과 proposal만 적용된다.
- 적용된 proposal은 chart panel 내부 undo 한 번으로 되돌아간다.

검증 시나리오:

- invalid LLM command rejection test
- auto off pending proposal test
- auto on grouped apply test
- grouped undo/redo test
- backend OpenAI key missing `503` test
- capability manifest의 `payloadSchema`, `requiredContext`, `previewable`, `autoApplyEligible`, `undoScope`, `conflictsWith`, `recommendedWith` 기준 테스트
- LLM이 market summary와 visible chart context를 바탕으로 여러 chart command를 조합하되 rationale을 포함하는지 검증

## M5. Live Update와 Multi-chart Data Cache

목표:

- WebSocket live/corrected candle update를 적용한다.
- 여러 chart panel이 같은 symbol/timeframe data를 공유하되 chart state는 독립적으로 유지한다.

포함 범위:

- `LIVE_CANDLE_UPDATE`
- `CANDLE_CLOSED`
- `CANDLE_CORRECTED`
- stale event ignore
- corrected candle replacement
- shared candle cache
- per-chart viewport/config isolation
- stream status: `connecting`, `live`, `stale`, `error`

완료 기준:

- 같은 timestamp update는 기존 candle을 교체한다.
- corrected candle이 MA 값을 포함하면 해당 render scene에 반영된다.
- 하나의 chart panel pan/zoom이 다른 chart panel viewport에 영향을 주지 않는다.
- 재연결 후 snapshot/live 중복이 chart를 깨뜨리지 않는다.

검증 시나리오:

- candle merge unit test
- stale event ignore test
- corrected candle test
- two chart panels same symbol/timeframe test
- WebSocket reconnect simulation

## 후속 단계

M5 이후 항목 중 comparison series, P0 drawing, proposal preview layer는 현재 `Chart Tool Runtime V1 core implementation baseline`에 포함됐다. Indicator registry 고도화, advanced drawing suite, Workspace-level grouped history, server-side chart document persistence는 별도 hardening 또는 후속 마일스톤으로 분리한다.

특히 layout command와 chart command가 한 proposal에 함께 들어가는 복합 적용은 chart panel-local history만으로는 충분하지 않으므로, Workspace-level grouped history가 준비된 뒤 구현한다.
