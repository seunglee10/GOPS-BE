# Frontend Handoff Report

## 목적

이 문서는 백엔드 개발자가 GOPS 프론트엔드의 현재 구현 전제와 연결 지점을 빠르게 파악하기 위한 첫 문서다.

프론트엔드는 `helix/front`의 workspace UI를 현재 GOPS repo 구조에 맞춰 `apps/gops-frontend`로 병합한 상태다. 이 문서는 현재 코드가 어떤 전제로 작성되어 있는지와 어디를 보면 되는지를 정리한다.

## 실행 구조

| 항목 | 현재 값 |
| --- | --- |
| 프론트 앱 | `apps/gops-frontend/` React + Vite |
| 차트 데이터 서버 | 기존 GOPS API server |
| 차트 에이전트 서버 | 기존 GOPS API server / agent orchestration |
| 차트 데이터 프록시 | `VITE_BACKEND_TARGET`, 기본 `http://127.0.0.1:8000` |
| 에이전트 프록시 | `/api` same-origin proxy |
| 온톨로지 리포트 경로 | `VITE_ONTOLOGY_REPORT_URL`, 기본 `/api/agents/analyze` |
| 주식 로고 | 선택적 `LOGODEV_PUB_KEY`, 없으면 티커 모노그램 표시 |

Vite dev proxy는 `apps/gops-frontend/vite.config.ts`에 있다. 현재 프론트는 같은 origin의 `/api/charts`, `/ws/charts`, `/api/llm/chat`, `/api/agents/analyze`, `/api/agents/reports/{analysisId}`를 사용한다.

S&P500 인기종목, 관심종목, 보유종목, 종목 검색, 기업정보 패널은
`apps/gops-frontend/src/components/StockLogo.tsx`를 통해 회사 로고를 렌더링한다.
Logo.dev 키가 없으면 외부 요청 없이 로컬 모노그램을 보여준다. 키가 있으면
Vite build 시점에 값이 정적 asset 안으로 들어가며, free/commercial plan의 link-back
요구를 위해 `VITE_LOGO_DEV_ATTRIBUTION=true`가 기본값이다.
`LOGODEV_SECRET_KEY`는 브라우저 번들에 넣으면 안 되므로 이 경로에서는 사용하지 않는다.

## 로컬 데모 자산

`helix/front`의 `mock_backend/`, `agent_backend/`, 더미 환경변수는 프론트 시연과 개발 검증을 위한 참조 자산이었고 이번 병합에는 들이지 않았다. 실제 데이터 소스와 운영 환경변수는 현재 GOPS backend/API server 기준을 따른다.

현재 TreeMap은 첫 렌더만 `apps/gops-frontend/src/market/sp500Universe.seed.ts`의 `sp500UniverseSeed`를 fallback으로 사용한다. 기본 데이터 경로는 `GET /api/market/heatmap?universe=sp500`이며, 백엔드는 트리맵에 필요한 `symbol/companyName/sector/industry/marketCap/layoutMarketCap/lastPrice/previousClose/changePercent`와 hover용 거래량만 compact projection으로 내려준다. 상세 재무 필드는 종목 선택 뒤 symbol별 fundamentals endpoint에서 조회한다. 프론트는 quote/color/current marketCap을 1분 주기로 갱신하고, 백엔드 `layoutAsOf`가 바뀌는 5분 경계에서만 `layoutMarketCap` 기준 타일 크기를 갱신한다.

## 화면 구조

첫 화면은 S&P500 TreeMap이다. TreeMap에서 종목을 선택하면 chart view로 전환된다.

chart view는 패널 workspace로 구성된다. 기본 패널은 `뉴스`, `온톨로지`, `차트`이며, `인기종목`, `지수`, `포트폴리오`, `거래`, `오더플로우`, 추가 `차트` 패널도 panel add 메뉴에서 생성될 수 있다.

