# Merge Handoff Report - 2026-06-30

## Purpose

이 문서는 `origin/dev` 병합 이후 팀원이 프론트엔드를 다시 수정할 때 참고할 handoff 보고서다.
차트/시장데이터 담당 작업과 프론트엔드 담당 작업의 경계를 분리하고, 병합 중 임시로 시도했다가 원복한 UI 변경을 명확히 남긴다.

프론트엔드 시각 기준은 팀 프론트 담당자의 `dev` 코드와 배포 화면(`https://stargops.com/`)이다.
차트 담당 브랜치는 데이터 계약과 차트 런타임 안정성에 집중한다.

## Current State

- 로컬 브랜치: `dev`
- 최근 병합 커밋: `37d63ba Merge remote-tracking branch 'origin/dev' into dev`
- 이 보고서 작성 전 작업 트리는 clean 상태였다.
- 병합 후 로컬 Docker 런타임은 현재 소스 기준으로 `gops-frontend`, `gops-backend`를 재빌드/재시작했다.
- 차트 담당자가 임시로 넣었던 Hot Ranking UI 변경과 TopAppBar 로그인 버튼 변경은 모두 원복했다.

## Reverted Temporary Frontend Edits

다음 변경은 병합 확인 중 임시로 시도했으나, 프론트 담당자의 소유 영역을 침범하지 않기 위해 최종 코드에 남기지 않았다.
팀원이 다시 반영할 때는 아래 내용을 그대로 복붙하지 말고 프론트 담당 코드 스타일과 배포 화면 기준으로 재구현해야 한다.

| File | Temporary Change | Final State |
| --- | --- | --- |
| `apps/gops-frontend/src/App.tsx` | Hot Ranking API 호출을 `limit=10`으로 변경 | 원래 `limit=20` 유지 |
| `apps/gops-frontend/src/components/PanelCard.tsx` | Hot Ranking을 관심종목 행 구조처럼 바꾸고 Top 10으로 표시 | 기존 Hot Ranking 패널 구조 유지 |
| `apps/gops-frontend/src/components/SystemArea.tsx` | 패널 카탈로그 설명을 `거래대금 Top 10`으로 변경 | 기존 `거래대금 Top 20` 유지 |
| `apps/gops-frontend/src/components/TopAppBar.tsx` | Google 로그인/로컬 상태 버튼을 상단바에 삽입 | 삽입분 제거 |
| `apps/gops-frontend/src/styles.css` | 인증 버튼 fixed 스타일과 Hot Ranking 스타일 변경 | 삽입/삭제분 모두 원복 |

## Ownership Boundary

차트/시장데이터 담당자가 직접 수정해도 되는 범위:

- `apps/chart-engine/**`
- `systems/api-server/**`의 차트/시장데이터 API 계약
- `systems/market-data/**`의 수집, 저장, backfill, query, realtime 처리
- 프론트와 합의된 최소 adapter/type 변경

프론트 담당자 소유로 남겨야 하는 범위:

- `apps/gops-frontend/src/components/TopAppBar.tsx`
- `apps/gops-frontend/src/components/SystemArea.tsx`
- `apps/gops-frontend/src/components/PanelCard.tsx`의 패널 레이아웃/시각 표현
- `apps/gops-frontend/src/styles.css`의 전역 UI 스타일
- Google 로그인 버튼의 위치, 형태, 표시 정책
- Hot Ranking/Watch List의 최종 시각 디자인

차트 담당자는 UI를 직접 고치기보다 프론트가 소비할 수 있는 안정적인 API 응답, 타입, 테스트 데이터를 제공하는 쪽으로 작업한다.

## Frontend Contracts To Reuse

### Hot Ranking

현재 백엔드는 거래대금 순위 API를 제공한다.

```text
GET /api/charts/hot-symbols?limit=10
```

계약:

- 기본 limit은 `20`.
- 요청 limit은 `1` 이상 `100` 이하.
- UI에서 10위까지만 보여도 된다면 `limit=10`으로 호출하거나, 프론트에서 상위 10개만 잘라서 표시할 수 있다.
- ranking method는 `current_session_dollar_volume`.
- universe는 `sp500`.

응답 주요 필드:

