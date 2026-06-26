# GOPS Chart Tool Runtime V1 Milestones

## Purpose

이 문서는 Chart Tool Runtime V1의 core implementation baseline과 남은 validation hardening backlog를 관리하기 위한 마일스톤이다.

현재 구현 상태명:

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

M0-M6의 core runtime은 구현 기준선으로 수용한다. M7의 browser regression, multi-chart browser scenario, `/ref/references` behavior comparison은 아직 hardening backlog다.

## Common Done Criteria

새 구현이나 hardening 작업은 다음 조건을 만족해야 완료로 본다.

- `npm run build` 통과.
- `npm run test:chart` 통과.
- backend 변경 시 `.venv/bin/python -m compileall backend/app` 통과.
- backend 변경 시 `.venv/bin/python -m unittest backend.tests.test_chart_runtime` 통과.
- 실제 브라우저에서 desktop chart panel을 확인.
- Canvas screenshot/pixel 검증으로 nonblank, draw order, 주요 도구 표시 확인.
- 실패 항목은 수정 후 같은 검증을 다시 실행.

모바일 전용 viewport와 모바일 UI 레이아웃은 현재 V1 완료 기준에 포함하지 않는다. 반응형 검증은 desktop workspace 안에서 panel 크기와 variant가 바뀌는 경우를 우선한다.

## M0. Baseline Audit and Stabilization

상태: core baseline 완료.

목표:

- 현재 구현을 다시 읽고 Chart Tool Runtime V1 구현에 재사용할 부분과 교체할 부분을 구분한다.

포함:

- `frontend/src/chart/*`, `frontend/src/components/ChartPanel.tsx`, `backend/app/*`, `shared/chart-contract/*` 확인.
- current renderer, command runtime, proposal runtime, service boundary가 V1 문서와 충돌하는 부분 기록.
- 기존 기능이 깨지지 않도록 smoke test baseline 확보.

완료 기준:

- 기존 candle/volume/MA/pan/zoom/crosshair/LLM proposal 흐름이 유지된다.
- V1 구현 전에 남길 compatibility note가 정리된다.

## M1. Scale, Pane, Coordinate Transform Core

상태: core baseline 완료. reference behavior comparison은 hardening backlog.

목표:

- drawing과 comparison이 의존할 좌표 변환 기반을 만든다.

포함:

- `TimeScale`
- `PriceScale`
- `PercentScale`
- `VolumeScale`
- `PaneLayout`
- `CoordinateTransform`
- visible range와 logical range 분리
- device pixel ratio 대응 유지

완료 기준:

- timestamp/price anchor가 screen coordinate로 안정 변환된다.
- pan/zoom/resize 후 같은 anchor가 올바른 candle/time/price 위치에 남는다.
- `/ref/references`의 time/price scale 설계를 최소 1회 비교하고 기록한다.

검증:

- scale unit test.
- resize 후 anchor coordinate consistency test.
- desktop screenshot.

## M2. Shared Contract, Registry, Document Model

상태: core baseline 완료. shared schema packaging과 generated validation은 hardening backlog.

목표:

- drawing/comparison command와 registry 기반 확장 구조를 추가한다.

포함:

- `DrawingEntity`, `DrawingAnchor`, `DrawingStyle`.
- `ChartDocument.drawings`, `ChartDocument.comparisons`, selection state.
- `chartToolRegistry`, `drawingRegistry`, `commandRegistry`, `rendererRegistry`.
- shared contract와 frontend/backend mirror 갱신.
- capability manifest에 drawing/comparison/preview 추가.

완료 기준:

- 새 drawing tool은 registry 등록으로 추가 가능하다.
- LLM capability manifest가 drawing command를 설명한다.
- invalid drawing/comparison command는 document를 변경하지 않는다.

검증:

- command validation unit test.
- no-op history 제외 test.
- schema JSON validation.

## M3. P0 Drawing Tools

상태: core baseline 완료.

목표:

- 분석에 필요한 최소 drawing 도구를 구현한다.

포함:

- `horizontalLine`
- `trendLine`
- `verticalMarker`
- `textLabel`
- `pointMarker`
- `arrow`
- `rangeBox`
- `measurement`

P1 drawing type인 `ellipse`, `riskRewardBox`, `fibonacciRetracement`는 schema/registry 준비 수준으로만 본다. toolbar, renderer, hit tester, regression이 갖춰지기 전까지 V1 core 완료 범위로 보지 않는다.

완료 기준:

- 모든 drawing은 data-coordinate anchor로 저장된다.
- `horizontalLine`은 price-only anchor를 허용한다.
- drawing은 Canvas drawing layer에 렌더링된다.
- pan/zoom/resize 후 drawing이 의미 위치를 유지한다.
- measurement는 가격 변화, percent 변화, 기간 정보를 표시한다.

검증:

- 각 drawing type render unit/snapshot helper test.
- browser screenshot/pixel check.
- pan/zoom 후 drawing position consistency check.

