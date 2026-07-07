# msg4agent.md

이 문서는 GOPS UI 에이전트, 특히 레이아웃 및 패널 관리 에이전트를 수정할 때 함께 참고하기 위한 작업 노트다. 레이아웃 모델이 바뀔 때마다 이 문서도 함께 CRUD한다.

## 현재 파악한 UI 에이전트 흐름

현재 프론트엔드의 레이아웃 에이전트 흐름은 다음 경로를 따른다.

1. `App.tsx`가 현재 패널 상태와 viewport를 읽는다.
2. `buildTiledAgentLayoutContext()`가 패널 목록, 배치 정보, 최소/최대 span, 활성 심볼을 포함한 `layoutContext`를 만든다.
3. Agent 또는 resolver가 `layoutProposal`을 반환한다.
4. `applyTiledAgentLayoutProposal()`이 proposal command를 읽고 같은 레이아웃 reducer 계열 함수로 상태를 갱신한다.
5. React는 갱신된 `TiledPanelState`를 다시 렌더링한다.

중요한 원칙은 에이전트가 DOM을 직접 조작하지 않는다는 점이다. 에이전트는 사람이 쓰는 UI 편집 기능과 같은 layout state/reducer contract를 통해서만 패널을 이동, 추가, 삭제, 교체, 정렬한다.

## 새 8x5 Grid Contract

레이아웃의 기준 좌표계는 8열 x 5행 grid다.

- 좌표는 1-based다.
- `gridRect`는 `{ col, row, colSpan, rowSpan }` 형태다.
- `col` 범위는 `1..8`, `row` 범위는 `1..5`다.
- `colSpan`과 `rowSpan`은 grid 밖으로 나가면 안 된다.
- `gridRect`가 공식 layout contract다. localStorage, Agent proposal, 수정모드 기준은 항상 이 값이다.
- `rect`는 일반모드 boundary resize에서만 쓰는 임시 렌더 상태다. 수정모드 진입, Agent context 생성, 저장 직전에는 `rect`를 결정적인 규칙으로 `gridRect`에 정규화한다.
- gutter는 기존 `panelGutter()`/grid 계열 값을 재사용한다.
- local 저장 key는 `gops:workspace-grid-layout:v1`이다.

기본 배치는 다음과 같다.

- 뉴스: `{ col: 1, row: 1, colSpan: 4, rowSpan: 2 }`
- 온톨로지: `{ col: 5, row: 1, colSpan: 4, rowSpan: 2 }`
- 차트: `{ col: 1, row: 3, colSpan: 8, rowSpan: 3 }`

## Panel Registry

패널의 이름, kind, agent panel type, 최소 span, 기본 span, 기본 props는 `apps/gops-frontend/src/layout/panelRegistry.ts`에서 관리한다.

수정모드 Dock 표기는 `패널명(세로x가로)` 형식을 사용한다. 예를 들어 차트의 최소 span이 `rowSpan: 2`, `colSpan: 2`이면 `차트(2x2)`로 표시한다.

최소 크기는 앞으로 자주 바뀔 수 있으므로 개별 컴포넌트나 에이전트 command 내부에 흩어두지 않는다. 새 패널을 추가하거나 최소 span을 바꿀 때는 registry를 먼저 수정하고, 레이아웃 reducer와 Agent context가 registry 값을 읽도록 유지한다.

## 일반모드와 Human Edit Mode

일반모드와 수정모드는 책임이 분리된다.

