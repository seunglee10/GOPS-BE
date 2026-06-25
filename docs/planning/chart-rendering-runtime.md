# GOPS Chart Rendering Runtime Planning

## 목적

이 문서는 GOPS 차트 패널의 Custom Canvas 렌더링 도구를 구현하기 위한 요구 기준이다.

현재 목표는 `/ref`의 이전 실험을 그대로 가져오는 것이 아니라, GOPS의 Bento Grid 패널 런타임과 팀원별 스펙 경계에 맞는 차트 렌더링 구조를 새로 정의하는 것이다.

차트는 전역 singleton 화면이 아니라 여러 패널 중 하나다. 같은 화면에 chart panel이 여러 개 존재할 수 있으며, 각 chart panel instance는 독립적인 `chartDocumentId`를 통해 자신의 `ChartDocument`를 참조한다.

## `/ref` 참고 원칙

`/ref` 폴더는 절대 참고 전용이다.

- `/ref`의 코드를 현재 GOPS 코드로 직접 복사하거나 이식하지 않는다.
- `/ref`의 UI, panel layout, chat/proposal 배치는 현재 UI 기준으로 보지 않는다.
- 참고 가능한 것은 설계 개념이다: `ChartDocument`, command/proposal, capability manifest, candle merge, render scene, scale model, Canvas layer 분리.
- `/ref/references`의 실제 차트/지표 라이브러리 source는 구현 중 반드시 참고할 수 있다. 주식 차트는 장난감이 아니므로, scale, pane, candle, volume, indicator, interaction 동작은 검증된 라이브러리의 설계와 비교하며 구현한다.
- `klinecharts`는 pane, indicator, overlay, drawing API 설계 참고에 사용한다.
- `lightweight-charts`는 financial chart rendering, time scale, series API, plugin model 참고에 사용한다.
- `uplot`은 고성능 time-series rendering, scale, cursor/zoom interaction 참고에 사용한다.
- `technicalindicators`는 지표 입력값, warmup, 계산 결과 shape 참고에 사용한다.
- `/ref`의 vendor source, `.venv`, `node_modules`, `dist`는 구현 대상도 runtime dependency도 아니다.
- 구현자는 `/ref`를 문제 해결 힌트로 보고, 현재 `frontend/`, `backend/`, `docs/` 기준에 맞춰 새로 작성한다.

## 책임 경계

Chart Rendering Runtime이 책임지는 것:

- 시장 데이터 API/WebSocket 응답을 chart runtime이 쓰는 candle state로 정규화한다.
- `ChartDocument`를 기준으로 symbol, timeframe, viewport, panes, layers, style, interaction state를 관리한다.
- 사용자 조작과 LLM 제안을 chart command로 검증하고 적용한다.
- `ChartDocument`, candle state, calculation output에서 `RenderScene`을 파생한다.
- `RenderScene`을 Custom Canvas layer로 그린다.
- chart panel 내부 undo/redo history를 관리한다.

Chart Rendering Runtime이 책임지지 않는 것:

- Alpaca 원본 포맷, Kafka/Flink 처리, Redis/ClickHouse/S3 저장 구현
- 공식 지표 계산의 authoritative 결과 생성
- OpenAI API key 관리와 LLM API 직접 호출
- 주문 접수, 체결, 계좌, KIS adapter, 주문 멱등성
- Bento Grid panel 위치/크기 reflow
- `/ref` 코드의 보존 또는 유지보수

## 데이터 입력 계약

MVP 첫 구현은 FastAPI backend dummy endpoint에서 데이터를 받는다. 다만 데이터 shape는 실제 시장 데이터 파이프라인이 프론트엔드로 전달할 계약에 맞춘다.

초기 snapshot은 시장 데이터 스펙의 `/api/charts/candles` 응답 형식을 따른다.

```ts
type CandleData = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  isClosed: boolean;
  ma5?: number;
  ma20?: number;
  ma60?: number;
};
```

실시간 업데이트는 다음 WebSocket message type을 기준으로 한다.

- `LIVE_CANDLE_UPDATE`: 진행 중인 candle을 같은 timestamp 기준으로 갱신한다.
- `CANDLE_CLOSED`: 확정 candle을 추가하거나 같은 timestamp candle을 교체한다.
- `CANDLE_CORRECTED`: 이미 확정된 candle을 같은 timestamp 기준으로 보정한다.

