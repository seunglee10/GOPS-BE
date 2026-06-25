# GOPS Frontend Layout Runtime Planning

## 목적과 현재 범위

이 문서는 GOPS 차트 구현 전에 단일 페이지 프론트엔드의 레이아웃 런타임 기준을 잡기 위한 계획 문서다. 현재 단계의 목표는 React + TypeScript 프론트엔드에서 Bento Grid 기반 workspace를 어떻게 표현하고, 사용자의 패널 조작과 향후 LLM command 조작을 같은 모델로 받을지 정의하는 것이다.

현재 UI/UX 기준은 `frontend/`에 구현된 scaffold를 우선한다. 이 문서와 구현이 충돌하면, 별도 제품 결정이 있기 전까지는 현재 구현을 기준으로 문서를 맞춘다. FastAPI 백엔드, 시장 데이터 수신, 실제 차트 렌더링, OpenAI 연동, WebSocket, 주문, GraphRAG는 이 문서의 구현 범위에서 제외한다. 차트 렌더링과 chart command 정책은 [chart-rendering-runtime.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-rendering-runtime.md)를 기준으로 한다.

현재 Navigator 결정:

- `swap-resize`는 MVP에서 구현하지 않는다. panel move 중 기존 panel의 `colSpan`/`rowSpan`을 target chunk 크기에 맞춰 자동 변경하지 않는다.
- no-op layout command는 layout history에 남기지 않는다. 같은 placement로 이동하거나 결과 layout이 바뀌지 않는 move/boundary resize/reflow는 무시한다.
- panel zone 자유도는 당분간 유지한다. workspace panel은 현재 구현처럼 `main`, `context`, `mainContext` 사이를 비교적 자유롭게 오갈 수 있으며, 타입별 zone 제약은 필요해질 때 registry에서 다시 좁힌다.
- top app bar의 전역 auto toggle은 LLM layout command와 LLM chart command의 적용 정책을 함께 제어한다. 단, 이 문서의 구현 범위는 layout command에 한정한다.
- top app bar undo/redo는 layout history 전용이다. chart panel 내부 undo/redo는 chart rendering runtime에서 별도로 관리한다.

## 전체 UI 구조

GOPS 초기 화면은 하나의 app shell 안에 다음 영역을 둔다.

- `top app bar`: 앱 전역 navigation, 사각형 검색 entry, favorite layout 4칸, layout undo/redo, LLM auto toggle, headline/alert strip, agent shortcut rail을 둔다. 저장은 top app bar 버튼이 아니라 settings panel의 Layouts 탭에서 수행한다.
- `workspace area`: top app bar 아래의 전체 작업 영역. Bento Grid runtime이 관리하는 주된 화면이다.
- `main workspace`: 왼쪽 3열 x 5행에 해당하는 주 작업 영역. 차트, watchlist, 분석 결과, 뉴스, 주문 후보 같은 핵심 패널을 배치한다.
- `context column`: 오른쪽에서 두 번째 1열 x 5행 영역. main workspace와 같은 workspace 그룹에 속하지만 더 넓은 보조 컬럼처럼 보일 수 있다. 현재 구현에서는 workspace 패널이 main/context/mainContext 사이를 비교적 자유롭게 오갈 수 있다.
- `system area`: 가장 오른쪽 1열 x 5행 영역. 기본 상태는 watch list이며, agent, notification, settings/menu가 켜지면 같은 오른쪽 영역 전체가 해당 system panel로 전환된다.

`layout.png` 기준 해석은 5x5처럼 보이는 grid를 출발점으로 삼는다. 다만 실제 CSS column width는 균등 5분할로 고정하지 않고, `main + context` workspace group과 오른쪽 `system area`를 같은 visual frame 안에서 분리해 표현한다.

## Grid Zone 모델

레이아웃 런타임은 모든 panel placement를 grid group과 zone 단위로 검증한다.

```ts
type GridGroup = "workspace" | "agentRail";
type GridZone = "main" | "context" | "mainContext" | "agentRail";
```