```text
ranking.method
ranking.universe
ranking.limit
ranking.asOf
ranking.refreshSeconds
symbols[].rank
symbols[].symbol
symbols[].name
symbols[].market
symbols[].lastPrice
symbols[].changePercent
symbols[].volume
symbols[].sessionDollarVolume
symbols[].sourceUpdatedAt
symbols[].rankingWindow
symbols[].rankReason
```

프론트 재반영 제안:

- Hot Ranking UI는 관심종목 패널의 시각 언어를 참고하되, 구현은 프론트 담당 브랜치에서 수행한다.
- 거래대금은 `sessionDollarVolume`, 현재가는 `lastPrice`, 등락률은 `changePercent`를 사용한다.
- “Top 20 데이터를 받아 10개만 표시”와 “처음부터 `limit=10` 요청”은 둘 다 가능하다. 화면이 10개 고정이면 `limit=10` 요청이 더 명확하다.

### Watch List / Symbol Search

관련 API:

```text
GET /api/charts/symbols
GET /api/charts/watchlist
GET /api/charts/watchlist?symbols=AAPL,MSFT,NVDA
PUT /api/charts/watchlist
GET /api/market/symbols/search
```

프론트 재반영 제안:

- 기본 관심종목 목록, 관심종목 편집 UX, 검색 드롭다운 표현은 프론트 담당 코드 기준으로 유지한다.
- 차트/데이터 쪽은 S&P500 universe와 최신 quote/change 데이터가 위 API로 안정적으로 내려가게 보장한다.
- 사용자가 편집한 관심종목은 `PUT /api/charts/watchlist` 계약을 통해 저장/조회하는 방향을 유지한다.

### Google OAuth

프론트에는 이미 `AuthProvider`와 `useAuth()` 계약이 있다.

프론트에서 사용할 수 있는 값:

```text
authEnabled
user
loading
error
login()
logout()
refresh()
```

백엔드 라우트:

```text
GET  /api/auth/me
GET  /api/auth/google/login?returnTo=...
GET  /api/auth/google/callback
POST /api/auth/logout
```

프론트 재반영 제안:

- Google 로그인 버튼이 상단이나 고정 영역에 필요하다면 프론트 담당자가 기존 레이아웃 체계 안에서 배치한다.
- 현재 코드에는 SystemArea/OrderTicket 쪽 auth 사용 지점이 있으므로, 상단바에 새 버튼을 둘지 여부는 프론트 UX 결정으로 남긴다.
- 차트 담당 브랜치에서는 `TopAppBar`에 임의 버튼을 삽입하거나 fixed CSS override를 추가하지 않는다.

## Merge Risk Notes

- `styles.css`에는 Watch List/Panel row 관련 규칙이 여러 구간에 반복되어 있다. 프론트 담당자가 Hot Ranking을 관심종목 형식으로 다시 맞출 때는 마지막 override 우선순위를 확인해야 한다.
- auth disabled 로컬 환경에서는 `login()` 호출이 실제 Google 로그인으로 가지 않을 수 있다. UI는 `authEnabled=false` 상태도 명시적으로 처리해야 한다.
- Hot Ranking UI가 10개 표시로 결정되더라도 백엔드 기본값은 현재 20이다. API 기본값을 바꿀지, 프론트에서 `limit=10`을 요청할지는 프론트/데이터 담당자가 합의해야 한다.
- 차트 담당 작업 중 프론트 레이아웃 파일을 수정해야 할 경우, 먼저 담당자와 파일 소유권을 확인한다.

## Suggested Verification For Frontend Rework

프론트 담당자가 위 내용을 반영할 때 권장 검증:

```text
npm run test:chart
npm run build
```

브라우저 확인 항목:

- 배포 화면 기준의 상단바/패널 레이아웃이 깨지지 않는가.
- Hot Ranking이 의도한 개수만 표시되는가.
- Hot Ranking 행의 가격/등락률/거래대금이 API 응답과 일치하는가.
- Google 로그인 버튼이 필요한 위치에 보이고, `AUTH_ENABLED=true`에서 `/api/auth/google/login`으로 이동하는가.
- `AUTH_ENABLED=false` 로컬 모드에서 로그인 UI가 오작동하지 않는가.