포트폴리오 `성과` 패널은 `GET /api/account/performance?range=1W|1M|3M|1Y|ALL`의
사용자별 일간 snapshot 이력을 사용한다. 금액 이력이 있으면 snapshot의
`portfolioValue`, 가상계좌의 `netInvestedPrincipal`, Yahoo `^GSPC` 수익률을 첫 평가금에
환산한 비교 평가금을 동일한 금액 축에 표시한다. `netInvestedPrincipal`은 매매 원가가
아니라 현재 paper account generation의 시작 원금이며 계좌 초기화 시에만 바뀐다.
평가금과 시작 원금 사이에는 간극 밴드를 표시하고, 시작 원금이 평가금보다 높으면 파랑,
낮으면 빨강으로 채운다. 두 값이 교차하면 실제 교차 지점에서 밴드 색을 분리한다.
초기화 전 snapshot이 함께 포함되거나 시뮬레이터 과거시각이면 현재 시작 원금을 과거에
소급하지 않는다. 단, 현재 시드 계좌를 그대로 재현하는 불변 `seeded-demo` 이력은 같은 시드
시작 원금을 SIM에서도 보강한다. 기존 snapshot처럼 금액 필드가 없고 수익률만 두 시점 이상 있으면 기존
포트폴리오·S&P 500 퍼센트 비교 차트로 안전하게 fallback하며, 어떤 이력도 두 시점 미만이면
임의 시계열을 만들지 않고 빈 상태를 표시한다.
단, Vite DEV에서는 화면 검증을 위해 실제 이력이 부족하거나 API가 연결되지 않았을 때
고정된 `DEV DEMO` 성과 fixture를 표시한다. production build에는 이 fallback이 포함되지 않는다.

차트 패널은 좌우 page edge에 붙을 때 gutter 없이 flush된다. 내부 경계에서는 일반 패널과 같은 gutter 규칙을 따른다. 패널 위치, 크기, 추가, 삭제, content swap 로직은 `apps/gops-frontend/src/layout/panelLayout.ts`와 `apps/gops-frontend/src/components/PanelWorkspace.tsx`가 담당한다.

## 주요 코드 위치

| 영역 | 위치 |
| --- | --- |
| 앱 view 전환 | `apps/gops-frontend/src/App.tsx` |
| 패널 layout engine | `apps/gops-frontend/src/layout/panelLayout.ts` |
| 패널 shell/render orchestration | `apps/gops-frontend/src/components/PanelWorkspace.tsx` |
| 패널 content 연결 | `apps/gops-frontend/src/components/PanelContentRenderer.tsx` |
| 차트 데이터 client | `apps/gops-frontend/src/chart/cdcClient.ts` |
| 오더플로우 데이터/렌더러 | `apps/gops-frontend/src/chart/orderFlow.ts`, `apps/gops-frontend/src/chart/orderFlowClient.ts`, `apps/gops-frontend/src/chart/orderFlowRender.ts` |
| 차트 DTO/type | `apps/gops-frontend/src/chart/types.ts` |
| 온톨로지 client/mapper | `apps/gops-frontend/src/ontology/` |
| 하단 I-VI 메뉴와 중앙 채팅 dock | `apps/gops-frontend/src/components/BottomCommandBar.tsx` |
| 디자인 token/surface | `apps/gops-frontend/src/styles.css` |

## 디자인 전제

디자인 컨셉은 신문 지면 위의 얕은 물리감이다. 기본 화면은 flat하고, hover/focus/active 상태에서만 recessed, raised, floating surface가 드러난다.

색상은 `apps/gops-frontend/src/styles.css`의 GOPS palette와 semantic token을 기준으로 관리한다. 새 색상 literal을 추가하기보다 기존 token과 opacity를 우선 사용한다.

주요 surface class는 다음과 같다.

| class | 용도 |
| --- | --- |
| `surface-flat` | 기본 지면 상태 |
| `surface-recessed` | 입력창, 검색창, hover/focus로 들어간 표면 |
| `surface-raised` | 버튼처럼 살짝 올라온 조작 요소 |
| `surface-floating` | 하단 메뉴 패널, 중앙 채팅 로그 패널 |

## 새 패널과 버튼 연결 위치

새 패널 content는 `PanelContentKind`에 kind를 추가하고, `panelContentTitle`, `insertablePanelKinds`, `PanelContentRenderer`에 연결한다. Agent layout과도 연결되는 패널이면 `AgentLayoutPanelType`, `kindToPanelType`, `panelTypeToKind`를 함께 갱신한다. 패널의 위치/크기 규칙은 가능한 한 `panelLayout.ts`의 공통 규칙을 따른다.

하단 I-VI 메뉴는 `BottomCommandBar.tsx`에 있다. 현재 I 버튼에는 TreeMap 복귀 기능이 연결되어 있고, 나머지는 메뉴 panel shell만 준비되어 있다.

중앙 질문창은 chart agent 입력 경로다. 대화 로그 패널은 세션 메모리만 사용하며, 저장 API는 없다.

## 병합 시 참고

현재 문서는 프론트 구현 보고서다. 백엔드의 기존 구조가 더 적합하면 API 방식과 프론트 client를 함께 바꿔도 된다.

차트 데이터 연결은 `docs/cdc.md`, 온톨로지 리포트 연결은 `docs/ODC.md`를 보면 된다.
