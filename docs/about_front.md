# Frontend Handoff Report

## 목적

이 문서는 백엔드 개발자가 GOPS 프론트엔드의 현재 구현 전제와 연결 지점을 빠르게 파악하기 위한 첫 문서다.

프론트엔드는 아직 병합 전 개발 상태이며, 백엔드 병합 과정에서 API 경로, DTO, 일부 프론트 로직은 변경될 수 있다. 이 문서는 백엔드 구현을 제한하지 않는다. 현재 코드가 어떤 전제로 작성되어 있는지와 어디를 보면 되는지를 정리한다.

## 실행 구조

| 항목 | 현재 값 |
| --- | --- |
| 프론트 앱 | `frontend/` React + Vite |
| 로컬 차트 데이터 서버 | `mock_backend/` |
| 로컬 차트 에이전트 서버 | `agent_backend/` |
| 차트 데이터 프록시 | `VITE_BACKEND_TARGET`, 기본 `http://127.0.0.1:8000` |
| 에이전트 프록시 | `VITE_AGENT_TARGET`, 기본 `http://127.0.0.1:8010` |
| 온톨로지 리포트 경로 | `VITE_ONTOLOGY_REPORT_URL`, 기본 `/api/agent-analysis/run` |

Vite dev proxy는 `frontend/vite.config.ts`에 있다. 현재 프론트는 같은 origin의 `/api/charts`, `/ws/charts`, `/api/chart-agent`, `/api/agent-analysis`를 사용한다.

## 로컬 데모 자산

`mock_backend/`, 더미 환경변수, local seed는 프론트 시연과 개발 검증을 위한 참조 자산이다. 백엔드 병합 시 실제 데이터 소스와 운영 환경변수로 대체해야 하며, 이 로컬 자산을 실제 구현 기준으로 삼지 않는다.

현재 TreeMap은 `frontend/src/market/sp500Universe.seed.ts`의 `sp500UniverseSeed`를 직접 사용한다. `marketCap`과 `changePercent`는 정적 seed 값이며 실시간 API에 연결되어 있지 않다. 실제 연결 시 TreeMap 데이터는 백엔드가 관리하는 S&P500 전체의 분봉 기반 summary/live 데이터로 교체하면 된다. TreeMap 기본 입력에는 tick/trade stream이 필요하지 않다.

## 화면 구조

첫 화면은 S&P500 TreeMap이다. TreeMap에서 종목을 선택하면 chart view로 전환된다.

chart view는 패널 workspace로 구성된다. 기본 패널은 `뉴스`, `온톨로지`, `기업분석`, `차트`이며, `거래`와 추가 `차트` 패널도 panel add 메뉴에서 생성될 수 있다.

차트 패널은 좌우 page edge에 붙을 때 gutter 없이 flush된다. 내부 경계에서는 일반 패널과 같은 gutter 규칙을 따른다. 패널 위치, 크기, 추가, 삭제, content swap 로직은 `frontend/src/layout/panelLayout.ts`와 `frontend/src/components/PanelWorkspace.tsx`가 담당한다.

## 주요 코드 위치

| 영역 | 위치 |
| --- | --- |
| 앱 view 전환 | `frontend/src/App.tsx` |
| 패널 layout engine | `frontend/src/layout/panelLayout.ts` |
| 패널 shell/render orchestration | `frontend/src/components/PanelWorkspace.tsx` |
| 패널 content 연결 | `frontend/src/components/PanelContentRenderer.tsx` |
| 차트 데이터 client | `frontend/src/chart/cdcClient.ts` |
| 차트 DTO/type | `frontend/src/chart/types.ts` |
| 온톨로지 client/mapper | `frontend/src/ontology/` |
| 하단 I-VI 메뉴와 중앙 채팅 dock | `frontend/src/components/BottomCommandBar.tsx` |
| 디자인 token/surface | `frontend/src/styles.css` |

## 디자인 전제

디자인 컨셉은 신문 지면 위의 얕은 물리감이다. 기본 화면은 flat하고, hover/focus/active 상태에서만 recessed, raised, floating surface가 드러난다.

색상은 `frontend/src/styles.css`의 GOPS palette와 semantic token을 기준으로 관리한다. 새 색상 literal을 추가하기보다 기존 token과 opacity를 우선 사용한다.

주요 surface class는 다음과 같다.

| class | 용도 |
| --- | --- |
| `surface-flat` | 기본 지면 상태 |
| `surface-recessed` | 입력창, 검색창, hover/focus로 들어간 표면 |
| `surface-raised` | 버튼처럼 살짝 올라온 조작 요소 |
| `surface-floating` | 하단 메뉴 패널, 중앙 채팅 로그 패널 |

## 새 패널과 버튼 연결 위치

새 패널 content는 `PanelContentKind`에 kind를 추가하고, `panelContentTitle`, `insertablePanelKinds`, `PanelContentRenderer`에 연결한다. 패널의 위치/크기 규칙은 가능한 한 `panelLayout.ts`의 공통 규칙을 따른다.

하단 I-VI 메뉴는 `BottomCommandBar.tsx`에 있다. 현재 I 버튼에는 TreeMap 복귀 기능이 연결되어 있고, 나머지는 메뉴 panel shell만 준비되어 있다.

중앙 질문창은 chart agent 입력 경로다. 대화 로그 패널은 세션 메모리만 사용하며, 저장 API는 없다.

## 병합 시 참고

현재 문서는 프론트 구현 보고서다. 백엔드의 기존 구조가 더 적합하면 API 방식과 프론트 client를 함께 바꿔도 된다.

차트 데이터 연결은 `docs/CDC.md`, 온톨로지 리포트 연결은 `docs/ODC.md`를 보면 된다.