- `main`: 왼쪽 주 작업 영역. 기본 grid는 3 columns x 5 rows로 시작한다.
- `context`: 보조 컨텍스트 영역. 기본 grid는 1 column x 5 rows로 시작하며, 시각적으로 main column보다 넓을 수 있다.
- `mainContext`: `main`과 `context`를 함께 쓰는 workspace span이다. MVP에서도 허용한다.
- `agentRail`: 데이터 모델에는 남아 있는 agent 전용 zone이다. 현재 UI 구현에서는 별도 panel grid로 쓰기보다 오른쪽 `system area`와 top app bar의 agent shortcut rail로 표현한다.

MVP에서도 `main`과 `context` 사이를 span하는 패널을 허용한다. 예를 들어 큰 차트, 비교 분석, 다중 뉴스 요약 패널은 `workspace` group의 4개 column을 활용할 수 있다.

오른쪽 `system area`는 항상 workspace와 독립적이다. `main`, `context`, `mainContext` 패널이 오른쪽 system area까지 확장되어서는 안 된다.

## 비대칭 Column Width와 Agent Rail 분리 원칙

Bento Grid는 겉으로는 5열처럼 보이더라도 실제 column width가 대칭일 필요는 없다.

- `workspace` group은 `main` 3 columns와 `context` 1 column을 포함한다.
- `context` column은 main의 단일 column보다 넓게 렌더링될 수 있다.
- 오른쪽 system area는 별도 width를 가지며 watch list, agent chat, notification, settings/menu overlay에 최적화한다.
- `mainContext` span은 `workspace` group 안에서만 허용된다.
- `agentRail` 또는 오른쪽 system area와 `workspace` 사이의 span은 금지한다.

agent 아이콘은 `assets/agent-icons`의 자산을 사용한다. 현재 구현은 `frontend/public/assets/agent-icons`로 복사한 SVG를 agent config에서 참조한다. 초기 UI는 agent 4명까지 지원하며, 오른쪽 system shortcut rail은 `4 agents + 1 spacer + notification + menu`의 7칸 구조를 사용한다.

## Panel Layout Data Model 초안

초기 `WorkspaceDocument`는 layout, panel registry reference, layout history, saved layout reference를 함께 가진다.

```ts
type GridGroup = "workspace" | "agentRail";
type GridZone = "main" | "context" | "mainContext" | "agentRail";

type PanelSizeVariant =
  | "micro"
  | "compact"
  | "standard"
  | "wide"
  | "large";

type PanelPlacement = {
  group: GridGroup;
  zone: GridZone;
  col: number;
  row: number;
  colSpan: number;
  rowSpan: number;
  zIndex?: number;
};

type PanelResourceRef = {
  kind:
    | "chartDocument"
    | "newsQuery"
    | "agentThread"
    | "portfolioView"
    | "watchlist"
    | string;
  id: string;
};

type PanelInstance = {
  id: string;
  type: string;
  placement: PanelPlacement;
  props: Record<string, unknown>;
  resourceRefs?: PanelResourceRef[];
  layoutPinned?: boolean;
  layoutWeight?: number;
  variant?: PanelSizeVariant;
  createdBy: "user" | "llm" | "system";
  updatedAt: string;
};

type WorkspaceLayoutSettings = {
  llmLayoutAutoApply: boolean;
  reflowMode: "auto";
};

type WorkspaceLayout = {
  version: 1;
  zones: {
    workspace: { columns: 4; rows: 5; mainColumns: 3; contextColumns: 1 };
    agentRail: { columns: 1; rows: 5 };
  };
  settings: WorkspaceLayoutSettings;
  panels: PanelInstance[];
  selectedPanelId?: string;
};
```

현재 구현은 여기에 `DefaultLayoutKey = "chart" | "news" | "overview" | "signals"`, `FavoriteLayoutSlot = 1 | 2 | 3 | 4`, `LayoutPreviewItem`을 추가로 사용한다. layout preview는 drag 중 표시되는 ghost placement이며 실제 layout state는 command 적용 전까지 변경하지 않는다.

초기 구현에서는 `col`, `row`, `colSpan`, `rowSpan`을 1-based coordinate로 다루는 것을 권장한다. CSS grid로 변환할 때만 `grid-column`과 `grid-row`에 맞춰 매핑한다.

`layoutPinned`은 패널의 위치와 크기를 고정하는 UI pin toggle이다. `layoutPinned: true`인 패널은 move, resize, auto reflow, swap, shrink/expand 대상이 될 수 없다. 패널 내부 데이터, 차트 렌더링, 뉴스 내용 갱신은 layout pin과 별개의 책임이다.

