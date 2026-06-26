# GOPS Chart Tool Runtime V1 Goal Brief

이 문서는 Forge에게 Chart Tool Runtime V1 관련 대형 구현 또는 hardening 작업을 전달할 때 쓰는 brief 형식이다.

## Goal

GOPS 차트를 단순 candle renderer에서 사용자와 LLM이 함께 조작할 수 있는 분석 도구 runtime으로 고도화한다.

구현 목표 이름:

```text
Chart Tool Runtime V1
```

현재 구현 상태명:

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

현재 core runtime은 기준선으로 수용한다. 후속 Forge 요청은 이 기준선을 깨뜨리지 않고 validation hardening, regression, reference comparison, real provider 전환 준비를 좁은 범위로 명시해야 한다.

## Must Read

- `AGENTS.md`
- `docs/process/codex-workflow.md`
- `docs/architecture/service-boundaries.md`
- `docs/planning/chart-tool-runtime-v1.md`
- `docs/planning/chart-tool-runtime-milestones.md`
- `docs/planning/chart-rendering-runtime.md`
- `docs/spec/10-chart/gops-chart-spec.md`
- `docs/spec/20-market-data/market-data-pipeline-spec.md`
- `shared/chart-contract/README.md`
- `shared/chart-contract/chart-command.schema.json`
- `shared/chart-contract/chart-capabilities.json`
- 필요 시 `/ref/references`의 chart/indicator library source

## Implementation Order

1. M0-M6 core baseline을 읽고 보존할 동작을 확인한다.
2. 요청받은 hardening 또는 feature 범위가 shared contract와 service boundary를 침범하지 않는지 확인한다.
3. 필요한 경우 M7 regression, multi-chart, reference comparison을 우선 강화한다.
4. 새 chart tool은 shared contract, registry, command validation, renderer, UI, LLM capability, regression 순서로 추가한다.

큰 설계 충돌이 있으면 바로 구현을 밀지 말고 Navigator에 보고한다.

## Required Product Behavior

- Drawing은 pixel 좌표가 아니라 data-coordinate anchor로 저장한다.
- 사용자의 직접 조작과 LLM 제안은 같은 `ChartCommand` contract를 사용한다.
- LLM은 `ChartDocument`를 직접 변경하지 않는다.
- LLM drawing/comparison proposal은 preview-first다.
- Preview는 `ChartDocument.drawings`에 들어가지 않는다.
- 새 LLM drawing proposal은 기존 pending preview를 덮어쓴다.
- Preview toggle은 show/hide만 바꾼다.
- Hidden preview는 apply할 수 없다. 다시 표시한 뒤 적용한다.
- Apply preview는 pending preview를 grouped command로 적용한다.
- Apply 후 drawing은 일반 편집 가능한 object가 된다.
- Applied drawing은 chart panel-local undo/redo 대상이다.
- Preview state 자체는 undo/redo 대상이 아니다.
- chart/layout auto toggle은 drawing/comparison을 즉시 적용하지 않는다.

## Required Tools

P0 drawing/analysis tools:

- horizontal line
- trend line
- vertical marker
- text label
- point marker
- arrow
- rectangle/range zone
- measurement tool
- comparison overlay

P1 tools may be prepared but do not block V1 completion:

- ellipse/circle
- risk/reward box
- Fibonacci retracement
- RSI/MACD panes
- Bollinger Bands
- VWAP

## Required Commands

Add shared contract and runtime support for at least:

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

Command rules:

- Every command targets `panelId` and `chartDocumentId`.
- Invalid command does not mutate chart state.
- No-op command is not recorded in history.
- Grouped preview apply creates one history entry.
- Undo/redo is chart panel-local.

## Required UI

- Chart panel has a tool palette for drawing/select/edit modes.
- Chart panel has `Preview toggle` and `Apply preview` buttons.
- Proposal 없음: preview/apply buttons disabled.
- Proposal 있음 + visible: preview toggle on, apply enabled.
- Proposal 있음 + hidden: preview toggle off, apply disabled.
- Apply 이후: pending preview removed, buttons disabled until next proposal.

## Required LLM Behavior

- Agent 01 can propose drawing/comparison commands.
- Agent 01 단독 선택 상태에서만 chart command chat/proposal 입력을 활성화한다.
- Agent 02~04와 multi-agent mode는 현재 chart command chat disabled scaffold다.
- LLM output must include rationale.
- LLM must not return pixel coordinates.
- LLM drawing/comparison proposal is preview-first even when global auto toggle is on.
- LLM may combine tools, but only through capability manifest and validation.
- Unsupported or unsafe commands are rejected.

## Out of Scope

- AWS physical service separation.
- Real order/trading commands.
- Server-side chart image rendering.
- Mobile-specific viewport/layout support.
- Multi-user persistence.
- Workspace-level grouped history.
- Mixed layout+chart proposal.
- Full real provider integration beyond preserving current market data boundary.

## Verification Requirements

Run and report:

- `npm run build`
- `npm run test:chart`
- `.venv/bin/python -m compileall backend/app` if backend changes.
- `.venv/bin/python -m unittest backend.tests.test_chart_runtime` if backend changes.
- Browser regression for desktop canvas.
- Drawing render/edit/undo/redo browser test.
- Preview toggle/apply browser test.
- Hidden preview apply rejection test.
- Auto on preview-first test.
- Multi-chart panel browser scenario.
- `/ref/references` behavior comparison summary.

Do not mark V1 complete if browser/canvas verification is skipped.

## Report Format

```text
Navigator 전달용 보고서

출처 채팅방:
자동 전달 여부:
보고 대상 작업:
현재 상태:
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