렌더링 담당 코드는 Alpaca 원본 이벤트나 Kafka Raw 메시지를 직접 사용하지 않는다.

## 런타임 파이프라인

차트 렌더링은 다음 단방향 파이프라인을 따른다.

```text
MarketDataAdapter -> CandleStore -> ChartDocument -> RenderScene -> CanvasLayerRenderer
```

- `MarketDataAdapter`: FastAPI dummy 응답과 future REST/WebSocket 메시지를 `CandleData`로 정규화한다.
- `CandleStore`: `symbol + timeframe` 기준 candle 배열을 보관하고 snapshot/live/corrected update를 적용한다.
- `ChartDocument`: chart panel이 참조하는 상태 문서다. symbol, timeframe, viewport, panes, layers, style, interaction state, history pointer를 가진다.
- `RenderScene`: document와 data에서 파생되는 순수 렌더링 입력이다. renderer가 document/data를 수정하지 않도록 경계를 만든다.
- `CanvasLayerRenderer`: grid, candles, volume, moving average, crosshair, drawing preview 같은 layer를 정해진 draw order로 그린다.

## ChartDocument 요구사항

`ChartDocument`는 chart panel 내부 상태의 기준이다.

MVP에서 필요한 최소 필드:

- `id`
- `symbol`
- `timeframe`
- `viewport`
- `panes`
- `layers`
- `style`
- `interactionState`
- `history`
- `future`
- `updatedAt`

`ChartDocument`는 panel layout state와 분리한다. `layoutPinned`은 chart panel의 위치와 크기를 고정할 뿐이며, symbol/timeframe/viewport/layer 같은 chart 내부 상태 변경을 막지 않는다.

## ChartCommand 요구사항

사용자 조작, LLM 제안, system action은 같은 chart command 처리 경로를 사용한다.

MVP 우선 command:

- `chart.symbol.set`
- `chart.timeframe.set`
- `chart.viewport.set`
- `chart.layer.visibility.set`
- `chart.undo`
- `chart.redo`

후속 command:

- `chart.indicator.add`
- `chart.indicator.update`
- `chart.indicator.remove`
- `chart.comparison.add`
- `chart.comparison.remove`
- `chart.drawing.add`
- `chart.drawing.update`
- `chart.drawing.remove`

모든 chart command는 `panelId`와 `chartDocumentId`를 명확히 target으로 가진다. 잘못된 target, 지원하지 않는 command type, 잘못된 payload, no-op command는 chart document를 변경하지 않는다.

no-op chart command는 chart history에 남기지 않는다.

## Capability Manifest와 LLM 도구 조합

chart capability manifest는 단순 command allowlist가 아니다. LLM이 어떤 chart tool과 command를 왜 조합할 수 있는지 판단하기 위한 공개 가능한 도구 설명서다.

각 capability는 최소한 다음 metadata를 가진다.

- `id`
- `label`
- `description`
- `commandTypes`
- `payloadSchema`
- `requiredContext`
- `previewable`
- `autoApplyEligible`
- `undoScope`
- `conflictsWith`
- `recommendedWith`
- `validationRules`

LLM은 사용자의 명시적 요청뿐 아니라 market summary, visible chart context, active layers, stream status를 바탕으로 여러 chart command를 조합할 수 있다. 예를 들어 급격한 가격 변동이 감지되면 viewport 조정, MA 표시, volume 강조, comparison 추가를 하나의 proposal로 제안할 수 있다.

예상 밖의 도구 조합은 허용한다. 다만 의미 있는 조합이어야 하며, 다음 조건을 모두 만족해야 한다.

- capability manifest에 노출된 command와 tool만 사용한다.
- required market/chart context가 충분하다.
- command validation을 통과한다.
- 충돌하는 capability 조합을 포함하지 않는다.
- 사용자에게 보여줄 rationale을 포함한다.
- auto on 즉시 적용 대상은 `autoApplyEligible`이 true인 chart/layout 분석 UI command로 제한한다.

## LLM 제안과 Auto Toggle

Top app bar의 전역 auto toggle은 layout command와 chart command의 LLM 적용 정책을 함께 제어한다.

- auto off: LLM chart command는 pending `ChartProposal`로 저장하고 사용자가 승인해야 적용한다.
- auto on: LLM chart proposal은 validation을 통과한 뒤 즉시 grouped apply된다.
- LLM이 만든 proposal 하나는 chart history에서 하나의 undo/redo 단위로 기록한다.
- LLM 응답은 `ChartDocument`를 직접 변경하지 않는다. 항상 chart command 또는 proposal로만 들어온다.