## Panel Registry

차트는 특별한 전역 예외가 아니라 panel registry 안의 하나의 panel type이다. registry는 패널의 렌더러, 기본 크기, 허용 zone, 최소/최대 span, size variant mapping, command capability, reflow weight를 정의한다.

현재 구현된 panel type:

- `chart`: 시세 차트 패널. 여러 개 생성될 수 있으며 각 instance는 별도의 `chartDocument`를 참조할 수 있다.
- `watchlist`: 관심 종목 목록. 여러 watchlist instance를 허용할 수 있다.
- `symbolSummary`: 선택 종목 요약.
- `newsFeed`: 종목/시장 뉴스. query나 symbol context에 따라 여러 instance를 둘 수 있다.
- `proposalReview`: LLM 제안 검토 패널.
- `agentStatus`: registry에는 남아 있지만 현재 UI에서는 오른쪽 system area의 agent 상태/대화 surface로 표현한다.
- `agentChat`: registry에는 남아 있지만 현재 UI에서는 agent 버튼을 눌렀을 때 오른쪽 system area 전체를 쓰는 대화 surface로 표현한다.
- `indicatorCompare`: 보조지표 비교 패널.
- `aiSummary`: LLM 분석 요약 패널.
- `notifications`: 알림/제안 대기 영역.

registry metadata 예시:

```ts
type PanelDefinition = {
  type: string;
  title: string;
  allowedZones: GridZone[];
  defaultPlacement: PanelPlacement;
  minSpan: { colSpan: number; rowSpan: number };
  maxSpan?: { colSpan: number; rowSpan: number };
  defaultWeight: number;
  variants: Partial<Record<PanelSizeVariant, PanelVariantDefinition>>;
  commands: string[];
};
```

패널의 상태와 리소스 소유권은 panel type별로 분리한다. 예를 들어 `chart` panel은 `props`에 symbol/timeframe/layer 전체를 넣지 않고 `resourceRefs: [{ kind: "chartDocument", id }]` 또는 명시적인 `chartDocumentId`를 통해 `ChartDocument`를 참조한다. 뉴스, 요약, 에이전트 대화 패널도 같은 방식으로 각자의 resource reference를 가진다.

## Size Variant 규칙

같은 panel type도 아이폰 위젯처럼 크기에 따라 다른 UI variant를 가진다. variant는 panel type별 registry에서 정의하고, layout runtime은 placement 크기와 현재 viewport를 기준으로 추천 variant를 계산한다.

- `micro`: agent rail 1칸, 작은 상태 표시, 아이콘 중심 UI.
- `compact`: 1x1 또는 좁은 영역. 핵심 수치와 짧은 액션 중심.
- `standard`: 일반 패널 기본 형태. 목록, 요약, 간단한 chart preview에 적합.
- `wide`: 가로 span이 넓은 형태. chart, 비교 목록, timeline에 적합.
- `large`: 많은 행/열을 차지하는 상세 작업 형태. 주 차트와 복합 분석 패널에 적합.

variant 선택은 placement 변경 후 registry의 `resolveVariant(panel, placement, viewport)` 같은 순수 함수로 계산한다. layout auto reflow로 패널 크기가 바뀌면 variant도 함께 재계산한다.

## Layout Control Policy

레이아웃 제어는 패널별 pin toggle과 전역 LLM auto toggle로 나눈다. 전역 LLM auto toggle은 layout command와 chart command의 공통 정책이지만, 이 문서는 layout command 처리만 정의한다.

패널별 pin toggle:

- 각 패널은 `layoutPinned` 상태를 가진다.
- pin이 켜진 패널의 위치와 크기는 사용자 drag/resize, LLM command, auto reflow, swap, shrink/expand로 변경될 수 없다.
- pin은 패널 내부 데이터 갱신이나 차트 렌더링 갱신을 막지 않는다.

Top app bar의 LLM auto toggle:

- `llmLayoutAutoApply: true`이면 LLM이 제안한 layout command를 validation과 command journal 기록 후 즉시 적용한다.
- `llmLayoutAutoApply: false`이면 LLM이 제안한 layout command를 pending proposal로 표시하고 사용자 승인 후 적용한다.
- chart command도 같은 전역 auto toggle 값을 참조한다. chart command의 proposal, grouped apply, undo/redo 정책은 chart rendering runtime에서 정의한다.
- 전역 auto toggle은 layout/chart 분석 UI command에만 적용한다. 주문 생성, 취소, 정정 같은 거래 command에는 적용하지 않는다.