- 일반 모드에서는 기존 차트, 뉴스, 온톨로지 등 패널 내부 기능을 그대로 사용한다.
- 일반 모드에서 허용되는 레이아웃 조작은 패널 사이 공유 경계 resize뿐이다.
- 일반 모드 boundary resize는 `rect`만 바꾸며, `gridRect`는 즉시 바꾸지 않는다. 이 `rect`는 사용자 편의를 위한 임시 화면 상태지만, 패널끼리 겹치면 안 된다.
- 일반 모드에서 큰 패널의 경계가 여러 패널과 맞닿아 있으면, 같은 경계에 걸린 패널들이 함께 영향을 받는다. 영향을 받는 패널 중 하나라도 registry 기반 최소 pixel 크기에 도달하면 더 이상 resize하지 않는다.
- 일반 모드에서는 패널 추가, 삭제, 교체, 이동 UI를 제공하지 않는다. 경계 `+`, 일반 패널 `x`, 차트 내부 close/swap handle은 사용하지 않는다.
- 수정모드에서는 8x5 가상 grid overlay를 보여준다.
- 수정모드의 패널 본문은 snapshot처럼 보이되 pointer interaction은 비활성화한다.
- 각 패널에는 8방향 resize button과 중앙 delete button을 렌더링한다.
- 버튼이 아닌 패널 영역을 드래그하면 패널 이동으로 처리한다.
- Dock의 패널 버튼을 드래그하면 새 패널 추가 또는 기존 패널 교체로 처리한다.
- 이동, resize, 추가, 교체 중에는 예상 결과 preview를 보여준다.
- 충돌, grid 밖 이동, 최소 span 미달은 적용하지 않고 invalid preview로만 표시한다.
- resize 중 기존 패널과 충돌하면 직접 겹치는 패널은 각자의 최소 span까지 edge를 줄여 자리를 양보할 수 있다. 이 양보 역시 DOM 조작이 아니라 layout reducer contract로 계산한다.
- 수정모드에서 빈 칸에 패널을 drop할 때는 현재 패널 크기나 registry `defaultSpan`을 우선하지 않는다. drop한 cell이 속한 4방향 연결 빈 영역 안에서 registry `minSpan` 이상인 최대 직사각형 `gridRect`를 계산하고, 비정형 빈 영역은 그 안에 들어가는 가장 큰 직사각형으로 preview/commit한다.

## Freeform Rect 정규화

일반모드 boundary resize로 패널의 실제 pixel `rect`가 grid와 어긋날 수 있다. 이 상태는 정상이며, 수정모드 진입이나 Agent 연결 시점에 다음 규칙으로 정규화한다.

- 각 패널의 현재 `rect` 중심점과 크기를 가장 가까운 grid cell/span으로 환산한다.
- 환산 span이 panel registry의 `minSpan`보다 작으면 같은 중심을 유지하면서 최소 span까지 확장한다.
- 처리 순서는 layout weight와 기존 slot 순서를 기준으로 고정한다.
- 이미 점유된 cell과 겹치면 가장 가까운 빈 non-overlap 후보를 deterministic scan으로 선택한다.
- 후보가 없으면 기존 valid `gridRect` 또는 최소 span 후보를 fallback으로 사용한다.
- 결과는 grid 밖으로 나가지 않고, 정상 사용 경로에서는 패널끼리 겹치지 않는다.

이 규칙은 복잡한 예외처리보다 단순하고 반복 가능한 결과를 우선한다. Agent도 이 정규화된 grid 상태만 읽는다.

## Agent Layout Commands

새 contract에서 `layoutContext.panels[].placement`는 8x5 grid 좌표를 내보낸다.

`maxSpan`은 항상 `{ colSpan: 8, rowSpan: 5 }`다. `minSpan`은 panel registry에서 읽는다.

현재 적용 대상 command는 다음과 같다.

- `layout.panel.add`: registry kind를 기준으로 패널을 추가한다.
- `layout.panel.remove`: 대상 패널을 삭제한다.
- `layout.panel.move`: 새 `gridRect`로 이동한다.
- `layout.panel.replace`: 기존 패널 content kind를 새 패널 kind로 교체한다.
- `layout.panels.arrange`: 여러 패널의 최종 `gridRect`를 batch로 검증한 뒤 적용한다.
- 수정모드 resize는 `resolvePanelResizeWithYield()`/`applyPanelResizeWithYield()` 계열 reducer를 기준으로 하며, 직접 충돌 패널만 최소 span까지 축소한다. 연쇄 밀기나 전체 재배치는 하지 않는다.

`layout.boundary.resize`는 legacy command로 간주한다. 새 Agent 설계에서는 gridRect 기반 command만 사용한다.

## Future Agent 연결 원칙

Agent가 실제 연결되면 다음 원칙을 지킨다.

- Agent는 DOM 좌표나 CSS selector를 반환하지 않는다.
- Agent는 `layoutContext`를 읽고 `layoutProposal.commands[]`만 반환한다.
- 적용은 항상 프론트엔드의 같은 layout reducer를 통해 이루어진다.
- 사람이 수정모드에서 보는 preview와 Agent proposal preview가 같은 자료구조를 쓰도록 설계한다.
- 자동 적용 전에 사용자 확인이 필요한 경우에도, proposal은 먼저 gridRect preview로 표현할 수 있어야 한다.
- 저장소 또는 DB 저장이 추가되더라도 v1 localStorage snapshot을 migration 가능한 입력으로 취급한다.

이 문서는 구현 진행 중 발견한 제약과 Agent 연결 결정을 계속 반영한다.