전역 auto toggle은 layout/chart 분석 UI command에만 적용한다. 주문 생성, 주문 취소, 주문 정정, 계좌/잔고/체결 변경 같은 거래 command에는 절대 적용하지 않는다.

chart panel 내부 undo/redo 버튼은 해당 chart panel의 chart history만 되돌린다. top app bar의 undo/redo는 layout history 전용으로 유지한다.

MVP에서는 한 proposal이 하나의 history scope만 변경한다. layout command와 chart command가 한 LLM proposal에 함께 들어가는 복합 적용은 후속 단계에서 `Workspace-level grouped history`로 확장한다.

## Canvas 렌더링 요구사항

Custom Canvas renderer는 `RenderScene`만 읽는다.

MVP 렌더링 대상:

- background
- grid
- candlestick
- volume bar
- `ma5`, `ma20`, `ma60` line
- axes label
- crosshair
- loading, empty, error state

MVP 이후 렌더링 대상:

- indicator pane
- comparison line
- horizontal line drawing
- proposal preview layer
- selected/hovered layer affordance

Canvas는 `ResizeObserver`와 device pixel ratio를 반영한다. chart panel 크기가 Bento Grid 조작으로 바뀌어도 candle, volume, axis, crosshair가 비정상적으로 늘어나거나 흐려지지 않아야 한다.

## 렌더링 검증 루프

Custom Canvas 구현은 “빌드 통과” 또는 “캔버스가 비어 있지 않음”만으로 완료하지 않는다. 각 렌더링 마일스톤은 다음 루프를 반복한다.

```text
구현 -> 단위 테스트 -> 브라우저 렌더 확인 -> canvas pixel/screenshot 확인 -> reference 동작 비교 -> 보완 -> 재검증
```

검증 기준:

- candle body와 wick이 OHLC 값에 맞는 y 좌표에 그려진다.
- 상승/하락 색상, volume bar, MA line, axis label, crosshair가 의도한 draw order로 보인다.
- `devicePixelRatio`가 1이 아닌 화면에서도 선과 텍스트가 흐릿하거나 어긋나지 않는다.
- compact, standard, wide, large panel에서 텍스트와 chart가 겹치지 않는다.
- desktop/mobile viewport, 작은 panel/큰 panel, resize 직후에도 Canvas가 nonblank이며 비율이 깨지지 않는다.
- pan/zoom/crosshair 같은 interaction 이후 data state와 chart document가 의도 없이 바뀌지 않는다.
- 필요한 경우 `/ref/references`의 `lightweight-charts`, `klinecharts`, `uplot` 동작을 읽고 time scale, value scale, pane, cursor 정책을 비교한다.

구현자는 실패한 항목을 고친 뒤 같은 검증을 다시 실행한다. 이 루프가 통과하기 전에는 해당 렌더링 마일스톤을 완료로 보지 않는다.

## Panel Variant 기준

chart panel은 size variant에 따라 UI 밀도를 바꾼다.

- `compact`: 작은 sparkline 또는 축약 candlestick, 핵심 가격/변화율 중심
- `standard`: candlestick, volume, 기본 MA, 최소 toolbar
- `wide`: 시간축과 crosshair readout을 강화
- `large`: chart tools, indicator controls, proposal preview affordance까지 표시

variant 변경은 panel placement 변경에서 파생된다. chart data나 `ChartDocument` identity를 초기화하지 않는다.

## 검증 기준

- 같은 `symbol + timeframe + timestamp` candle update는 기존 candle을 교체한다.
- 오래된 timestamp update는 무시한다.
- snapshot/live/corrected update가 viewport와 chart configuration을 초기화하지 않는다.
- renderer는 `ChartDocument` 또는 `CandleStore`를 수정하지 않는다.
- chart panel이 2개 이상이어도 각 panel의 `chartDocumentId`, viewport, history가 분리된다.
- chart panel resize 후 Canvas가 빈 화면이 아니고 축/캔들/거래량이 다시 그려진다.
- auto off LLM 제안은 pending proposal로 남고 chart document를 바꾸지 않는다.
- auto on LLM 제안은 validation 후 적용되고 한 번의 chart undo로 되돌릴 수 있다.