Undo/Redo:

- top app bar undo/redo는 layout 변경만 대상으로 한다.
- 사용자 조작과 LLM 적용으로 발생한 layout 변경은 모두 layout undo/redo 대상이다.
- layout undo/redo는 layout command journal 또는 snapshot history를 기반으로 한다.
- LLM auto 적용도 사용자가 되돌릴 수 있어야 한다.
- chart panel 내부의 chart undo/redo는 해당 chart panel의 chart history만 대상으로 한다.

Layout save/load:

- 사용자는 settings panel의 Layouts 탭에서 현재 layout을 사용자 layout으로 저장할 수 있다.
- 기본 layout은 `Chart`, `News`, `Overview`, `Signals` 네 개다.
- favorite layout은 top app bar에 4칸만 노출한다.
- 사용자 layout은 4개까지 저장한다. 기본 4개와 사용자 4개를 합쳐 localStorage record는 최대 8개를 읽는다.
- 저장 목록에서 layout을 불러오면 기존 layout을 교체하되, invalid resource reference나 더 이상 존재하지 않는 panel type은 복구 정책을 따른다.
- MVP는 localStorage 저장으로 시작하지만, 문서 모델에는 version을 둬 backend persistence와 migration을 준비한다.

## Command Runtime

사용자, LLM, system은 같은 command 처리 경로를 사용한다. 차이는 `actor`, auto 적용 여부, 승인 흐름에 있다.

- `actor: "user"`: 사용자의 직접 조작. validation 통과 시 즉시 적용한다.
- `actor: "llm"`: LLM 제안. layout command는 `llmLayoutAutoApply`가 켜져 있으면 즉시 적용하고, 꺼져 있으면 layout proposal에 저장한다. chart command도 같은 전역 auto toggle을 참조하지만 chart runtime에서 처리한다.
- `actor: "system"`: 초기 layout 생성, migration, 복구 같은 시스템 작업. audit journal에는 남긴다.

React component는 layout state를 직접 수정하지 않는다. drag, resize, remove, replace, save, load, undo, redo 같은 UI 조작도 모두 command로 변환해 command runtime에 전달한다.

command namespace는 layout command와 panel 내부 command를 분리한다.

- layout command: `layout.panel.move`, `layout.boundary.resize`, `layout.undo`
- chart command: `chart.indicator.add`, `chart.timeframe.change`
- news/agent/portfolio command도 panel 내부 책임에 맞는 namespace를 가진다.

command envelope 초안:

```ts
type LayoutCommand = {
  id: string;
  type: string;
  actor: "user" | "llm" | "system";
  target?: { panelId?: string; group?: GridGroup; zone?: GridZone };
  payload: Record<string, unknown>;
  createdAt: string;
  proposalId?: string;
};
```

MVP에서는 한 proposal이 하나의 history scope만 변경한다. layout proposal은 layout history만, chart proposal은 chart panel 내부 chart history만 변경한다. layout command와 chart command가 섞인 복합 proposal은 후속 `Workspace-level grouped history`가 준비된 뒤 허용한다.

## MVP Command 목록

MVP에서 우선 고려할 layout command는 다음과 같다.

- `layout.panel.add`: registry에 등록된 panel type을 특정 placement에 추가한다.
- `layout.panel.remove`: panel을 제거한다.
- `layout.panel.move`: panel 위치를 이동한다.
- `layout.boundary.resize`: 인접 panel 사이의 가상 boundary를 이동해 양쪽 panel의 placement를 함께 조정한다. 현재 구현은 corner handle 기반 `layout.panel.resize`를 쓰지 않는다.
- `layout.panel.replace`: 기존 panel slot을 다른 panel type으로 교체한다.
- `layout.panel.pin`: panel을 `layoutPinned: true`로 바꾼다.
- `layout.panel.unpin`: panel의 layout pin을 해제한다.
- `layout.panel.select`: inspector 또는 context column이 참조할 selected panel을 바꾼다.
- `layout.reflow`: 현재 layout을 검증하고, 후속 packing 고도화 지점으로 남겨둔다. 현재 구현은 적극적인 빈칸 자동 최소화보다 직접 조작과 chunk swap을 우선한다.
- `layout.undo`: 직전 layout 변경을 되돌린다.
- `layout.redo`: 되돌린 layout 변경을 다시 적용한다.
- `layout.save`: 현재 layout snapshot을 저장한다.
- `layout.load`: 저장된 layout snapshot을 불러온다.
- `layout.reset`: MVP 기본 layout으로 되돌린다.
- `layout.autoApply.set`: top app bar의 전역 LLM auto toggle 값을 바꾼다.