## M4. Selection, Edit, Delete, Undo/Redo

상태: core baseline 완료.

목표:

- 적용된 drawing을 사용자가 직접 편집할 수 있게 한다.

포함:

- select tool mode.
- hit testing.
- hover/selected affordance.
- drag move.
- anchor handle edit.
- label/style edit 최소 UI.
- delete/remove.
- clear all drawings grouped remove.
- chart-local undo/redo.

완료 기준:

- drawing select/move/edit/delete가 모두 command를 통해 실행된다.
- 직접 object mutation 없이 command runtime이 document를 변경한다.
- LLM으로 적용된 drawing도 사용자 drawing과 동일하게 편집 가능하다.
- grouped drawing apply는 undo 한 번으로 되돌아간다.

검증:

- hit testing unit test.
- drag edit browser test.
- undo/redo isolation test.

## M5. Comparison Overlay

상태: core baseline 완료. legend/cache UX 고도화는 hardening backlog.

목표:

- 둘 이상의 종목/ETF를 한 chart panel에서 비교한다.

포함:

- `chart.comparison.add`
- `chart.comparison.remove`
- `chart.comparison.update`
- comparison data request/cache integration.
- percent scale rendering.
- comparison legend/label.
- comparison command undo/redo.
- current symbol은 comparison 후보에서 제외한다.
- 이미 추가된 comparison symbol은 active 상태로 보이며 다시 클릭하면 remove한다.

완료 기준:

- comparison line이 main price scale을 왜곡하지 않는다.
- visible range 시작점을 기본 percent 기준으로 사용한다.
- pan/zoom/live update 이후 comparison path가 안정적이다.
- comparison data failure가 main candle rendering을 깨뜨리지 않는다.

검증:

- two-symbol comparison test.
- percent scale test.
- browser screenshot check.

## M6. LLM Preview-First Proposal Flow

상태: core baseline 완료. Agent/Context reference token 확장은 hardening backlog.

목표:

- LLM drawing/comparison proposal이 preview로 표시되고, 사용자가 적용하면 편집 가능한 object가 되게 한다.

포함:

- `pendingPreview` runtime state.
- `proposalPreviewLayer`.
- `chart.preview.set`
- `chart.preview.toggle`
- `chart.preview.apply`
- `chart.preview.clear`
- chart panel `Preview toggle`, `Apply preview` buttons.
- LLM prompt/schema 확장.
- preview replacement policy.

정책:

- proposal 없음: preview/apply buttons disabled.
- proposal 있음: preview button toggles visibility, apply button enabled.
- preview hidden이면 apply button disabled.
- preview hidden 상태의 apply command는 실패한다.
- 새 LLM drawing proposal은 기존 pending preview를 덮어쓴다.
- apply 후 preview는 제거된다.
- preview state는 chart undo/redo 대상이 아니다.
- applied drawing은 chart undo/redo 대상이다.

완료 기준:

- auto on이어도 drawing/comparison proposal은 바로 document에 적용되지 않는다.
- apply preview 이후 일반 drawing object로 편집 가능하다.
- reject/clear 이후 preview artifact가 남지 않는다.

검증:

- auto off preview test.
- auto on preview-first test.
- apply preview grouped undo/redo test.
- hidden preview apply rejection test.
- new proposal replaces previous preview test.
- browser Agent 01 proposal flow test.

## M7. Regression, Multi-chart, Reference Comparison

목표:

- Chart Tool Runtime V1 core baseline을 검증 가능한 장기 기준선으로 강화한다.

포함:

- Playwright 또는 동등한 browser regression script.
- desktop canvas nonblank.
- crosshair diff.
- drawing render and edit.
- preview toggle/apply.
- unsupported symbol.
- multi-chart panel scenario.
- `/ref/references` behavior comparison checklist.
- Agent 01 chat preview-first flow.
- Agent signal light Agent/LLM-only state check.

완료 기준:

- npm script로 browser regression을 실행할 수 있다.
- chart panel 2개가 독립 `chartDocumentId`, viewport, drawing history를 유지한다.
- 같은 symbol/timeframe candle cache 공유와 chart state 분리가 확인된다.
- reference comparison 결과가 문서 또는 보고서에 남는다.

현재 상태:

- `npm run test:chart`, backend unittest, build 검증은 core baseline에서 통과한 이력이 있다.
- Playwright/browser regression script와 multi-chart browser scenario는 아직 backlog다.

검증:

- `npm run build`
- `npm run test:chart`
- 신규 browser regression script.
- backend compile/unit test.

## Final Report Required

Forge는 완료 보고에 다음을 포함한다.

```text
작업 목적:
완료한 마일스톤:
변경 파일:
구현 내용:
검증 방법:
검증 결과:
브라우저/canvas 검증 결과:
LLM preview-first 검증 결과:
/ref/references 비교 내용:
남은 이슈 또는 미확정 사항:
Navigator에서 결정할 사항:
추천 다음 단계:
```
