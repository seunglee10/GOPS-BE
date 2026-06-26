# GOPS Chart Tool Runtime V1

## Purpose

이 문서는 GOPS 차트를 단순 렌더링 패널에서 조작 가능한 분석 도구로 고도화하기 위한 기준이다.

현재 구현은 다음 상태로만 취급한다.

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

즉, 현재 chart renderer, dummy data, command runtime, drawing/comparison runtime, Agent 01 proposal/chat scaffold는 V1 core 구현 기준선이다. 다만 browser regression, multi-chart browser scenario, `/ref/references` behavior comparison, real provider 전환 정책까지 완료된 검증 완결판은 아니다.

## Product Direction

GOPS 차트 도구는 사용자가 직접 분석을 표시하는 도구이면서, LLM Agent가 시장/차트 해석을 시각적으로 제안하는 도구다.

따라서 LLM은 Canvas에 직접 그리거나 pixel 좌표를 반환하지 않는다. 사용자 UI와 LLM Agent는 같은 `ChartCommand` contract를 사용한다.

```text
User click/drag -> ChartCommand -> ChartDocument -> RenderScene -> Canvas
LLM proposal -> ChartProposal -> ChartCommand[] -> ChartDocument -> RenderScene -> Canvas
```

## Core Rule: Data Coordinates Only

Drawing과 annotation은 screen pixel에 저장하지 않는다.

금지:

```json
{ "x": 240, "y": 180 }
```

허용:

```json
{
  "timestamp": "2026-06-25T14:30:00Z",
  "price": 142.5,
  "paneId": "main",
  "symbol": "AAPL"
}
```

이 원칙을 지켜야 pan, zoom, resize, device pixel ratio 변경 이후에도 drawing이 같은 시장 의미를 유지한다.

## Runtime Concepts

V1에서 구현해야 할 핵심 모델:

- `TimeScale`: timestamp/logical index를 x 좌표로 변환한다.
- `PriceScale`: price를 y 좌표로 변환한다.
- `PercentScale`: comparison series를 percent 기준으로 표시한다.
- `VolumeScale`: volume bar를 별도 scale로 표시한다.
- `PaneLayout`: main price pane, volume pane, indicator pane의 크기와 위치를 계산한다.
- `CoordinateTransform`: data coordinate와 screen coordinate 변환을 담당한다.
- `DrawingEntity`: 차트 위에 남는 편집 가능한 오브젝트다.
- `DrawingAnchor`: drawing이 참조하는 time/price/pane/symbol anchor다.
- `HitTestResult`: pointer 위치에서 선택 가능한 chart object를 찾은 결과다.
- `ToolMode`: select, pan, draw-horizontal-line, draw-trend-line 같은 현재 도구 모드다.
- `LayerRenderer`: candle, volume, indicator, drawing, preview layer를 draw order에 맞게 렌더링한다.

## Drawing Entity Model

모든 drawing object는 최소한 다음 필드를 가진다.

```ts
type DrawingEntity = {
  id: string;
  type: DrawingType;
  anchors: DrawingAnchor[];
  style: DrawingStyle;
  label?: string;
  locked?: boolean;
  visible: boolean;
  createdBy: "user" | "llm" | "system";
  sourceProposalId?: string;
  createdAt: string;
  updatedAt: string;
};
```

V1 drawing type:

- `horizontalLine`: 지지선, 저항선, 목표가, 손절가. 가격 레벨 자체가 의미이므로 timestamp 없는 price-only anchor를 공식 허용한다.
- `trendLine`: 상승/하락 추세선.
- `verticalMarker`: 뉴스, 이벤트, 급등락 시점.
- `textLabel`: 분석 메모, LLM 해석 요약.
- `pointMarker`: 고점, 저점, 이상 신호, 진입 후보.
- `arrow`: 돌파, 반등, 급락 방향 표시.
- `rangeBox`: 박스권, 매물대, 지지/저항 구간.
- `measurement`: 구간 수익률, 가격 변화, 기간 변화.

P1 drawing type:

- `ellipse`: 특정 price/time 영역 강조.
- `riskRewardBox`: 진입/손절/목표 구간.
- `fibonacciRetracement`: 주요 되돌림 구간.

P1 drawing type은 schema와 registry에 준비될 수 있지만 V1 core 완료 기준은 아니다. toolbar, renderer, hit tester, browser regression이 갖춰진 뒤 완료로 본다.

## Comparison Overlay

Comparison overlay는 drawing과 별개로 chart analysis tool에 포함한다.

V1 요구:

- 같은 chart panel 안에 comparison symbol을 추가/삭제할 수 있다.
- comparison series는 main price scale을 왜곡하지 않는다.
- 기본 표시 방식은 percent scale이다.
- 기준값은 visible range 시작점 또는 selected base timestamp 중 하나로 계산하되, V1 기본값은 visible range 시작점이다.
- comparison line은 chart-local undo/redo 대상이다.
- LLM은 market context가 충분할 때 comparison 추가를 제안할 수 있다.
- 현재 UI는 supported symbol 목록에서 comparison을 선택하는 picker를 제공한다. 이미 추가된 symbol은 active 상태로 표시되고 다시 클릭하면 remove command를 실행한다.

## Preview-First LLM Policy

LLM drawing/annotation proposal은 preview-first다.

정책:

- LLM drawing proposal은 `pendingPreview`에 저장한다.
- `pendingPreview`는 `ChartDocument.drawings`에 들어가지 않는다.
- Canvas에는 별도 `proposalPreviewLayer`로 렌더링한다.
- preview는 chart undo/redo 대상이 아니다.
- 새 LLM drawing proposal이 오면 기존 `pendingPreview`를 덮어쓴다.
- preview toggle은 화면 표시 여부만 바꾼다.
- preview hidden 상태에서는 apply할 수 없다. 숨김은 pending preview를 유지한 채 화면 표시와 적용 버튼만 끄는 상태다.
- 사용자가 preview를 다시 표시하면 apply preview가 가능하다.
- apply preview는 pending preview를 grouped chart command로 적용한다.
- apply 후 pending preview는 제거된다.
- 적용된 drawing은 일반 `DrawingEntity`가 되어 select/move/edit/delete 가능하다.
- 적용된 drawing은 chart panel-local undo/redo 대상이다.

Chart panel UI:

```text
Preview toggle
Apply preview
```

버튼 상태:

- proposal 없음: 둘 다 disabled.
- proposal 있음 + preview visible: preview toggle on, apply enabled.
- proposal 있음 + preview hidden: preview toggle off, apply disabled.
- apply 후: pending preview 제거, 둘 다 disabled.

이 정책은 기존 “hidden 상태 apply 가능” 논의에서 변경된 최종 기준이다. preview-first UX는 사용자가 실제 표시 상태를 확인하고 적용하는 것을 우선한다.

## Current V1 Core UI Baseline

현재 chart panel의 구현 기준:

- chart 왼쪽 세로 rail에 select, pan, drawing tool mode를 둔다.
- chart 상단 toolbar는 viewport/timeframe, zoom/pan, candle/volume/MA layer, selected drawing edit/style/delete/clear-all, comparison, chart undo/redo, Agent proposal, preview toggle/apply를 둔다.
- MA5/MA20/MA60은 dropdown checkbox로 표시한다.
- selected drawing label은 floating input으로 편집한다.
- selected drawing style은 최소 색상 toggle 수준으로 제공한다.
- clear all drawings는 grouped remove command로 실행하고 chart undo 한 번으로 복원할 수 있다.
- chart panel header는 현재 symbol을 title로, 종목명을 description으로 표시한다.
- live 상태가 아니면 chart panel header의 가격/등락률은 `-`로 표시한다.
- top search/watchlist가 만든 symbol 변경은 external chart command scope로 취급해 chart panel undo 대상에서 제외한다.

현재 Agent/SystemArea 기준:

- Agent 01 단독 선택일 때만 chart command proposal/chat 입력을 활성화한다.
- Agent 02~04 또는 multi-agent 선택 상태는 소개와 비활성 composer만 보여준다.
- Agent chat reference token은 현재 symbol 하나만 표시한다. news, candle, drawing, comparison token은 후속 Agent/Context contract에서 정의한다.
- Agent chat signal light는 Agent/LLM 상태 전용이다. chart data stream 상태는 chart panel 내부 stream/status UI에서 다룬다.

## Auto Toggle Policy

Global auto toggle은 계속 유지하지만 capability별로 자동 적용 가능 여부를 나눈다.

기본 정책:

- `chart.viewport.set`: `autoApplyEligible: true`
- `chart.layer.visibility.set`: `autoApplyEligible: true`
- `chart.timeframe.set`: `autoApplyEligible: true`
- `chart.symbol.set`: V1에서는 `autoApplyEligible: false` 권장
- `chart.drawing.*`: `autoApplyEligible: false`, `previewable: true`
- `chart.comparison.*`: `autoApplyEligible: false`, `previewable: true`
- 주문/계좌/체결 command: chart auto toggle 범위 밖

LLM이 drawing이나 comparison을 제안하면 auto on 상태라도 preview로 들어간다.

Backend AI Agents Service scaffold는 shared canonical chart command 전체가 아니라 LLM이 생성 가능한 안전 subset을 OpenAI strict schema로 노출한다. 현재 generation subset은 symbol/timeframe/viewport/layer visibility, drawing add, comparison add, measurement add 중심이며, preview control과 remove/select/update 계열 command는 runtime/user UI command로 남긴다.

## Command Scope

V1 command 후보:

```text
chart.drawing.add
chart.drawing.update
chart.drawing.remove
chart.drawing.select
chart.drawing.clearSelection
chart.preview.set
chart.preview.toggle
chart.preview.apply
chart.preview.clear
chart.comparison.add
chart.comparison.remove
chart.comparison.update
chart.measurement.add
```

원칙:

- command는 항상 `panelId`와 `chartDocumentId`를 target으로 가진다.
- no-op command는 history에 남기지 않는다.
- grouped apply는 하나의 chart history entry가 된다.
- invalid command는 document를 변경하지 않는다.
- LLM command는 `actor: "llm"`이며 rationale을 포함한 proposal을 통해서만 들어온다.
- `ChartPendingPreview.visible`은 shared contract에 포함한다. `visible: false`는 hidden pending preview이며 apply 불가 상태다.

## Tool Registry

도구는 React component 안에 하드코딩하지 않는다.

필요 registry:

- `chartToolRegistry`: tool mode와 toolbar 표시 기준.
- `drawingRegistry`: drawing type, anchor count, renderer, hit tester.
- `commandRegistry`: command validation과 apply handler.
- `rendererRegistry`: layer renderer 등록.
- `capabilityManifest`: LLM에 노출 가능한 도구 설명.

새 도구 추가 순서:

1. shared chart contract에 command/capability 추가.
2. command validation과 document mutation 추가.
3. drawing registry 또는 overlay registry 추가.
4. renderer와 hit tester 추가.
5. UI tool control 추가.
6. LLM capability manifest와 proposal validation 추가.
7. Playwright regression 추가.

## Reference Source Policy

`/ref`는 계속 reference-only다.

필수 참고 대상:

- `lightweight-charts`: time scale, price scale, candle width, series rendering.
- `klinecharts`: drawing/overlay/indicator tool model.
- `uplot`: cursor, zoom, high-performance coordinate handling.
- `technicalindicators`: indicator input/output shape와 warmup 구간.

코드 직접 복사/이식은 금지한다. 비교 결과는 구현 PR 또는 보고서에 짧게 남긴다.

## Out of Scope for V1

- AWS 물리 서비스 분리.
- 실제 주문/체결/잔고 command.
- server-side chart image rendering.
- 모든 보조지표 완성.
- Agent/Context reference token contract 완성.
- Agent 02~04 chart command 권한과 multi-agent orchestration 활성화.
- Playwright/browser regression 완결.
- multi-user persistence.
- Workspace-level grouped history.
- layout command와 chart command가 섞인 mixed proposal.