모든 command는 적용 전에 grid boundary, agent rail 분리 규칙, panel registry 제약, pin 정책, collision/reflow 정책을 검사한다.

## Collision, Reflow, 빈칸 처리

MVP에서도 자동 밀어내기, swap, chunk swap을 허용한다. 현재 구현은 완전한 빈칸 최소화 packing보다 사용자가 직접 조작하는 Bento 편집감을 우선한다. 단, chunk swap은 같은 footprint 교환에 한정한다. 큰 panel이 작은 chunk로 이동하며 자동 축소되고, 작은 panel 묶음이 큰 source footprint로 이동하며 자동 확대되는 `swap-resize`는 MVP에서 구현하지 않는다.

기본 처리 순서:

1. command 대상 panel의 요청 placement를 계산한다.
2. `layoutPinned` panel과 충돌하는지 확인한다. 충돌하면 해당 command는 실패한다.
3. unpinned panel과 충돌하면 먼저 같은 footprint의 chunk swap 가능한지 확인한다. 예를 들어 `1x3` panel은 `1x1 + 1x1 + 1x1`, `1x2` panel은 `1x1 + 1x1` 묶음과 교환될 수 있다.
4. chunk swap이 부적절하면 주변 unpinned panel을 밀어내는 push reflow를 시도한다.
5. 이동/resize 중에는 적용 가능한 다음 placement를 매우 투명한 ghost preview로 표시한다.
6. 모든 panel이 grid boundary와 min/max span을 만족하면 적용한다.
7. 만족하지 못하면 전체 command를 실패 처리하고 기존 layout을 유지한다.

같은 위치, 같은 크기, 같은 결과 layout을 만드는 no-op move/boundary resize/reflow는 적용하지 않고 history에도 남기지 않는다. LLM/system actor가 같은 no-op command를 보내도 undo stack을 오염시키지 않는 것을 원칙으로 한다.

가중치 원칙:

- `layoutWeight`는 panel type별 기본 중요도 metadata로 남긴다.
- 현재 구현은 `layoutWeight`로 빈칸을 자동 흡수하지 않는다.
- `layoutPinned` 패널은 weight와 무관하게 위치/크기 변경 대상에서 제외한다.
- panel type별 기본 weight는 registry에 둔다.

후속 고도화:

- 더 정교한 packing 알고리즘은 별도 layout engine으로 분리할 수 있다.
- 알고리즘만으로 안정적인 배치가 어렵다면 화면배치 Agent를 도입해 후보 layout을 생성하고 command proposal로 검증할 수 있다.
- Layout Agent가 도입되더라도 직접 state를 바꾸지 않고 `layout.*` command proposal을 생성해야 한다.

## MVP 포함 범위

초기 MVP는 레이아웃 런타임 골격에 집중한다.

- React + TypeScript 단일 페이지 app shell 기준 수립
- top app bar와 workspace area 구조
- top app bar의 검색, favorite layout 4칸, layout undo/redo, auto toggle, headline/alert strip, agent shortcut rail 기준
- `main`, `context`, `mainContext`, `agentRail` zone 모델
- panel registry의 최소 metadata 구조
- panel instance와 placement data model
- add/remove/move/boundary resize/replace/pin command validation 기준
- 자동 chunk swap, push reflow의 기본 알고리즘
- panel size에 따른 variant 선택 규칙의 최소 버전
- `assets/agent-icons` 기반 top app bar agent shortcut 참조 방식
- command journal 또는 layout history의 최소 형태
- local saved layout 목록의 최소 형태

## MVP 제외 범위

초기 MVP 제외 범위는 다음과 같다.

- 이 문서 범위 안에서의 실제 차트 캔들 렌더링과 시계열 drawing
- OpenAI API 호출과 실제 LLM 응답 생성
- WebSocket 실시간 데이터 연결
- 주문 시스템, 체결 상태, 계좌 보안
- GraphRAG, 외부 문서 검색, 장기 memory
- 화면배치 Agent의 실제 구현
- 서버 기반 layout persistence와 계정 간 동기화
- 권한/협업 편집

FastAPI backend는 health check 같은 최소 scaffold를 둘 수 있지만, layout persistence API는 MVP에서 필수로 보지 않는다.

## 추천 구현 순서

1. `WorkspaceLayout`, `PanelInstance`, `PanelPlacement`, `GridGroup`, `GridZone` 타입을 정의한다.
2. 기본 layout seed를 만든다. chart는 `chart` panel type instance로 배치하고 `chartDocumentId` 또는 `resourceRefs`를 준비한다.
3. panel registry를 만들고 placeholder panel renderer를 연결한다.
4. CSS grid 기반 app shell을 구성한다. top app bar, main workspace, context column, 오른쪽 system area의 시각적 경계를 확인한다.
5. command runtime의 reducer 또는 command handler를 만든다.
6. `layout.panel.add`, `layout.panel.remove`, `layout.panel.move`, `layout.boundary.resize`부터 validation과 함께 구현한다.
7. `layoutPinned`을 구현해 pinned panel의 move/resize/reflow를 막는다.
8. chunk swap, push reflow 알고리즘의 MVP 버전을 구현한다.
9. size variant resolver를 panel registry에 연결한다.
10. top app bar의 auto toggle, layout undo/redo, favorite layout load UI와 settings panel의 save/load UI를 command에 연결한다.
11. top app bar 오른쪽 shortcut rail에 `assets/agent-icons`를 사용한 agent 버튼, notification 버튼, menu 버튼을 배치한다.
12. command debug view 또는 test fixture로 user/llm/system actor command가 같은 경로를 타는지 검증한다.

## 검증 기준

다음 조건을 만족하면 이 단계의 레이아웃 기준을 구현 가능한 상태로 본다.

- `main`, `context`, `mainContext`, `agentRail` zone이 data model에 존재하고, 현재 UI에서는 `agentRail` 책임을 top app bar shortcut rail과 오른쪽 system area로 표현한다.
- `mainContext` span은 workspace group 안에서 가능하다.
- 오른쪽 system area는 workspace group과 span을 공유하지 않는다.
- 차트는 전역 singleton이 아니라 여러 개 생성 가능한 `chart` panel type instance로 표현된다.
- chart panel은 `ChartDocument`를 참조하고, chart 내부 상태를 panel props에 흡수하지 않는다.
- 뉴스, 요약, 에이전트, 포트폴리오 같은 다른 panel도 복수 instance와 resource reference를 가질 수 있다.
- panel placement는 command를 통해서만 변경된다.
- `layoutPinned` panel의 위치와 크기는 user/LLM/system/reflow에 의해 바뀌지 않는다.
- LLM layout command는 top app bar auto toggle 상태에 따라 즉시 적용 또는 proposal 대기로 나뉜다.
- top app bar undo/redo는 user와 LLM이 만든 layout 변경을 모두 대상으로 한다.
- chart panel 내부 undo/redo는 chart rendering runtime의 chart history를 대상으로 한다.
- saved layout을 저장하고 불러오는 최소 모델이 존재한다.
- collision 발생 시 chunk swap 또는 push reflow를 시도한다.
- 같은 command 처리 경로를 user, LLM, system actor가 공유한다.
- panel registry가 각 panel type의 allowed zone, default size, min/max span, default weight, variant를 설명한다.

## 후속 고려 사항

- 당분간 panel zone 자유도는 유지한다. `mainContext` span 패널이 많아져 context column의 의미가 흐려지는 문제가 실제로 생기면 panel type별 allowed zone을 보수적으로 좁힌다.
- reflow 알고리즘이 복잡해지면 layout engine을 독립 모듈로 분리한다.
- 화면배치 Agent는 layout engine이 만든 후보를 대체하는 것이 아니라, 검증 가능한 `layout.*` command proposal을 생성하는 방식으로 붙인다.
- 서버 기반 saved layout, 사용자별 layout sync, layout migration은 backend 설계 시 별도 문서로 분리한다.
- 모바일 viewport에서는 오른쪽 system area와 top app bar shortcut rail을 접힘, 하단 rail, overlay 중 하나로 바꿔야 한다.
